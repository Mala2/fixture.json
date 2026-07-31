"""Strict validation and atomic persistence for the ESP32 fixture schema."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
MINIMUM_REFRESH_SECONDS = 300
MAXIMUM_REFRESH_SECONDS = 86_400
MAXIMUM_OUTPUT_BYTES = 4_096
VALID_STATUSES = frozenset(
    {"scheduled", "live", "finished", "postponed", "cancelled"}
)
VALID_HOME_AWAY = frozenset({"home", "away"})

ROOT_KEYS = frozenset(
    {
        "schema_version",
        "generated_at",
        "refresh_after_seconds",
        "team",
        "fixture",
    }
)
TEAM_KEYS = frozenset({"id", "name", "short_name"})
FIXTURE_KEYS = frozenset(
    {
        "id",
        "competition",
        "kickoff_utc",
        "venue",
        "home_away",
        "home_team",
        "away_team",
        "status",
    }
)

TIMESTAMP_PATTERN = re.compile(
    r"^(?P<year>[0-9]{4})-[0-9]{2}-[0-9]{2}"
    r"T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class OutputValidationError(ValueError):
    pass


def _require_exact_keys(
    obj: Any,
    expected: frozenset[str],
    path: str,
) -> Mapping[str, Any]:
    if not isinstance(obj, dict):
        raise OutputValidationError(f"{path}: expected object")
    actual = frozenset(obj)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise OutputValidationError(
            f"{path}: missing keys {', '.join(missing)}"
        )
    if extra:
        raise OutputValidationError(
            f"{path}: unexpected keys {', '.join(extra)}"
        )
    return obj


def _require_integer(
    value: Any,
    path: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutputValidationError(f"{path}: expected integer")
    if value < minimum or value > maximum:
        raise OutputValidationError(
            f"{path}: value must be between {minimum} and {maximum}"
        )
    return value


def _require_string(
    value: Any,
    path: str,
    maximum_length: int,
    *,
    minimum_length: int = 1,
) -> str:
    if not isinstance(value, str):
        raise OutputValidationError(f"{path}: expected string")
    byte_length = len(value.encode("utf-8"))
    if byte_length < minimum_length or byte_length > maximum_length:
        raise OutputValidationError(
            f"{path}: UTF-8 byte length must be {minimum_length} through "
            f"{maximum_length}"
        )
    if not value.strip():
        raise OutputValidationError(f"{path}: value must contain visible text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise OutputValidationError(
            f"{path}: control characters are not allowed"
        )
    return value


def _require_timestamp(value: Any, path: str) -> str:
    text = _require_string(value, path, 20, minimum_length=20)
    match = TIMESTAMP_PATTERN.fullmatch(text)
    if match is None:
        raise OutputValidationError(
            f"{path}: expected YYYY-MM-DDTHH:MM:SSZ"
        )
    year = int(match.group("year"))
    if year < 2024 or year > 2100:
        raise OutputValidationError(f"{path}: year is outside firmware range")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise OutputValidationError(f"{path}: invalid timestamp") from error
    return text


def _validate_team(value: Any, path: str) -> dict[str, str]:
    team = _require_exact_keys(value, TEAM_KEYS, path)
    team_id = _require_string(team["id"], f"{path}.id", 32)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", team_id):
        raise OutputValidationError(
            f"{path}.id: expected lowercase ASCII slug"
        )
    name = _require_string(team["name"], f"{path}.name", 32)
    short_name = _require_string(
        team["short_name"],
        f"{path}.short_name",
        4,
        minimum_length=2,
    )
    if (
        not short_name.isascii()
        or not short_name.isalnum()
        or short_name != short_name.upper()
    ):
        raise OutputValidationError(
            f"{path}.short_name: expected uppercase ASCII alphanumeric"
        )
    return {"id": team_id, "name": name, "short_name": short_name}


def validate_normalized_fixture(value: Any) -> dict[str, Any]:
    root = _require_exact_keys(value, ROOT_KEYS, "$")
    schema_version = _require_integer(
        root["schema_version"],
        "schema_version",
        SCHEMA_VERSION,
        SCHEMA_VERSION,
    )
    generated_at = _require_timestamp(root["generated_at"], "generated_at")
    refresh_after_seconds = _require_integer(
        root["refresh_after_seconds"],
        "refresh_after_seconds",
        MINIMUM_REFRESH_SECONDS,
        MAXIMUM_REFRESH_SECONDS,
    )
    selected_team = _validate_team(root["team"], "team")

    fixture = _require_exact_keys(root["fixture"], FIXTURE_KEYS, "fixture")
    fixture_id = _require_string(fixture["id"], "fixture.id", 64)
    competition = _require_string(
        fixture["competition"],
        "fixture.competition",
        40,
    )
    kickoff_utc = _require_timestamp(
        fixture["kickoff_utc"],
        "fixture.kickoff_utc",
    )
    venue = _require_string(fixture["venue"], "fixture.venue", 48)
    home_away = _require_string(
        fixture["home_away"],
        "fixture.home_away",
        4,
    )
    if home_away not in VALID_HOME_AWAY:
        raise OutputValidationError(
            "fixture.home_away: expected home or away"
        )
    status = _require_string(fixture["status"], "fixture.status", 9)
    if status not in VALID_STATUSES:
        raise OutputValidationError("fixture.status: unsupported status")

    home_team = _validate_team(fixture["home_team"], "fixture.home_team")
    away_team = _validate_team(fixture["away_team"], "fixture.away_team")
    if home_team["id"] == away_team["id"]:
        raise OutputValidationError(
            "fixture.away_team.id: home and away IDs must differ"
        )
    designated_team = home_team if home_away == "home" else away_team
    if selected_team != designated_team:
        raise OutputValidationError(
            "team: selected team must match home_away designation"
        )

    return {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "refresh_after_seconds": refresh_after_seconds,
        "team": selected_team,
        "fixture": {
            "id": fixture_id,
            "competition": competition,
            "kickoff_utc": kickoff_utc,
            "venue": venue,
            "home_away": home_away,
            "home_team": home_team,
            "away_team": away_team,
            "status": status,
        },
    }


def serialize_validated_fixture(value: Any) -> bytes:
    validated = validate_normalized_fixture(value)
    text = json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAXIMUM_OUTPUT_BYTES:
        raise OutputValidationError("serialized output exceeds firmware limit")
    reparsed = json.loads(encoded.decode("utf-8"))
    validate_normalized_fixture(reparsed)
    return encoded


def meaningfully_equal(left: Any, right: Any) -> bool:
    try:
        left_validated = validate_normalized_fixture(left)
        right_validated = validate_normalized_fixture(right)
    except OutputValidationError:
        return False
    left_comparable = copy.deepcopy(left_validated)
    right_comparable = copy.deepcopy(right_validated)
    left_comparable.pop("generated_at", None)
    right_comparable.pop("generated_at", None)
    return left_comparable == right_comparable


def replace_if_meaningfully_changed(
    path: str | Path,
    value: Any,
) -> bool:
    destination = Path(path)
    encoded = serialize_validated_fixture(value)

    if destination.exists():
        try:
            with destination.open("r", encoding="utf-8") as current_file:
                current = json.load(current_file)
            if meaningfully_equal(current, value):
                return False
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return True


def load_and_validate(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as fixture_file:
            value = json.load(fixture_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OutputValidationError(f"could not read output: {error}") from error
    return validate_normalized_fixture(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a normalized ESP32 fixture JSON file."
    )
    parser.add_argument("path")
    arguments = parser.parse_args()
    try:
        load_and_validate(arguments.path)
    except OutputValidationError as error:
        print(f"Validation failed: {error}")
        return 1
    print(f"Validated normalized fixture: {arguments.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
