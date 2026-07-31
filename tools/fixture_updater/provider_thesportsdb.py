"""TheSportsDB transport, validation, and next-event selection."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from provider_common import (
    NoUpcomingFixture,
    ProviderConfigurationError,
    ProviderFixture,
    ProviderHttpError,
    ProviderResponseError,
    ProviderTeam,
)

API_BASE_URL = "https://www.thesportsdb.com/api/v1/json"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
PAST_EVENT_TOLERANCE = timedelta(minutes=5)
CANONICAL_TARGET_NAME = "Al Hilal"
RECOGNIZED_TARGET_NAMES = frozenset(
    {
        "al-hilal",
        "al hilal",
        "al-hilal saudi fc",
        "al hilal sfc",
    }
)

SCHEDULED_STATUSES = frozenset({"NS", "NOT STARTED", "TBD"})
POSTPONED_STATUSES = frozenset({"POSTP", "POSTPONED"})
CANCELLED_STATUSES = frozenset({"CANC", "CANCELLED"})
FINISHED_STATUSES = frozenset({"FT", "MATCH FINISHED"})
LIVE_STATUSES = frozenset(
    {
        "LIVE",
        "IN PROGRESS",
        "1H",
        "HT",
        "2H",
        "ET",
        "P",
        "SUSP",
        "INT",
    }
)


@dataclass(frozen=True)
class SportsDbFetchResult:
    fixture: ProviderFixture


@dataclass(frozen=True)
class _EventCandidate:
    fixture: ProviderFixture
    kickoff_utc: datetime


def validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise ProviderConfigurationError("THESPORTSDB_API_KEY is required")
    api_key = value.strip()
    if (
        not api_key
        or len(api_key) > 64
        or re.fullmatch(r"[A-Za-z0-9_-]+", api_key) is None
    ):
        raise ProviderConfigurationError(
            "THESPORTSDB_API_KEY must be a non-empty URL-safe value"
        )
    return api_key


def validate_team_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ProviderConfigurationError(
            "THESPORTSDB_TEAM_ID must be a positive integer"
        )
    team_id = str(value).strip()
    if (
        not team_id
        or not team_id.isascii()
        or not team_id.isdigit()
        or int(team_id) <= 0
    ):
        raise ProviderConfigurationError(
            "THESPORTSDB_TEAM_ID must be a positive integer"
        )
    return team_id


def require_aware_utc(value: datetime, path: str = "now_utc") -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderConfigurationError(
            f"{path} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def load_provider_sample(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as sample:
            return json.load(sample)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderResponseError(
            f"could not parse provider sample: {error}"
        ) from error


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProviderResponseError(f"{path}: expected object")
    return value


def _require_string(
    obj: Mapping[str, Any],
    key: str,
    path: str,
) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError(f"{path}: required string is missing")
    return value.strip()


def _optional_string(obj: Mapping[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderResponseError(f"{key}: expected string or null")
    text = value.strip()
    return text or None


def _canonical_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _parse_date_event(value: str, path: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ProviderResponseError(
            f"{path}: expected YYYY-MM-DD"
        ) from error


def _parse_str_time(value: str, path: str) -> datetime_time:
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, pattern).time()
        except ValueError:
            continue
    raise ProviderResponseError(
        f"{path}: expected HH:MM or HH:MM:SS"
    )


def parse_provider_timestamp(
    timestamp: str,
    *,
    date_event: str | None,
    str_time: str | None,
    logger: logging.Logger,
) -> tuple[datetime, str]:
    """Parse TheSportsDB timestamp and validate its companion date/time."""

    text = timestamp.strip()
    if not text:
        raise ProviderResponseError(
            "strTimestamp: required string is missing"
        )
    if text.endswith("Z"):
        iso_text = f"{text[:-1]}+00:00"
    else:
        iso_text = text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError as error:
        raise ProviderResponseError(
            "strTimestamp: invalid ISO-8601 timestamp"
        ) from error

    source_datetime = parsed
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        logger.warning(
            "Provider supplied timezone-naive timestamp; treating it as UTC"
        )
        parsed = parsed.replace(tzinfo=timezone.utc)
    kickoff_utc = parsed.astimezone(timezone.utc)

    if date_event is not None:
        expected_date = _parse_date_event(date_event, "dateEvent")
        if source_datetime.date() != expected_date:
            raise ProviderResponseError(
                "strTimestamp conflicts with dateEvent"
            )
    if str_time is not None:
        expected_time = _parse_str_time(str_time, "strTime")
        source_time = source_datetime.time().replace(tzinfo=None)
        if (
            source_time.hour != expected_time.hour
            or source_time.minute != expected_time.minute
            or (
                len(str_time) == 8
                and source_time.second != expected_time.second
            )
        ):
            raise ProviderResponseError(
                "strTimestamp conflicts with strTime"
            )

    return kickoff_utc, _canonical_utc(kickoff_utc)


def map_thesportsdb_status(
    status: str | None,
    *,
    kickoff_utc: datetime,
    now_utc: datetime,
    logger: logging.Logger,
) -> str:
    if status is None or not status.strip():
        if kickoff_utc > now_utc:
            logger.warning(
                "Provider status is empty; inferred scheduled from future kickoff"
            )
            return "scheduled"
        raise ProviderResponseError(
            "strStatus is empty for a non-future event"
        )

    normalized = " ".join(status.strip().upper().split())
    if normalized in SCHEDULED_STATUSES:
        return "scheduled"
    if normalized in POSTPONED_STATUSES:
        return "postponed"
    if normalized in CANCELLED_STATUSES:
        return "cancelled"
    if normalized in FINISHED_STATUSES:
        return "finished"
    if normalized in LIVE_STATUSES:
        return "live"
    raise ProviderResponseError(
        f"unknown TheSportsDB status: {normalized}"
    )


def _is_recognized_target_name(value: str) -> bool:
    return " ".join(value.strip().lower().split()) in RECOGNIZED_TARGET_NAMES


def _parse_event(
    value: Any,
    index: int,
    target_team_id: str,
    *,
    now_utc: datetime,
    logger: logging.Logger,
) -> _EventCandidate:
    path = f"events[{index}]"
    event = _require_mapping(value, path)
    event_id = _require_string(event, "idEvent", f"{path}.idEvent")
    if not event_id.isascii() or not event_id.isdigit():
        raise ProviderResponseError(
            f"{path}.idEvent: expected numeric string"
        )

    home_name = _require_string(
        event,
        "strHomeTeam",
        f"{path}.strHomeTeam",
    )
    away_name = _require_string(
        event,
        "strAwayTeam",
        f"{path}.strAwayTeam",
    )
    home_id = _optional_string(event, "idHomeTeam")
    away_id = _optional_string(event, "idAwayTeam")
    target_is_home = home_id == target_team_id
    target_is_away = away_id == target_team_id
    if target_is_home == target_is_away:
        raise ProviderResponseError(
            f"{path}: configured target team is absent or ambiguous"
        )
    target_name = home_name if target_is_home else away_name
    if not _is_recognized_target_name(target_name):
        raise ProviderResponseError(
            f"{path}: target team name is not a recognized Al Hilal variant"
        )

    timestamp = _require_string(
        event,
        "strTimestamp",
        f"{path}.strTimestamp",
    )
    logger.info("Provider timestamp: %s", timestamp)
    kickoff_utc, canonical_kickoff = parse_provider_timestamp(
        timestamp,
        date_event=_optional_string(event, "dateEvent"),
        str_time=_optional_string(event, "strTime"),
        logger=logger,
    )
    logger.info("Normalized kickoff UTC: %s", canonical_kickoff)

    status = map_thesportsdb_status(
        _optional_string(event, "strStatus"),
        kickoff_utc=kickoff_utc,
        now_utc=now_utc,
        logger=logger,
    )
    venue = _optional_string(event, "strVenue")
    if venue is None:
        venue = "Venue TBC"
        logger.warning("Provider venue is missing; using Venue TBC")

    home_team = ProviderTeam(
        provider_id=home_id,
        name=CANONICAL_TARGET_NAME if target_is_home else home_name,
    )
    away_team = ProviderTeam(
        provider_id=away_id,
        name=CANONICAL_TARGET_NAME if target_is_away else away_name,
    )
    return _EventCandidate(
        fixture=ProviderFixture(
            provider_name="thesportsdb",
            provider_fixture_id=event_id,
            kickoff=canonical_kickoff,
            competition_name=_require_string(
                event,
                "strLeague",
                f"{path}.strLeague",
            ),
            venue_name=venue,
            normalized_status=status,
            home_team=home_team,
            away_team=away_team,
        ),
        kickoff_utc=kickoff_utc,
    )


def parse_events_envelope(
    payload: Any,
    target_team_id: str | int,
    *,
    now_utc: datetime,
    logger: logging.Logger | None = None,
) -> ProviderFixture:
    target_id = validate_team_id(target_team_id)
    fixed_now_utc = require_aware_utc(now_utc)
    provider_logger = logger or logging.getLogger(__name__)
    root = _require_mapping(payload, "$")
    events = root.get("events")
    if not isinstance(events, list):
        raise ProviderResponseError("events: expected array")
    provider_logger.info("Events returned: %d", len(events))
    if not events:
        raise NoUpcomingFixture(
            "TheSportsDB returned no upcoming home event"
        )

    candidates: list[_EventCandidate] = []
    failures: list[str] = []
    for index, event in enumerate(events):
        try:
            candidate = _parse_event(
                event,
                index,
                target_id,
                now_utc=fixed_now_utc,
                logger=provider_logger,
            )
        except ProviderResponseError as error:
            failures.append(str(error))
            provider_logger.warning(
                "Skipping invalid provider event at events[%d]: %s",
                index,
                error,
            )
            continue

        if candidate.fixture.normalized_status in {"finished", "cancelled"}:
            continue
        if (
            candidate.fixture.normalized_status != "live"
            and candidate.kickoff_utc <
                fixed_now_utc - PAST_EVENT_TOLERANCE
        ):
            continue
        candidates.append(candidate)

    if not candidates:
        if failures:
            raise ProviderResponseError(
                "TheSportsDB returned no valid usable event: "
                f"{failures[0]}"
            )
        raise NoUpcomingFixture(
            "TheSportsDB returned no usable upcoming home event"
        )

    candidates.sort(key=lambda candidate: candidate.kickoff_utc)
    live = [
        candidate
        for candidate in candidates
        if candidate.fixture.normalized_status == "live"
    ]
    selected = live[0] if live else candidates[0]
    provider_logger.info(
        "Selected event ID: %s",
        selected.fixture.provider_fixture_id,
    )
    provider_logger.info(
        "Selected fixture: %s vs %s",
        selected.fixture.home_team.name,
        selected.fixture.away_team.name,
    )
    return selected.fixture


def validate_team_lookup(
    payload: Any,
    target_team_id: str | int,
) -> None:
    target_id = validate_team_id(target_team_id)
    root = _require_mapping(payload, "$")
    teams = root.get("teams")
    if not isinstance(teams, list) or len(teams) != 1:
        raise ProviderResponseError(
            "teams: expected exactly one lookup result"
        )
    team = _require_mapping(teams[0], "teams[0]")
    if _require_string(team, "idTeam", "teams[0].idTeam") != target_id:
        raise ProviderResponseError("team lookup returned unexpected ID")
    if not _is_recognized_target_name(
        _require_string(team, "strTeam", "teams[0].strTeam")
    ):
        raise ProviderResponseError(
            "team lookup returned unrecognized Al Hilal name"
        )
    sport = _optional_string(team, "strSport")
    if sport is not None and sport.casefold() != "soccer":
        raise ProviderResponseError(
            "team lookup returned a non-soccer team"
        )


class TheSportsDbClient:
    """Bounded HTTPS client for TheSportsDB's next-event endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 10.0,
        maximum_attempts: int = 2,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._api_key = validate_api_key(api_key)
        if timeout_seconds <= 0:
            raise ProviderConfigurationError(
                "provider timeout must be positive"
            )
        if maximum_attempts < 1 or maximum_attempts > 3:
            raise ProviderConfigurationError(
                "maximum attempts must be between 1 and 3"
            )
        self._timeout_seconds = timeout_seconds
        self._maximum_attempts = maximum_attempts
        self._opener = opener
        self._sleeper = sleeper
        self._logger = logger or logging.getLogger(__name__)

    def fetch_next_event(
        self,
        team_id: str | int,
        *,
        now_utc: datetime,
    ) -> SportsDbFetchResult:
        target_id = validate_team_id(team_id)
        fixed_now_utc = require_aware_utc(now_utc)
        query = urllib.parse.urlencode({"id": target_id})
        encoded_key = urllib.parse.quote(self._api_key, safe="")
        url = f"{API_BASE_URL}/{encoded_key}/eventsnext.php?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "soccer-fixture-updater/1",
            },
            method="GET",
        )
        self._logger.info("Provider request: /eventsnext.php?%s", query)

        for attempt in range(1, self._maximum_attempts + 1):
            try:
                with self._opener(
                    request,
                    timeout=self._timeout_seconds,
                ) as response:
                    status = getattr(response, "status", None)
                    if status is None:
                        status = response.getcode()
                    self._logger.info("HTTP status: %s", status)
                    if status != 200:
                        raise ProviderHttpError(
                            f"provider HTTP status {status}"
                        )
                    body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                        raise ProviderResponseError(
                            "provider response exceeds size limit"
                        )
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise ProviderResponseError(
                        "provider returned invalid JSON"
                    ) from error
                return SportsDbFetchResult(
                    fixture=parse_events_envelope(
                        payload,
                        target_id,
                        now_utc=fixed_now_utc,
                        logger=self._logger,
                    )
                )
            except urllib.error.HTTPError as error:
                self._logger.info("HTTP status: %d", error.code)
                if (
                    500 <= error.code <= 599
                    and attempt < self._maximum_attempts
                ):
                    self._logger.warning(
                        "Temporary provider HTTP %d; retrying once",
                        error.code,
                    )
                    self._sleeper(float(attempt))
                    continue
                raise ProviderHttpError(
                    f"provider HTTP status {error.code}"
                ) from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < self._maximum_attempts:
                    self._logger.warning(
                        "Temporary provider network failure; retrying once"
                    )
                    self._sleeper(float(attempt))
                    continue
                raise ProviderHttpError(
                    "provider network request failed"
                ) from error

        raise ProviderHttpError("provider request attempts exhausted")
