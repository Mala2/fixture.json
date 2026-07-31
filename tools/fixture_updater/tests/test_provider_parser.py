from __future__ import annotations

import copy
import io
import json
import logging
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

UPDATER_DIR = Path(__file__).resolve().parents[1]
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

from provider_api_football import (  # noqa: E402
    ApiFootballClient,
    NoUpcomingFixture,
    ProviderHttpError,
    ProviderResponseError,
    load_provider_sample,
    parse_provider_envelope,
    sanitize_provider_errors,
)

FIXTURES_DIR = Path(__file__).with_name("fixtures")
VALID_SAMPLE_PATH = FIXTURES_DIR / "api_football_next_fixture.json"
FIXED_NOW = datetime(2026, 7, 31, 0, 48, tzinfo=timezone.utc)


def load_json(name: str) -> dict:
    with (FIXTURES_DIR / name).open("r", encoding="utf-8") as sample:
        return json.load(sample)


def make_payload(entries: list[dict]) -> dict:
    return {
        "errors": [],
        "results": len(entries),
        "response": entries,
    }


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"X-RateLimit-Remaining": "99"}
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]


class ProviderParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid = load_json("api_football_next_fixture.json")
        self.valid_entry = self.valid["response"][0]

    def select(
        self,
        payload: object,
        *,
        team_id: int = 2939,
        logger: logging.Logger | None = None,
    ):
        return parse_provider_envelope(
            payload,
            team_id,
            now_utc=FIXED_NOW,
            logger=logger,
        )

    def entry(
        self,
        fixture_id: int,
        kickoff: str,
        status: str,
    ) -> dict:
        entry = copy.deepcopy(self.valid_entry)
        entry["fixture"]["id"] = fixture_id
        entry["fixture"]["date"] = kickoff
        entry["fixture"]["status"]["short"] = status
        return entry

    def test_valid_upcoming_fixture(self) -> None:
        fixture = self.select(self.valid)
        self.assertEqual(fixture.provider_fixture_id, 1234567)
        self.assertEqual(fixture.home_team.provider_id, 2939)
        self.assertEqual(fixture.away_team.name, "Al Nassr")

    def test_empty_provider_response(self) -> None:
        payload = load_json("api_football_empty_response.json")
        with self.assertRaises(NoUpcomingFixture):
            self.select(payload)

    def test_provider_errors_field(self) -> None:
        payload = load_json("api_football_error_response.json")
        with self.assertRaisesRegex(
            ProviderResponseError,
            "provider returned",
        ):
            self.select(payload)

    def test_sanitize_provider_errors_object_redacts_api_key(self) -> None:
        secret = "highly-sensitive-test-key"
        errors = {
            "token": f"Invalid key {secret}",
            f"field-{secret}": {"supplied": secret},
        }
        sanitized = sanitize_provider_errors(errors, secret)
        self.assertEqual(
            json.loads(sanitized),
            {
                "token": "Invalid key ***",
                "field-***": {"supplied": "***"},
            },
        )
        self.assertNotIn(secret, sanitized)

    def test_sanitize_provider_errors_array_redacts_api_key(self) -> None:
        secret = "highly-sensitive-test-key"
        errors = [
            {"token": secret},
            f"Repeated {secret} and {secret}",
        ]
        sanitized = sanitize_provider_errors(errors, secret)
        self.assertEqual(
            json.loads(sanitized),
            [
                {"token": "***"},
                "Repeated *** and ***",
            ],
        )
        self.assertNotIn(secret, sanitized)

    def test_random_order_selects_earliest_future_fixture(self) -> None:
        payload = load_json("api_football_multi_fixture.json")
        fixture = self.select(payload)
        self.assertEqual(fixture.provider_fixture_id, 5000002)
        self.assertEqual(fixture.kickoff, "2026-08-05T18:00:00Z")

    def test_finished_and_cancelled_fixtures_are_excluded(self) -> None:
        payload = make_payload(
            [
                self.entry(10, "2026-08-01T18:00:00Z", "FT"),
                self.entry(11, "2026-08-02T18:00:00Z", "CANC"),
                self.entry(12, "2026-08-03T18:00:00Z", "NS"),
            ]
        )
        self.assertEqual(self.select(payload).provider_fixture_id, 12)

    def test_postponed_fixture_is_usable(self) -> None:
        payload = make_payload(
            [self.entry(20, "2026-08-04T18:00:00Z", "PST")]
        )
        self.assertEqual(self.select(payload).status_code, "PST")

    def test_live_fixture_is_preferred_over_earlier_scheduled(self) -> None:
        payload = make_payload(
            [
                self.entry(30, "2026-08-01T18:00:00Z", "NS"),
                self.entry(31, "2026-07-30T18:00:00Z", "1H"),
            ]
        )
        self.assertEqual(self.select(payload).provider_fixture_id, 31)

    def test_past_scheduled_fixture_is_excluded(self) -> None:
        payload = make_payload(
            [
                self.entry(40, "2026-07-30T18:00:00Z", "NS"),
                self.entry(41, "2026-08-03T18:00:00Z", "NS"),
            ]
        )
        self.assertEqual(self.select(payload).provider_fixture_id, 41)

    def test_scheduled_fixture_uses_five_minute_clock_tolerance(self) -> None:
        payload = make_payload(
            [
                self.entry(42, "2026-07-31T00:44:00Z", "NS"),
                self.entry(43, "2026-08-03T18:00:00Z", "NS"),
            ]
        )
        self.assertEqual(self.select(payload).provider_fixture_id, 42)

    def test_target_team_away_fixture_is_accepted(self) -> None:
        entry = self.entry(50, "2026-08-03T18:00:00Z", "NS")
        teams = entry["teams"]
        teams["home"], teams["away"] = teams["away"], teams["home"]
        fixture = self.select(make_payload([entry]))
        self.assertEqual(fixture.away_team.provider_id, 2939)

    def test_unknown_status_is_logged_and_skipped(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("provider-unknown-status-test")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))
        payload = make_payload(
            [
                self.entry(60, "2026-08-01T18:00:00Z", "XYZ"),
                self.entry(61, "2026-08-02T18:00:00Z", "NS"),
            ]
        )
        fixture = self.select(payload, logger=logger)
        self.assertEqual(fixture.provider_fixture_id, 61)
        self.assertIn("unknown status XYZ", stream.getvalue())

    def test_invalid_kickoff_is_logged_and_skipped(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("provider-invalid-kickoff-test")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))
        payload = make_payload(
            [
                self.entry(70, "not-a-timestamp", "NS"),
                self.entry(71, "2026-08-02T18:00:00Z", "NS"),
            ]
        )
        fixture = self.select(payload, logger=logger)
        self.assertEqual(fixture.provider_fixture_id, 71)
        self.assertIn("invalid structure", stream.getvalue())

    def test_no_usable_candidate_logs_exclusion_counts(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger("provider-no-candidate-test")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))
        invalid = self.entry(82, "invalid", "NS")
        payload = make_payload(
            [
                self.entry(80, "2026-08-01T18:00:00Z", "FT"),
                self.entry(81, "2026-07-30T18:00:00Z", "NS"),
                invalid,
            ]
        )
        with self.assertRaises(NoUpcomingFixture):
            self.select(payload, logger=logger)
        logged = stream.getvalue()
        self.assertIn("Total provider results: 3", logged)
        self.assertIn(
            "Excluded fixtures: status=1, date=1, invalid_structure=1",
            logged,
        )
        self.assertIn("Usable fixture candidates: 0", logged)

    def test_invalid_provider_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"response":', encoding="utf-8")
            with self.assertRaisesRegex(
                ProviderResponseError,
                "could not parse",
            ):
                load_provider_sample(path)

    def test_http_error(self) -> None:
        def failing_opener(*args: object, **kwargs: object) -> object:
            raise urllib.error.HTTPError(
                "https://v3.football.api-sports.io/fixtures",
                401,
                "Unauthorized",
                {},
                None,
            )

        client = ApiFootballClient(
            "not-a-real-key",
            maximum_attempts=1,
            opener=failing_opener,
        )
        with self.assertRaisesRegex(ProviderHttpError, "401"):
            client.fetch_next_fixture(2939, now_utc=FIXED_NOW)

    def test_request_uses_dynamic_utc_date_range_without_next(self) -> None:
        captured_url = ""

        def capturing_opener(request, **kwargs):
            nonlocal captured_url
            captured_url = request.full_url
            return FakeResponse(self.valid)

        client = ApiFootballClient(
            "not-a-real-key",
            maximum_attempts=1,
            opener=capturing_opener,
        )
        client.fetch_next_fixture(
            2939,
            now_utc=datetime(
                2026,
                7,
                30,
                20,
                0,
                tzinfo=timezone(-timedelta(hours=7)),
            ),
            lookahead_days=180,
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(captured_url).query
        )
        self.assertNotIn("next", query)
        self.assertEqual(query["team"], ["2939"])
        self.assertEqual(query["from"], ["2026-07-31"])
        self.assertEqual(query["to"], ["2027-01-27"])
        self.assertEqual(query["timezone"], ["UTC"])

    def test_api_key_never_appears_in_logs(self) -> None:
        secret = "highly-sensitive-test-key"
        stream = io.StringIO()
        logger = logging.getLogger("provider-secret-test")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))
        payload = copy.deepcopy(self.valid)
        payload["response"][0]["teams"]["away"]["name"] = (
            f"Opponent {secret}"
        )

        client = ApiFootballClient(
            secret,
            maximum_attempts=1,
            opener=lambda *args, **kwargs: FakeResponse(payload),
            logger=logger,
        )
        result = client.fetch_next_fixture(2939, now_utc=FIXED_NOW)
        self.assertEqual(result.remaining_requests, 99)
        self.assertNotIn(secret, stream.getvalue())
        self.assertIn(
            "/fixtures?team=2939&from=2026-07-31"
            "&to=2027-01-27&timezone=UTC",
            stream.getvalue(),
        )
        self.assertNotIn("next=", stream.getvalue())

    def test_provider_error_logs_safe_response_diagnostics(self) -> None:
        secret = "highly-sensitive-test-key"
        stream = io.StringIO()
        logger = logging.getLogger("provider-error-diagnostics-test")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))
        payload = {
            "errors": {
                "token": f"Invalid API key {secret}",
                "nested": [secret],
            },
            "results": 0,
            "response": [],
        }
        response = FakeResponse(
            payload,
            headers={
                "X-RateLimit-Remaining": "9",
                "x-ratelimit-requests-remaining": "87",
            },
        )
        client = ApiFootballClient(
            secret,
            maximum_attempts=1,
            opener=lambda *args, **kwargs: response,
            logger=logger,
        )

        with self.assertRaisesRegex(
            ProviderResponseError,
            "provider returned",
        ):
            client.fetch_next_fixture(2939, now_utc=FIXED_NOW)

        logged = stream.getvalue()
        self.assertNotIn(secret, logged)
        self.assertIn("API-Football HTTP status: 200", logged)
        self.assertIn("API-Football results: 0", logged)
        self.assertIn(
            "rate limit remaining (x-ratelimit-remaining): 9",
            logged,
        )
        self.assertIn(
            "rate limit remaining "
            "(x-ratelimit-requests-remaining): 87",
            logged,
        )
        self.assertIn("API-Football provider errors:", logged)
        self.assertIn('"token": "Invalid API key ***"', logged)
        self.assertIn('"nested": [\n    "***"\n  ]', logged)


if __name__ == "__main__":
    unittest.main()
