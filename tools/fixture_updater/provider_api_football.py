"""API-Football transport and provider-specific response parsing."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

API_BASE_URL = "https://v3.football.api-sports.io"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
DEFAULT_LOOKAHEAD_DAYS = 180
MINIMUM_LOOKAHEAD_DAYS = 7
MAXIMUM_LOOKAHEAD_DAYS = 365
MINIMUM_SEASON = 2000
PAST_FIXTURE_TOLERANCE = timedelta(minutes=5)
RATE_LIMIT_REMAINING_HEADERS = (
    "x-ratelimit-remaining",
    "x-ratelimit-requests-remaining",
)


class ProviderError(RuntimeError):
    """Base class for safe, user-facing provider failures."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderHttpError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass


class NoUpcomingFixture(ProviderResponseError):
    pass


@dataclass(frozen=True)
class ProviderTeam:
    provider_id: int
    name: str


@dataclass(frozen=True)
class ProviderFixture:
    provider_fixture_id: int
    kickoff: str
    competition_name: str
    venue_name: str
    status_code: str
    home_team: ProviderTeam
    away_team: ProviderTeam


@dataclass(frozen=True)
class ProviderFetchResult:
    fixture: ProviderFixture
    remaining_requests: int | None


@dataclass(frozen=True)
class _FixtureCandidate:
    fixture: ProviderFixture
    kickoff_utc: datetime
    normalized_status: str


def _redact_api_key(value: str, api_key: str) -> str:
    if not api_key:
        return value
    return value.replace(api_key, "***")


