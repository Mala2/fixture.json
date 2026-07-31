from __future__ import annotations

import copy
import io
import json
import logging
import sys
import tempfile
import unittest
import urllib.error
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


def load_json(name: str) -> dict:
    with (FIXTURES_DIR / name).open("r", encoding="utf-8") as sample:
        return json.load(sample)


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

    def test_valid_upcoming_fixture(self) -> None:
        fixture = parse_provider_envelope(self.valid, 2939)
        self.assertEqual(fixture.provider_fixture_id, 1234567)
        self.assertEqual(fixture.home_team.provider_id, 2939)
        self.assertEqual(fixture.away_team.name, "Al Nassr")

    def test_empty_provider_response(self) -> None:
        payload = load_json("api_football_empty_response.json")
        with self.assertRaises(NoUpcomingFixture):
            parse_provider_envelope(payload, 2939)

    def test_provider_errors_field(self) -> None:
        payload = load_json("api_football_error_response.json")
        with self.assertRaisesRegex(
            ProviderResponseError,
            "provider returned",
        ):
            parse_provider_envelope(payload, 2939)

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

    def test_missing_fixture_id(self) -> None:
        payload = copy.deepcopy(self.valid)
        del payload["response"][0]["fixture"]["id"]
        with self.assertRaisesRegex(ProviderResponseError, "fixture.id"):
            parse_provider_envelope(payload, 2939)

    def test_missing_kickoff_date(self) -> None:
        payload = copy.deepcopy(self.valid)
        del payload["response"][0]["fixture"]["date"]
        with self.assertRaisesRegex(ProviderResponseError, "fixture.date"):
            parse_provider_envelope(payload, 2939)

    def test_missing_venue(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["response"][0]["fixture"]["venue"] = None
        with self.assertRaisesRegex(ProviderResponseError, "fixture.venue"):
            parse_provider_envelope(payload, 2939)

    def test_target_team_absent(self) -> None:
        with self.assertRaisesRegex(ProviderResponseError, "absent"):
            parse_provider_envelope(self.valid, 9999)

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
            client.fetch_next_fixture(2939)

    def test_api_key_never_appears_in_logs(self) -> None:
        secret = "highly-sensitive-test-key"
        stream = io.StringIO()
        logger = logging.getLogger("provider-secret-test")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))

        client = ApiFootballClient(
            secret,
            maximum_attempts=1,
            opener=lambda *args, **kwargs: FakeResponse(self.valid),
            logger=logger,
        )
        result = client.fetch_next_fixture(2939)
        self.assertEqual(result.remaining_requests, 99)
        self.assertNotIn(secret, stream.getvalue())
        self.assertIn("/fixtures?team=2939&next=1", stream.getvalue())

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
            client.fetch_next_fixture(2939)

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
