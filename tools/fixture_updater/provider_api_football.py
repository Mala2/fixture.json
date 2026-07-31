"""API-Football transport and provider-specific response parsing."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

API_BASE_URL = "https://v3.football.api-sports.io"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
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


def _redact_api_key(value: str, api_key: str) -> str:
    if not api_key:
        return value
    return value.replace(api_key, "***")


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
) -> ProviderTeam:
    team = _require_mapping(teams.get(side), f"response[0].teams.{side}")
    return ProviderTeam(
        provider_id=_require_positive_int(
            team,
            "id",
            f"response[0].teams.{side}.id",
        ),
        name=_require_string(
            team,
            "name",
            f"response[0].teams.{side}.name",
        ),
    )


def parse_provider_envelope(
    payload: Any,
    target_team_id: int,
) -> ProviderFixture:
    """Validate one API-Football fixture and return a provider-only model."""

    target_team_id = validate_team_id(target_team_id)
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
    if results == 0 and len(response) == 0:
        raise NoUpcomingFixture("provider returned no upcoming fixture")
    if results != 1 or len(response) != 1:
        raise ProviderResponseError(
            "response: expected exactly one upcoming fixture"
        )

    entry = _require_mapping(response[0], "response[0]")
    fixture = _require_mapping(entry.get("fixture"), "response[0].fixture")
    league = _require_mapping(entry.get("league"), "response[0].league")
    teams = _require_mapping(entry.get("teams"), "response[0].teams")
    venue = _require_mapping(
        fixture.get("venue"),
        "response[0].fixture.venue",
    )
    status = _require_mapping(
        fixture.get("status"),
        "response[0].fixture.status",
    )

    home_team = _parse_team(teams, "home")
    away_team = _parse_team(teams, "away")
    if target_team_id not in (
        home_team.provider_id,
        away_team.provider_id,
    ):
        raise ProviderResponseError(
            "configured target team is absent from the fixture"
        )

    return ProviderFixture(
        provider_fixture_id=_require_positive_int(
            fixture,
            "id",
            "response[0].fixture.id",
        ),
        kickoff=_require_string(
            fixture,
            "date",
            "response[0].fixture.date",
        ),
        competition_name=_require_string(
            league,
            "name",
            "response[0].league.name",
        ),
        venue_name=_require_string(
            venue,
            "name",
            "response[0].fixture.venue.name",
        ),
        status_code=_require_string(
            status,
            "short",
            "response[0].fixture.status.short",
        ).upper(),
        home_team=home_team,
        away_team=away_team,
    )


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

    def fetch_next_fixture(self, team_id: int) -> ProviderFetchResult:
        team_id = validate_team_id(team_id)
        query = urllib.parse.urlencode({"team": team_id, "next": 1})
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
                    fixture=parse_provider_envelope(payload, team_id),
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
