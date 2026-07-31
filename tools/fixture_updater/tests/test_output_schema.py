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
from unittest import mock

UPDATER_DIR = Path(__file__).resolve().parents[1]
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

from normalize_fixture import NormalizationConfig, normalize_fixture  # noqa: E402
from provider_common import ProviderConfigurationError  # noqa: E402
from provider_thesportsdb import parse_events_envelope  # noqa: E402
from update_fixture import settings_from_environment  # noqa: E402
from validate_output import (  # noqa: E402
    OutputValidationError,
    load_and_validate,
    replace_if_meaningfully_changed,
    serialize_validated_fixture,
    validate_normalized_fixture,
)

FIXTURES_DIR = Path(__file__).with_name("fixtures")
FIXED_NOW = datetime(2026, 7, 31, 0, 48, tzinfo=timezone.utc)
BASE_ENVIRONMENT = {
    "FIXTURE_PROVIDER": "thesportsdb",
    "THESPORTSDB_API_KEY": "123",
    "THESPORTSDB_TEAM_ID": "136013",
}


def load_payload(name: str) -> dict:
    return json.loads(
        (FIXTURES_DIR / name).read_text(encoding="utf-8")
    )


def make_normalized(now: datetime = FIXED_NOW) -> dict:
    provider = parse_events_envelope(
        load_payload("thesportsdb_next_event.json"),
        "136013",
        now_utc=FIXED_NOW,
    )
    return normalize_fixture(
        provider,
        NormalizationConfig(
            target_team_id="136013",
            target_team_slug="al-hilal",
            target_team_short_name="HIL",
            target_team_name="Al Hilal",
            refresh_after_seconds=21600,
        ),
        {
            "Al-Hilal": "HIL",
            "Al Hilal": "HIL",
            "Al-Ahli Doha": "AHD",
        },
        now=now,
    )


def subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(BASE_ENVIRONMENT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class OutputSchemaTests(unittest.TestCase):
    def test_normalized_output_matches_existing_firmware_schema(self) -> None:
        normalized = validate_normalized_fixture(make_normalized())
        self.assertEqual(normalized["schema_version"], 1)
        self.assertEqual(
            normalized["fixture"]["id"],
            "thesportsdb-2549422",
        )
        self.assertEqual(
            normalized["fixture"]["kickoff_utc"],
            "2026-08-03T15:00:00Z",
        )
        self.assertEqual(normalized["team"]["id"], "al-hilal")
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
            replacement["fixture"]["id"] = "thesportsdb-7654321"
            replacement["fixture"]["kickoff_utc"] = "2026-08-23T03:00:00Z"
            self.assertTrue(
                replace_if_meaningfully_changed(output, replacement)
            )
            self.assertEqual(
                load_and_validate(output)["fixture"]["id"],
                "thesportsdb-7654321",
            )

    def test_sample_dry_run_does_not_write_output_or_log_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.json"
            environment = subprocess_environment()
            environment["THESPORTSDB_API_KEY"] = "do-not-log-this"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(UPDATER_DIR / "update_fixture.py"),
                    "--dry-run",
                    "--provider-sample",
                    str(FIXTURES_DIR / "thesportsdb_next_event.json"),
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
            self.assertIn(
                "Normalized schema validation succeeded",
                completed.stderr,
            )
            self.assertNotIn("do-not-log-this", completed.stderr)
            self.assertFalse(output.exists())

    def test_provider_validation_error_preserves_exit_code_four(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(UPDATER_DIR / "update_fixture.py"),
                "--dry-run",
                "--provider-sample",
                str(FIXTURES_DIR / "thesportsdb_unknown_status.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=subprocess_environment(),
        )
        self.assertEqual(completed.returncode, 4, completed.stderr)
        self.assertIn("Provider error:", completed.stderr)

    def test_thesportsdb_is_default_provider(self) -> None:
        environment = dict(BASE_ENVIRONMENT)
        environment.pop("FIXTURE_PROVIDER")
        with mock.patch.dict(os.environ, environment, clear=True):
            settings = settings_from_environment(now_utc=FIXED_NOW)
        self.assertEqual(settings.provider, "thesportsdb")
        self.assertEqual(settings.team_id, "136013")

    def test_missing_provider_configuration_fails(self) -> None:
        for missing_name in ("THESPORTSDB_API_KEY", "THESPORTSDB_TEAM_ID"):
            with self.subTest(missing_name=missing_name):
                environment = dict(BASE_ENVIRONMENT)
                environment.pop(missing_name)
                with mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(
                        ProviderConfigurationError,
                        missing_name,
                    ):
                        settings_from_environment(now_utc=FIXED_NOW)

    def test_unsupported_provider_is_rejected(self) -> None:
        environment = dict(BASE_ENVIRONMENT)
        environment["FIXTURE_PROVIDER"] = "api-football"
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(
                ProviderConfigurationError,
                "must be thesportsdb",
            ):
                settings_from_environment(now_utc=FIXED_NOW)

    def test_empty_events_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.json"
            output.write_bytes(serialize_validated_fixture(make_normalized()))
            original = output.read_bytes()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(UPDATER_DIR / "update_fixture.py"),
                    "--provider-sample",
                    str(FIXTURES_DIR / "thesportsdb_empty_events.json"),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=subprocess_environment(),
            )
            self.assertEqual(completed.returncode, 3, completed.stderr)
            self.assertIn("Events returned: 0", completed.stderr)
            self.assertIn("TheSportsDB team ID: 136013", completed.stderr)
            self.assertIn("existing output preserved", completed.stderr)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