def _sanitize_log_text(value: str, api_key: str) -> str:
    return (
        _redact_api_key(value, api_key)
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def sanitize_provider_errors(errors: object, api_key: str) -> str:
    """Serialize provider errors while redacting every API-key occurrence."""

    def redact(value: object) -> object:
        if isinstance(value, str):
            return _redact_api_key(value, api_key)
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {
                _redact_api_key(str(key), api_key): redact(item)
                for key, item in value.items()
            }
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return "<unserializable provider error value>"

    sanitized = json.dumps(
        redact(errors),
        ensure_ascii=False,
        indent=2,
    )
    return _redact_api_key(sanitized, api_key)


def validate_team_id(value: str | int) -> int:
    if isinstance(value, bool):
        raise ProviderConfigurationError(
            "API_FOOTBALL_TEAM_ID must be a positive integer"
        )
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        raise ProviderConfigurationError(
            "API_FOOTBALL_TEAM_ID must be a positive integer"
        )
    team_id = int(text)
    if team_id <= 0:
        raise ProviderConfigurationError(
            "API_FOOTBALL_TEAM_ID must be a positive integer"
        )
    return team_id


def validate_lookahead_days(value: str | int) -> int:
    if isinstance(value, bool):
        raise ProviderConfigurationError(
            "FIXTURE_LOOKAHEAD_DAYS must be an integer"
        )
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        raise ProviderConfigurationError(
            "FIXTURE_LOOKAHEAD_DAYS must be an integer"
        )
    lookahead_days = int(text)
    if (
        lookahead_days < MINIMUM_LOOKAHEAD_DAYS
        or lookahead_days > MAXIMUM_LOOKAHEAD_DAYS
    ):
        raise ProviderConfigurationError(
            "FIXTURE_LOOKAHEAD_DAYS must be between "
            f"{MINIMUM_LOOKAHEAD_DAYS} and {MAXIMUM_LOOKAHEAD_DAYS}"
        )
    return lookahead_days


def _require_aware_utc(value: datetime, path: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderConfigurationError(
            f"{path} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def validate_season(value: str | int, *, now_utc: datetime) -> int:
    fixed_now_utc = _require_aware_utc(now_utc, "now_utc")
    if isinstance(value, bool):
        raise ProviderConfigurationError(
            "API_FOOTBALL_SEASON must contain exactly four digits"
        )
    text = str(value)
    if len(text) != 4 or not text.isascii() or not text.isdigit():
        raise ProviderConfigurationError(
            "API_FOOTBALL_SEASON must contain exactly four digits"
        )
    season = int(text)
    maximum_season = fixed_now_utc.year + 1
    if season < MINIMUM_SEASON or season > maximum_season:
        raise ProviderConfigurationError(
            "API_FOOTBALL_SEASON must be between "
            f"{MINIMUM_SEASON} and {maximum_season}"
        )
    return season


def _require_mapping(
    value: Any,
    path: str,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProviderResponseError(f"{path}: expected object")
    return value


def _require_positive_int(
    obj: Mapping[str, Any],
    key: str,
    path: str,
) -> int:
    if key not in obj:
        raise ProviderResponseError(f"{path}: required field missing")
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderResponseError(f"{path}: expected positive integer")
    return value


def _require_string(
    obj: Mapping[str, Any],
    key: str,
    path: str,
) -> str:
    if key not in obj:
        raise ProviderResponseError(f"{path}: required field missing")
    value = obj[key]
    if not isinstance(value, str):
        raise ProviderResponseError(f"{path}: expected string")
    value = value.strip()
    if not value:
        raise ProviderResponseError(f"{path}: required string is empty")
    return value


def _parse_team(
    teams: Mapping[str, Any],
    side: str,
    entry_path: str,
) -> ProviderTeam:
    team_path = f"{entry_path}.teams.{side}"
    team = _require_mapping(teams.get(side), team_path)
    return ProviderTeam(
        provider_id=_require_positive_int(
            team,
            "id",
            f"{team_path}.id",
        ),
        name=_require_string(
            team,
            "name",
            f"{team_path}.name",
        ),
    )


def _parse_kickoff(value: str, path: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ProviderResponseError(
            f"{path}: expected ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderResponseError(
            f"{path}: timestamp must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _parse_fixture_entry(
    value: Any,
    index: int,
    target_team_id: int,
) -> tuple[ProviderFixture, datetime]:
    entry_path = f"response[{index}]"
    entry = _require_mapping(value, entry_path)
    fixture_path = f"{entry_path}.fixture"
    fixture = _require_mapping(entry.get("fixture"), fixture_path)
    league = _require_mapping(
        entry.get("league"),
        f"{entry_path}.league",
    )
    teams = _require_mapping(
        entry.get("teams"),
        f"{entry_path}.teams",
    )
    venue = _require_mapping(
        fixture.get("venue"),
        f"{fixture_path}.venue",
    )
    status = _require_mapping(
        fixture.get("status"),
        f"{fixture_path}.status",
    )

    home_team = _parse_team(teams, "home", entry_path)
    away_team = _parse_team(teams, "away", entry_path)
    if target_team_id not in (
        home_team.provider_id,
        away_team.provider_id,
    ):
        raise ProviderResponseError(
            f"{entry_path}: configured target team is absent"
        )

    kickoff_text = _require_string(
        fixture,
        "date",
        f"{fixture_path}.date",
    )
    kickoff_utc = _parse_kickoff(
        kickoff_text,
        f"{fixture_path}.date",
    )
    return (
        ProviderFixture(
            provider_fixture_id=_require_positive_int(
                fixture,
                "id",
                f"{fixture_path}.id",
            ),
            kickoff=_canonical_utc(kickoff_utc),
            competition_name=_require_string(
                league,
                "name",
                f"{entry_path}.league.name",
            ),
            venue_name=_require_string(
                venue,
                "name",
                f"{fixture_path}.venue.name",
            ),
            status_code=_require_string(
                status,
                "short",
                f"{fixture_path}.status.short",
            ).upper(),
            home_team=home_team,
            away_team=away_team,
        ),
        kickoff_utc,
    )


def parse_provider_envelope(
    payload: Any,
    target_team_id: int,
    *,
    now_utc: datetime,
    logger: logging.Logger | None = None,
    requested_from: date | None = None,
    requested_to: date | None = None,
    season: str | int,
    api_key: str = "",
) -> ProviderFixture:
    """Validate, filter, and select one fixture from an API-Football page."""

    target_team_id = validate_team_id(target_team_id)
    fixed_now_utc = _require_aware_utc(now_utc, "now_utc")
    validated_season = validate_season(season, now_utc=fixed_now_utc)
    selection_logger = logger or logging.getLogger(__name__)
    root = _require_mapping(payload, "$")

    if "errors" not in root:
        raise ProviderResponseError("errors: required envelope field missing")
    errors = root["errors"]
    if not isinstance(errors, (list, dict)):
        raise ProviderResponseError("errors: expected array or object")
    if errors:
        raise ProviderResponseError("provider returned one or more errors")

    if "results" not in root:
        raise ProviderResponseError("results: required envelope field missing")
    results = root["results"]
    if isinstance(results, bool) or not isinstance(results, int) or results < 0:
        raise ProviderResponseError("results: expected non-negative integer")

    response = root.get("response")
    if not isinstance(response, list):
        raise ProviderResponseError("response: expected array")
    if results != len(response):
        raise ProviderResponseError(
            "results: does not match response array length"
        )

    selection_logger.info("Provider team ID: %d", target_team_id)
    selection_logger.info("Provider season: %d", validated_season)
    if requested_from is not None and requested_to is not None:
        selection_logger.info(
            "Requested fixture date range: %s to %s",
            requested_from.isoformat(),
            requested_to.isoformat(),
        )
    selection_logger.info("Total provider results: %d", results)

    excluded_status = 0
    excluded_date = 0
    excluded_invalid = 0
    candidates: list[_FixtureCandidate] = []

    from normalize_fixture import NormalizationError, map_status

    for index, entry in enumerate(response):
        try:
            provider_fixture, kickoff_utc = _parse_fixture_entry(
                entry,
                index,
                target_team_id,
            )
        except ProviderResponseError as error:
            excluded_invalid += 1
            selection_logger.warning(
                "Skipping response[%d] with invalid structure: %s",
                index,
                error,
            )
            continue

        try:
            normalized_status = map_status(provider_fixture.status_code)
        except NormalizationError:
            excluded_status += 1
            selection_logger.warning(
                "Skipping provider fixture ID %d with unknown status %s",
                provider_fixture.provider_fixture_id,
                _sanitize_log_text(
                    provider_fixture.status_code,
                    api_key,
                ),
            )
            continue

        if normalized_status in {"finished", "cancelled"}:
            excluded_status += 1
            continue
        if (
            normalized_status != "live"
            and kickoff_utc < fixed_now_utc - PAST_FIXTURE_TOLERANCE
        ):
            excluded_date += 1
            continue

        candidates.append(
            _FixtureCandidate(
                fixture=provider_fixture,
                kickoff_utc=kickoff_utc,
                normalized_status=normalized_status,
            )
        )

    candidates.sort(key=lambda candidate: candidate.kickoff_utc)
    selection_logger.info(
        "Excluded fixtures: status=%d, date=%d, invalid_structure=%d",
        excluded_status,
        excluded_date,
        excluded_invalid,
    )
    selection_logger.info("Usable fixture candidates: %d", len(candidates))

    if not candidates:
        raise NoUpcomingFixture(
            "provider returned no usable fixture in the requested date range"
        )

    live_candidates = [
        candidate
        for candidate in candidates
        if candidate.normalized_status == "live"
    ]
    selected = live_candidates[0] if live_candidates else candidates[0]
    selection_logger.info(
        "Selected provider fixture ID: %d",
        selected.fixture.provider_fixture_id,
    )
    selection_logger.info(
        "Selected kickoff UTC: %s",
        selected.fixture.kickoff,
    )
    selection_logger.info(
        "Selected teams: %s vs %s",
        _sanitize_log_text(selected.fixture.home_team.name, api_key),
        _sanitize_log_text(selected.fixture.away_team.name, api_key),
    )
    return selected.fixture


def load_provider_sample(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as sample:
            return json.load(sample)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderResponseError(
            f"could not parse provider sample: {error}"
        ) from error


class ApiFootballClient:
    """One-request API-Football client with bounded temporary retries."""

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
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderConfigurationError(
                "API_FOOTBALL_KEY is required"
            )
        if timeout_seconds <= 0:
            raise ProviderConfigurationError(
                "provider timeout must be positive"
            )
        if maximum_attempts < 1 or maximum_attempts > 3:
            raise ProviderConfigurationError(
                "maximum attempts must be between 1 and 3"
            )
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._maximum_attempts = maximum_attempts
        self._opener = opener
        self._sleeper = sleeper
        self._logger = logger or logging.getLogger(__name__)

    def fetch_next_fixture(
        self,
        team_id: int,
        *,
        season: str | int,
        now_utc: datetime,
        lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    ) -> ProviderFetchResult:
        team_id = validate_team_id(team_id)
        fixed_now_utc = _require_aware_utc(now_utc, "now_utc")
        validated_season = validate_season(
            season,
            now_utc=fixed_now_utc,
        )
        validated_lookahead_days = validate_lookahead_days(lookahead_days)
        from_date = fixed_now_utc.date()
        to_date = from_date + timedelta(days=validated_lookahead_days)
        query = urllib.parse.urlencode(
            {
                "team": team_id,
                "season": validated_season,
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "timezone": "UTC",
            }
        )
        url = f"{API_BASE_URL}/fixtures?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "x-apisports-key": self._api_key,
                "User-Agent": "soccer-fixture-updater/1",
            },
            method="GET",
        )
        self._logger.info("Provider request: /fixtures?%s", query)

        for attempt in range(1, self._maximum_attempts + 1):
            try:
                with self._opener(
                    request,
                    timeout=self._timeout_seconds,
                ) as response:
                    status = getattr(response, "status", None)
                    if status is None:
                        status = response.getcode()
                    self._log_http_diagnostics(status, response.headers)
                    if status != 200:
                        raise ProviderHttpError(
                            f"provider HTTP status {status}"
                        )
                    body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                        raise ProviderResponseError(
                            "provider response exceeds size limit"
                        )
                    remaining = self._remaining_requests(response.headers)
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise ProviderResponseError(
                        "provider returned invalid JSON"
                    ) from error
                self._log_payload_diagnostics(payload)
                return ProviderFetchResult(
                    fixture=parse_provider_envelope(
                        payload,
                        team_id,
                        now_utc=fixed_now_utc,
                        logger=self._logger,
                        requested_from=from_date,
                        requested_to=to_date,
                        season=validated_season,
                        api_key=self._api_key,
                    ),
                    remaining_requests=remaining,
                )
            except urllib.error.HTTPError as error:
                self._log_http_diagnostics(error.code, error.headers)
                if 500 <= error.code <= 599 and attempt < self._maximum_attempts:
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

    def _log_http_diagnostics(self, status: object, headers: Any) -> None:
        self._logger.info("API-Football HTTP status: %s", status)
        for header_name in RATE_LIMIT_REMAINING_HEADERS:
            raw_value = self._header_value(headers, header_name)
            if raw_value is not None:
                self._logger.info(
                    "API-Football rate limit remaining (%s): %s",
                    header_name,
                    _redact_api_key(str(raw_value), self._api_key),
                )

    def _log_payload_diagnostics(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            self._logger.info("API-Football results: <unavailable>")
            return

        results = payload.get("results", "<missing>")
        self._logger.info(
            "API-Football results: %s",
            sanitize_provider_errors(results, self._api_key),
        )
        if "errors" in payload and payload["errors"]:
            self._logger.error(
                "API-Football provider errors:\n%s",
                sanitize_provider_errors(
                    payload["errors"],
                    self._api_key,
                ),
            )

    @staticmethod
    def _remaining_requests(headers: Any) -> int | None:
        raw_value = ApiFootballClient._header_value(
            headers,
            "x-ratelimit-remaining",
        )
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        return int(text) if text.isascii() and text.isdigit() else None

    @staticmethod
    def _header_value(headers: Any, name: str) -> Any:
        if headers is None:
            return None
        raw_value = headers.get(name)
        if raw_value is not None:
            return raw_value
        try:
            for header_name, value in headers.items():
                if str(header_name).lower() == name:
                    return value
        except (AttributeError, TypeError):
            return None
        return None
