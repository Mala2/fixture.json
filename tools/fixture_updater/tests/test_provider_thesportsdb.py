from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

UPDATER_DIR = Path(__file__).resolve().parents[1]
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

from provider_common import (  # noqa: E402
    NoUpcomingFixture,
    ProviderConfigurationError,
    ProviderResponseError,
)
from provider_thesportsdb import (  # noqa: E402
    TheSportsDbClient,
    map_thesportsdb_status,
    parse_events_envelope,
    parse_provider_timestamp,
    validate_team_lookup,
)

FIXTURES_DIR = Path(__file__).with_name("fixtures")
FIXED_NOW = datetime(2026, 7, 31, 0, 48, tzinfo=timezone.utc)


def load_payload(name: str) -> dict:
    return json.loads(
        (FIXTURES_DIR / name).read_text(encoding="utf-8")
    )


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class TheSportsDbProviderTests(unittest.TestCase):
    def test_home_event_is_parsed(self) -> None:
        fixture = parse_events_envelope(
            load_payload("thesportsdb_next_event.json"),
            "136013",
            now_utc=FIXED_NOW,
        )
        self.assertEqual(fixture.provider_name, "thesportsdb")
        self.assertEqual(fixture.provider_fixture_id, "2549422")
        self.assertEqual(fixture.home_team.provider_id, "136013")
        self.assertEqual(fixture.away_team.provider_id, "138049")
        self.assertEqual(fixture.kickoff, "2026-08-03T15:00:00Z")
        self.assertEqual(fixture.normalized_status, "scheduled")

    def test_away_event_is_parsed(self) -> None:
        fixture = parse_events_envelope(
            load_payload("thesportsdb_target_team_away.json"),
            "136013",
            now_utc=FIXED_NOW,
        )
        self.assertEqual(fixture.away_team.provider_id, "136013")
        self.assertEqual(fixture.away_team.name, "Al Hilal")
        self.assertEqual(fixture.home_team.provider_id, "138049")

    def test_team_lookup_validates_verified_identity(self) -> None:
        validate_team_lookup(
            load_payload("thesportsdb_team_136013.json"),
            "136013",
        )

    def test_empty_events_is_not_malformed_json(self) -> None:
        with self.assertRaises(NoUpcomingFixture):
            parse_events_envelope(
                load_payload("thesportsdb_empty_events.json"),
                "136013",
                now_utc=FIXED_NOW,
            )

    def test_invalid_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProviderResponseError,
            "no valid usable event.*invalid ISO-8601",
        ):
            parse_events_envelope(
                load_payload("thesportsdb_invalid_timestamp.json"),
                "136013",
                now_utc=FIXED_NOW,
            )

    def test_missing_event_id_is_rejected(self) -> None:
        payload = load_payload("thesportsdb_next_event.json")
        del payload["events"][0]["idEvent"]
        with self.assertRaisesRegex(
            ProviderResponseError,
            "idEvent.*required string",
        ):
            parse_events_envelope(
                payload,
                "136013",
                now_utc=FIXED_NOW,
            )

    def test_target_team_absent_is_rejected(self) -> None:
        payload = load_payload("thesportsdb_next_event.json")
        payload["events"][0]["idHomeTeam"] = "999001"
        with self.assertRaisesRegex(
            ProviderResponseError,
            "configured target team is absent",
        ):
            parse_events_envelope(
                payload,
                "136013",
                now_utc=FIXED_NOW,
            )

    def test_timestamp_conflict_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProviderResponseError,
            "conflicts with dateEvent",
        ):
            parse_provider_timestamp(
                "2026-08-04T15:00:00Z",
                date_event="2026-08-03",
                str_time="15:00:00",
                logger=logging.getLogger("test"),
            )

    def test_aware_timestamp_converts_to_utc(self) -> None:
        _, timestamp = parse_provider_timestamp(
            "2026-08-03T15:00:00+03:00",
            date_event="2026-08-03",
            str_time="15:00:00",
            logger=logging.getLogger("test"),
        )
        self.assertEqual(timestamp, "2026-08-03T12:00:00Z")

    def test_naive_timestamp_warns_and_means_utc(self) -> None:
        with self.assertLogs(level="WARNING") as captured:
            _, timestamp = parse_provider_timestamp(
                "2026-08-03T15:00:00",
                date_event="2026-08-03",
                str_time="15:00:00",
                logger=logging.getLogger("test-naive"),
            )
        self.assertEqual(timestamp, "2026-08-03T15:00:00Z")
        self.assertIn("treating it as UTC", "\n".join(captured.output))

    def test_missing_venue_uses_schema_safe_fallback(self) -> None:
        with self.assertLogs(level="WARNING") as captured:
            fixture = parse_events_envelope(
                load_payload("thesportsdb_missing_venue.json"),
                "136013",
                now_utc=FIXED_NOW,
            )
        self.assertEqual(fixture.venue_name, "Venue TBC")
        self.assertIn("venue is missing", "\n".join(captured.output))

    def test_status_mapping_is_explicit(self) -> None:
        cases = {
            "NS": "scheduled",
            "Not Started": "scheduled",
            "TBD": "scheduled",
            "POSTP": "postponed",
            "Postponed": "postponed",
            "CANC": "cancelled",
            "Cancelled": "cancelled",
            "FT": "finished",
            "Match Finished": "finished",
            "LIVE": "live",
            "In Progress": "live",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    map_thesportsdb_status(
                        source,
                        kickoff_utc=datetime(
                            2026, 8, 3, tzinfo=timezone.utc
                        ),
                        now_utc=FIXED_NOW,
                        logger=logging.getLogger("test-status"),
                    ),
                    expected,
                )

    def test_unknown_nonempty_status_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProviderResponseError,
            "unknown TheSportsDB status",
        ):
            parse_events_envelope(
                load_payload("thesportsdb_unknown_status.json"),
                "136013",
                now_utc=FIXED_NOW,
            )

    def test_empty_future_status_is_inferred_with_warning(self) -> None:
        payload = load_payload("thesportsdb_next_event.json")
        payload["events"][0]["strStatus"] = ""
        with self.assertLogs(level="WARNING") as captured:
            fixture = parse_events_envelope(
                payload,
                "136013",
                now_utc=FIXED_NOW,
            )
        self.assertEqual(fixture.normalized_status, "scheduled")
        self.assertIn("inferred scheduled", "\n".join(captured.output))

    def test_client_uses_https_query_parameter_and_never_logs_key(self) -> None:
        captured_request = []
        log_stream = io.StringIO()
        logger = logging.getLogger("test-client")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(log_stream))
        api_key = "secret-key-123"

        def opener(request: object, timeout: float) -> FakeResponse:
            captured_request.append((request, timeout))
            return FakeResponse(load_payload("thesportsdb_next_event.json"))

        result = TheSportsDbClient(
            api_key,
            opener=opener,
            maximum_attempts=1,
            logger=logger,
        ).fetch_next_event("136013", now_utc=FIXED_NOW)

        request, timeout = captured_request[0]
        self.assertEqual(timeout, 10.0)
        self.assertTrue(request.full_url.startswith("https://"))
        self.assertIn("/eventsnext.php?id=136013", request.full_url)
        self.assertEqual(result.fixture.provider_fixture_id, "2549422")
        logs = log_stream.getvalue()
        self.assertIn("HTTP status: 200", logs)
        self.assertIn("/eventsnext.php?id=136013", logs)
        self.assertNotIn(api_key, logs)
        self.assertNotIn('"events"', logs)

    def test_invalid_team_id_fails_before_http_request(self) -> None:
        called = False

        def opener(request: object, timeout: float) -> FakeResponse:
            nonlocal called
            called = True
            return FakeResponse({})

        client = TheSportsDbClient(
            "123",
            opener=opener,
            maximum_attempts=1,
        )
        with self.assertRaises(ProviderConfigurationError):
            client.fetch_next_event("not-numeric", now_utc=FIXED_NOW)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
