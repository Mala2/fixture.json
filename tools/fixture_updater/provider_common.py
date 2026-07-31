"""Provider-neutral fixture models and updater error categories."""

from __future__ import annotations

from dataclasses import dataclass


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
    provider_id: str | None
    name: str


@dataclass(frozen=True)
class ProviderFixture:
    provider_name: str
    provider_fixture_id: str
    kickoff: str
    competition_name: str
    venue_name: str
    normalized_status: str
    home_team: ProviderTeam
    away_team: ProviderTeam
