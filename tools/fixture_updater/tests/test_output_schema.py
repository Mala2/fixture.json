from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

UPDATER_DIR = Path(__file__).resolve().parents[1]
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

from normalize_fixture import NormalizationConfig, normalize_fixture  # noqa: E402
from provider_api_football import parse_provider_envelope  # noqa: E402
from validate_output import (  # noqa: E402
    OutputValidationError,
    load_and_validate,
    replace_if_meaningfully_changed,
    serialize_validated_fixture,
    validate_normalized_fixture,
)

FIXTURES_DIR = Path(__file__).with_name("fixtures")
FIXED_NOW = datetime(2026, 7, 31, 0, 48, tzinfo=timezone.utc)


def make_normalized(now: datetime = FIXED_NOW) -> dict:
    with (
        FIXTURES_DIR / "api_football_next_fixture.json"
    ).open("r", encoding="utf-8") as sample:
        provider = parse_provider_envelope(json.load(sample), 2939)
    return normalize_fixture(
        provider,
        NormalizationConfig(
            target_team_id=2939,
            target_team_slug="al-hilal",
            target_team_short_name="HIL",
            refresh_after_seconds=21600,
        ),
        {"Al Hilal": "HIL", "Al Nassr": "NAS"},
        now=now,
    )


class OutputSchemaTests(unittest.TestCase):
    def test_normalized_output_matches_firmware_schema(self) -> None:
        normalized = validate_normalized_fixture(make_normalized())
        self.assertEqual(normalized["schema_version"], 1)
        self.assertEqual(
            normalized["fixture"]["id"],
            "api-football-1234567",
        )
        self.assertEqual(
            normalized["fixture"]["kickoff_utc"],
            "2026-08-16T03:00:00Z",
        )
        self.assertEqual(
            normalized["team"],
            normalized["fixture"]["home_team"],
        )

    def test_deterministic_utf8_serialization(self) -> None:
        encoded = serialize_validated_fixture(make_normalized())
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertFalse(encoded.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(
            encoded,
            serialize_validated_fixture(json.loads(encoded)),
        )

    def test_existing_output_unchanged_after_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.json"
            output.write_bytes(serialize_validated_fixture(make_normalized()))
            original = output.read_bytes()
            invalid = copy.deepcopy(make_normalized())
            del invalid["fixture"]["venue"]
            with self.assertRaises(OutputValidationError):
                replace_if_meaningfully_changed(output, invalid)
            self.assertEqual(output.read_bytes(), original)

    def test_no_rewrite_when_only_generated_at_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.json"
            first = make_normalized()
            self.assertTrue(replace_if_meaningfully_changed(output, first))
            original = output.read_bytes()
            later = make_normalized(FIXED_NOW + timedelta(hours=6))
            self.assertFalse(replace_if_meaningfully_changed(output, later))
            self.assertEqual(output.read_bytes(), original)

    def test_new_fixture_atomically_replaces_old_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.json"
            original = make_normalized()
            replace_if_meaningfully_changed(output, original)
            replacement = copy.deepcopy(original)
            replacement["fixture"]["id"] = "api-football-7654321"
            replacement["fixture"]["kickoff_utc"] = "2026-08-23T03:00:00Z"
            self.assertTrue(
                replace_if_meaningfully_changed(output, replacement)
            )
            self.assertEqual(
                load_and_validate(output)["fixture"]["id"],
                "api-football-7654321",
            )

    def test_sample_dry_run_does_not_write_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.json"
            environment = os.environ.copy()
            environment["API_FOOTBALL_TEAM_ID"] = "2939"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(UPDATER_DIR / "update_fixture.py"),
                    "--dry-run",
                    "--provider-sample",
                    str(
                        FIXTURES_DIR /
                        "api_football_next_fixture.json"
                    ),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Dry run", completed.stdout)
            self.assertFalse(output.exists())

    def test_provider_validation_error_preserves_exit_code_four(self) -> None:
        environment = os.environ.copy()
        environment["API_FOOTBALL_TEAM_ID"] = "2939"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                str(UPDATER_DIR / "update_fixture.py"),
                "--dry-run",
                "--provider-sample",
                str(FIXTURES_DIR / "api_football_error_response.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 4, completed.stderr)
        self.assertIn("Provider error:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
