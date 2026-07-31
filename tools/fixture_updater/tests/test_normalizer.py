from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

UPDATER_DIR = Path(__file__).resolve().parents[1]
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

from normalize_fixture import (  # noqa: E402
    NormalizationConfig,
    NormalizationError,
    canonical_utc_timestamp,
    fallback_abbreviation,
    map_status,
    normalize_fixture,
    resolve_short_name,
)
from provider_api_football import parse_provider_envelope  # noqa: E402

FIXTURES_DIR = Path(__file__).with_name("fixtures")
FIXED_NOW = datetime(2026, 7, 31, 0, 48, tzinfo=timezone.utc)


def load_valid_payload() -> dict:
    with (
        FIXTURES_DIR / "api_football_next_fixture.json"
    ).open("r", encoding="utf-8") as sample:
        return json.load(sample)


def config() -> NormalizationConfig:
    return NormalizationConfig(
        target_team_id=2939,
        target_team_slug="al-hilal",
        target_team_short_name="HIL",
        refresh_after_seconds=21600,
    )


class NormalizerTests(unittest.TestCase):
    def test_target_team_is_home(self) -> None:
        provider = parse_provider_envelope(load_valid_payload(), 2939)
        normalized = normalize_fixture(
            provider,
            config(),
            {"Al Hilal": "HIL", "Al Nassr": "NAS"},
            now=FIXED_NOW,
        )
        self.assertEqual(normalized["fixture"]["home_away"], "home")
        self.assertEqual(normalized["team"], normalized["fixture"]["home_team"])
        self.assertEqual(
            normalized["fixture"]["kickoff_utc"],
            "2026-08-16T03:00:00Z",
        )

    def test_target_team_is_away(self) -> None:
        payload = copy.deepcopy(load_valid_payload())
        teams = payload["response"][0]["teams"]
        teams["home"], teams["away"] = teams["away"], teams["home"]
        provider = parse_provider_envelope(payload, 2939)
        normalized = normalize_fixture(
            provider,
            config(),
            {"Al Hilal": "HIL", "Al Nassr": "NAS"},
            now=FIXED_NOW,
        )
        self.assertEqual(normalized["fixture"]["home_away"], "away")
        self.assertEqual(normalized["team"], normalized["fixture"]["away_team"])

    def test_invalid_and_naive_kickoff_dates(self) -> None:
        for timestamp in ("2026-02-30T03:00:00Z", "2026-08-16T03:00:00"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(NormalizationError):
                    canonical_utc_timestamp(timestamp)

    def test_every_supported_status_category(self) -> None:
        expected = {
            "TBD": "scheduled",
            "NS": "scheduled",
            "1H": "live",
            "HT": "live",
            "2H": "live",
            "ET": "live",
            "BT": "live",
            "P": "live",
            "SUSP": "live",
            "INT": "live",
            "LIVE": "live",
            "FT": "finished",
            "AET": "finished",
            "PEN": "finished",
            "PST": "postponed",
            "CANC": "cancelled",
            "ABD": "cancelled",
            "AWD": "cancelled",
            "WO": "cancelled",
        }
        for provider_status, normalized_status in expected.items():
            with self.subTest(provider_status=provider_status):
                self.assertEqual(
                    map_status(provider_status),
                    normalized_status,
                )

    def test_unknown_status_fails(self) -> None:
        with self.assertRaisesRegex(NormalizationError, "unknown.*XYZ"):
            map_status("XYZ")

    def test_abbreviation_alias_then_configured_then_fallback(self) -> None:
        self.assertEqual(
            resolve_short_name(
                "Al Nassr",
                aliases={"Al Nassr": "NAS"},
                configured_target_short_name="ALT",
            ),
            "NAS",
        )
        self.assertEqual(
            resolve_short_name(
                "Al Hilal",
                aliases={},
                configured_target_short_name="HIL",
            ),
            "HIL",
        )
        self.assertEqual(fallback_abbreviation("Real Madrid"), "RM")
        self.assertEqual(fallback_abbreviation("Ajax"), "AJA")


if __name__ == "__main__":
    unittest.main()
