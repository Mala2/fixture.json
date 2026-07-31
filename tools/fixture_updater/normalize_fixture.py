"""Normalize provider fixtures into the firmware's stable schema."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from provider_common import ProviderFixture, ProviderResponseError

SCHEDULED_CODES = frozenset({"TBD", "NS"})
LIVE_CODES = frozenset(
    {"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"}
)
FINISHED_CODES = frozenset({"FT", "AET", "PEN"})
POSTPONED_CODES = frozenset({"PST"})
CANCELLED_CODES = frozenset({"CANC", "ABD", "AWD", "WO"})


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizationConfig:
    target_team_id: str | int
    target_team_slug: str
    target_team_short_name: str
    refresh_after_seconds: int
    target_team_name: str = "Al Hilal"


def map_status(status_code: str) -> str:
    code = status_code.strip().upper()
    if code in SCHEDULED_CODES:
        return "scheduled"
    if code in LIVE_CODES:
        return "live"
    if code in FINISHED_CODES:
        return "finished"
    if code in POSTPONED_CODES:
        return "postponed"
    if code in CANCELLED_CODES:
        return "cancelled"
    raise NormalizationError(f"unknown provider status code: {code}")


def slugify(
    name: str,
    *,
    fallback: str | None = None,
    maximum_length: int = 32,
) -> str:
    if not isinstance(name, str):
        raise NormalizationError("team name must be a string")
    ascii_name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not slug and fallback is not None:
        slug = re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-")
    if not slug:
        raise NormalizationError("team name cannot produce a stable ID")
    if len(slug) > maximum_length:
        slug = slug[:maximum_length].rstrip("-")
    if not slug:
        raise NormalizationError("team ID is empty after length limiting")
    return slug


def normalize_team_id(
    provider_team_id: str | int | None,
    *,
    provider_target_team_id: str | int,
    canonical_target_team_id: str,
    team_name: str,
) -> str:
    """Map provider identity into the provider-independent device identity."""

    raw_id = None if provider_team_id is None else str(provider_team_id)
    if raw_id == str(provider_target_team_id):
        return slugify(canonical_target_team_id)
    if (
        raw_id is not None
        and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", raw_id)
    ):
        return raw_id
    return slugify(
        team_name,
        fallback=f"team-{raw_id}" if raw_id is not None else None,
    )


def _validate_short_name(value: str, source: str) -> str:
    if not isinstance(value, str):
        raise NormalizationError(f"{source} abbreviation must be a string")
    abbreviation = "".join(
        character
        for character in value.upper()
        if character.isascii() and character.isalnum()
    )
    if abbreviation != value.strip().upper():
        raise NormalizationError(
            f"{source} abbreviation must be ASCII alphanumeric"
        )
    if len(abbreviation) < 2 or len(abbreviation) > 4:
        raise NormalizationError(
            f"{source} abbreviation must contain 2 to 4 characters"
        )
    return abbreviation


def fallback_abbreviation(team_name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", team_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .upper()
    )
    words = re.findall(r"[A-Z0-9]+", ascii_name)
    if len(words) >= 2:
        abbreviation = "".join(word[0] for word in words[:4])
        if len(abbreviation) >= 2:
            return abbreviation
    characters = "".join(words)
    if len(characters) >= 2:
        return characters[:3]
    if len(characters) == 1:
        return f"{characters}X"
    return "TM"


def resolve_short_name(
    team_name: str,
    *,
    aliases: Mapping[str, str],
    configured_target_short_name: str | None,
) -> str:
    if team_name in aliases:
        return _validate_short_name(aliases[team_name], "alias")
    if configured_target_short_name:
        return _validate_short_name(
            configured_target_short_name,
            "configured target-team",
        )
    return _validate_short_name(
        fallback_abbreviation(team_name),
        "generated",
    )


def canonical_utc_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError("kickoff timestamp is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise NormalizationError("kickoff timestamp is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NormalizationError("kickoff timestamp must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _generated_at(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise NormalizationError(
            "generated_at source time must be timezone-aware"
        )
    return (
        now.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def normalize_fixture(
    provider: ProviderFixture,
    config: NormalizationConfig,
    aliases: Mapping[str, str],
    *,
    now: datetime,
) -> dict[str, Any]:
    target_team_id = str(config.target_team_id)

    def provider_id(team: Any) -> str | None:
        value = team.provider_id
        return None if value is None else str(value)

    if provider_id(provider.home_team) == target_team_id:
        home_away = "home"
    elif provider_id(provider.away_team) == target_team_id:
        home_away = "away"
    else:
        raise ProviderResponseError(
            "configured target team is absent from the fixture"
        )

    target_slug = slugify(config.target_team_slug)

    def normalize_team(provider_team: Any) -> dict[str, str]:
        raw_provider_id = provider_id(provider_team)
        is_target = raw_provider_id == target_team_id
        if is_target:
            team_id = normalize_team_id(
                raw_provider_id,
                provider_target_team_id=target_team_id,
                canonical_target_team_id=target_slug,
                team_name=provider_team.name,
            )
            team_name = config.target_team_name.strip()
        else:
            team_name = provider_team.name.strip()
            team_id = normalize_team_id(
                raw_provider_id,
                provider_target_team_id=target_team_id,
                canonical_target_team_id=target_slug,
                team_name=team_name,
            )
        short_name = resolve_short_name(
            team_name,
            aliases=aliases,
            configured_target_short_name=(
                config.target_team_short_name if is_target else None
            ),
        )
        return {
            "id": team_id,
            "name": team_name,
            "short_name": short_name,
        }

    home_team = normalize_team(provider.home_team)
    away_team = normalize_team(provider.away_team)
    selected_team = home_team if home_away == "home" else away_team
    provider_name = getattr(provider, "provider_name", "api-football")
    normalized_status = getattr(provider, "normalized_status", None)
    if normalized_status is None:
        normalized_status = map_status(provider.status_code)

    return {
        "schema_version": 1,
        "generated_at": _generated_at(now),
        "refresh_after_seconds": config.refresh_after_seconds,
        "team": dict(selected_team),
        "fixture": {
            "id": f"{provider_name}-{provider.provider_fixture_id}",
            "competition": provider.competition_name.strip(),
            "kickoff_utc": canonical_utc_timestamp(provider.kickoff),
            "venue": provider.venue_name.strip(),
            "home_away": home_away,
            "home_team": home_team,
            "away_team": away_team,
            "status": normalized_status,
        },
    }
