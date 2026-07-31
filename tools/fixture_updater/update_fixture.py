#!/usr/bin/env python3
"""Fetch and safely publish the next normalized team fixture."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from normalize_fixture import (
    NormalizationConfig,
    NormalizationError,
    normalize_fixture,
)
from provider_api_football import (
    ApiFootballClient,
    NoUpcomingFixture,
    ProviderConfigurationError,
    ProviderError,
    load_provider_sample,
    parse_provider_envelope,
    validate_team_id,
)
from validate_output import (
    OutputValidationError,
    replace_if_meaningfully_changed,
    serialize_validated_fixture,
    validate_normalized_fixture,
)

DEFAULT_REFRESH_AFTER_SECONDS = 21_600
DEFAULT_TARGET_TEAM_SLUG = "al-hilal"
DEFAULT_TARGET_TEAM_SHORT_NAME = "HIL"
DEFAULT_OUTPUT_PATH = "fixture.json"
DEFAULT_ALIASES_PATH = Path(__file__).with_name("team_aliases.json")

EXIT_CONFIGURATION = 2
EXIT_NO_FIXTURE = 3
EXIT_PROVIDER = 4
EXIT_VALIDATION = 5


@dataclass(frozen=True)
class UpdaterSettings:
    team_id: int
    output_path: Path
    refresh_after_seconds: int
    target_team_slug: str
    target_team_short_name: str
    aliases_path: Path


def _parse_positive_integer(
    value: str,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    text = value.strip()
    if not text.isascii() or not text.isdigit():
        raise ProviderConfigurationError(f"{name} must be an integer")
    parsed = int(text)
    if parsed < minimum or parsed > maximum:
        raise ProviderConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return parsed


def settings_from_environment(
    output_override: str | None = None,
) -> UpdaterSettings:
    team_id_text = os.environ.get("API_FOOTBALL_TEAM_ID", "")
    team_id = validate_team_id(team_id_text)
    refresh_after_seconds = _parse_positive_integer(
        os.environ.get(
            "FIXTURE_REFRESH_AFTER_SECONDS",
            str(DEFAULT_REFRESH_AFTER_SECONDS),
        ),
        "FIXTURE_REFRESH_AFTER_SECONDS",
        minimum=300,
        maximum=86_400,
    )
    target_team_slug = os.environ.get(
        "TARGET_TEAM_SLUG",
        DEFAULT_TARGET_TEAM_SLUG,
    ).strip()
    target_team_short_name = os.environ.get(
        "TARGET_TEAM_SHORT_NAME",
        DEFAULT_TARGET_TEAM_SHORT_NAME,
    ).strip()
    if not target_team_slug:
        raise ProviderConfigurationError("TARGET_TEAM_SLUG must not be empty")
    if not target_team_short_name:
        raise ProviderConfigurationError(
            "TARGET_TEAM_SHORT_NAME must not be empty"
        )
    output_path = Path(
        output_override
        or os.environ.get("FIXTURE_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    )
    aliases_path = Path(
        os.environ.get("TEAM_ALIASES_PATH", str(DEFAULT_ALIASES_PATH))
    )
    return UpdaterSettings(
        team_id=team_id,
        output_path=output_path,
        refresh_after_seconds=refresh_after_seconds,
        target_team_slug=target_team_slug,
        target_team_short_name=target_team_short_name,
        aliases_path=aliases_path,
    )


def load_aliases(path: str | Path) -> dict[str, str]:
    try:
        with Path(path).open("r", encoding="utf-8") as aliases_file:
            aliases = json.load(aliases_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderConfigurationError(
            f"could not read team aliases: {error}"
        ) from error
    if not isinstance(aliases, dict):
        raise ProviderConfigurationError(
            "team_aliases.json must contain an object"
        )
    for name, abbreviation in aliases.items():
        if not isinstance(name, str) or not name.strip():
            raise ProviderConfigurationError(
                "team alias names must be non-empty strings"
            )
        if not isinstance(abbreviation, str):
            raise ProviderConfigurationError(
                "team alias values must be strings"
            )
    return aliases


def normalize_provider_payload(
    payload: Any,
    settings: UpdaterSettings,
    aliases: Mapping[str, str],
    *,
    now: datetime,
) -> dict[str, Any]:
    provider_fixture = parse_provider_envelope(payload, settings.team_id)
    normalized = normalize_fixture(
        provider_fixture,
        NormalizationConfig(
            target_team_id=settings.team_id,
            target_team_slug=settings.target_team_slug,
            target_team_short_name=settings.target_team_short_name,
            refresh_after_seconds=settings.refresh_after_seconds,
        ),
        aliases,
        now=now,
    )
    return validate_normalized_fixture(normalized)


def _print_sanitized_summary(normalized: Mapping[str, Any]) -> None:
    fixture = normalized["fixture"]
    print(
        "Normalized fixture: "
        f"{fixture['id']} | "
        f"{fixture['home_team']['name']} vs "
        f"{fixture['away_team']['name']} | "
        f"{fixture['kickoff_utc']} | "
        f"{fixture['status']}"
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update the ESP32 fixture JSON from API-Football."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate without writing the output file",
    )
    parser.add_argument(
        "--output",
        help="override FIXTURE_OUTPUT_PATH",
    )
    parser.add_argument(
        "--provider-sample",
        help="read a saved provider response instead of using the network",
    )
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("fixture_updater")
    arguments = build_argument_parser().parse_args()

    try:
        settings = settings_from_environment(arguments.output)
        aliases = load_aliases(settings.aliases_path)
        now = datetime.now(timezone.utc)

        if arguments.provider_sample:
            payload = load_provider_sample(arguments.provider_sample)
            normalized = normalize_provider_payload(
                payload,
                settings,
                aliases,
                now=now,
            )
        else:
            api_key = os.environ.get("API_FOOTBALL_KEY", "")
            fetch_result = ApiFootballClient(
                api_key,
                logger=logger,
            ).fetch_next_fixture(settings.team_id)
            normalized = normalize_fixture(
                fetch_result.fixture,
                NormalizationConfig(
                    target_team_id=settings.team_id,
                    target_team_slug=settings.target_team_slug,
                    target_team_short_name=settings.target_team_short_name,
                    refresh_after_seconds=settings.refresh_after_seconds,
                ),
                aliases,
                now=now,
            )
            validate_normalized_fixture(normalized)
            if fetch_result.remaining_requests is not None:
                logger.info(
                    "Provider requests remaining: %d",
                    fetch_result.remaining_requests,
                )

        serialize_validated_fixture(normalized)
        _print_sanitized_summary(normalized)
        if arguments.dry_run:
            print("Dry run: output file was not modified")
            return 0

        changed = replace_if_meaningfully_changed(
            settings.output_path,
            normalized,
        )
        if changed:
            print(f"Updated normalized fixture: {settings.output_path}")
        else:
            print("No meaningful fixture change; output left untouched")
        return 0
    except NoUpcomingFixture as error:
        logger.warning("%s; existing output preserved", error)
        return EXIT_NO_FIXTURE
    except ProviderConfigurationError as error:
        logger.error("Configuration error: %s", error)
        return EXIT_CONFIGURATION
    except ProviderError as error:
        logger.error("Provider error: %s", error)
        return EXIT_PROVIDER
    except (NormalizationError, OutputValidationError) as error:
        logger.error("Validation error: %s", error)
        return EXIT_VALIDATION


if __name__ == "__main__":
    raise SystemExit(main())
