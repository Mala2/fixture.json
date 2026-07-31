from __future__ import annotations

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
    normalize_team_id,
    resolve_short_name,
)
from provider_thesportsdb import parse_events_envelope  # noqa: E402

FIXTURES_DIR = Path(__file__).with_name("fixtures")
FIXED_NOW = datetime(2026, 7, 31, 0, 48, tzinfo=timezone.utc)


def load_payload(name: str) -> dict:
    return json.loads(
        (FIXTURES_DIR / name).read_text(encoding="utf-8")
    )


def config() -> NormalizationConfig:
    return NormalizationConfig(
        target_team_id="136013",
        target_team_slug="al-hilal",
        target_team_short_name="HIL",
        target_team_name="Al Hilal",
        refresh_after_seconds=21600,
    )


class NormalizerTests(unittest.TestCase):
    def normalize_sample(self, name: str) -> dict:
        provider = parse_events_envelope(
            load_payload(name),
            "136013",
            now_utc=FIXED_NOW,
        )
        return normalize_fixture(
            provider,
            config(),
            {
                "Al-Hilal": "HIL",
                "Al Hilal": "HIL",
                "Al-Ahli Doha": "AHD",
            },
            now=FIXED_NOW,
        )

    def test_target_team_is_canonical_when_home(self) -> None:
        normalized = self.normalize_sample("thesportsdb_next_event.json")
        fixture = normalized["fixture"]
        self.assertEqual(fixture["home_away"], "home")
        self.assertEqual(normalized["team"]["id"], "al-hilal")
        self.assertEqual(fixture["home_team"]["id"], "al-hilal")
        self.assertEqual(fixture["home_team"]["name"], "Al Hilal")
        self.assertEqual(fixture["away_team"]["id"], "138049")
        self.assertEqual(normalized["team"], fixture["home_team"])
        self.assertNotIn("136013", json.dumps(normalized))

    def test_target_team_is_canonical_when_away(self) -> None:
        normalized = self.normalize_sample(
            "thesportsdb_target_team_away.json"
        )
        fixture = normalized["fixture"]
        self.assertEqual(fixture["home_away"], "away")
        self.assertEqual(normalized["team"]["id"], "al-hilal")
        self.assertEqual(fixture["away_team"]["id"], "al-hilal")
        self.assertEqual(fixture["away_team"]["name"], "Al Hilal")
        self.assertEqual(fixture["home_team"]["id"], "138049")
        self.assertEqual(normalized["team"], fixture["away_team"])
        self.assertNotIn("136013", json.dumps(normalized))

    def test_provider_id_mapping_is_explicit_and_provider_independent(self) -> None:
        self.assertEqual(
            normalize_team_id(
                "136013",
                provider_target_team_id="136013",
                canonical_target_team_id="al-hilal",
                team_name="Al-Hilal",
            ),
            "al-hilal",
        )
        self.assertEqual(
            normalize_team_id(
                "137000",
                provider_target_team_id="136013",
                canonical_target_team_id="al-hilal",
                team_name="Al-Ahli Doha",
            ),
            "137000",
        )
        self.assertEqual(
            normalize_team_id(
                None,
                provider_target_team_id="136013",
                canonical_target_team_id="al-hilal",
                team_name="Al-Ahli Doha",
            ),
            "al-ahli-doha",
        )

    def test_fixture_id_identifies_provider_without_leaking_target_id(self) -> None:
        normalized = self.normalize_sample("thesportsdb_next_event.json")
        self.assertEqual(
            normalized["fixture"]["id"],
            "thesportsdb-2549422",
        )

    def test_invalid_and_naive_normalized_kickoff_dates(self) -> None:
        for timestamp in (
            "2026-02-30T03:00:00Z",
            "2026-08-16T03:00:00",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(NormalizationError):
                    canonical_utc_timestamp(timestamp)

    def test_legacy_status_mapper_remains_strict(self) -> None:
        self.assertEqual(map_status("NS"), "scheduled")
        self.assertEqual(map_status("LIVE"), "live")
        self.assertEqual(map_status("FT"), "finished")
        self.assertEqual(map_status("PST"), "postponed")
        self.assertEqual(map_status("CANC"), "cancelled")
        with self.assertRaisesRegex(NormalizationError, "unknown.*XYZ"):
            map_status("XYZ")

    def test_abbreviation_alias_then_configured_then_fallback(self) -> None:
        self.assertEqual(
            resolve_short_name(
                "Al-Ahli Doha",
                aliases={"Al-Ahli Doha": "AHD"},
                configured_target_short_name=None,
            ),
            "AHD",
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
