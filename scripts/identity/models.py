from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IdentityStatus(str, Enum):
    """Current trust level stored in ``players.identity_status``."""

    UNVERIFIED = "unverified"
    CONFLICTED = "conflicted"
    VERIFIED = "verified"


class ResolverStatus(str, Enum):
    """Outcome categories returned by the future Identity Resolver."""

    MATCH = "match"
    CREATED = "created"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class IdentityContext:
    """Identity information supplied by a parser for one player occurrence."""

    raw_name: str
    team_id: str
    season_id: int
    league_id: int
    jersey_number: str | None = None
    game_id: str | None = None
    provider: str = "livestats"
    external_player_id: str | None = None
    position: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerIdentity:
    """Normalized ARC player identity matching the Identity v2 player model."""

    player_id: str
    canonical_name: str
    identity_status: IdentityStatus = IdentityStatus.UNVERIFIED
    first_name: str | None = None
    last_name: str | None = None
    birth_date: str | None = None
    height_cm: int | None = None
    nationality: str | None = None
    position: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerCandidate:
    """Normalized identity candidate returned by any external provider."""

    provider: str
    external_player_id: str
    canonical_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    team_id: str | None = None
    season_id: int | None = None
    jersey_number: str | None = None
    birth_date: str | None = None
    height_cm: int | None = None
    nationality: str | None = None
    position: str | None = None


@dataclass(frozen=True, slots=True)
class ResolverResult:
    """Resolver outcome returned to the future parser integration point."""

    status: ResolverStatus
    player_id: str | None = None
    confidence: float = 0.0
    provider: str | None = None
    requires_manual_review: bool = False


@dataclass(frozen=True, slots=True)
class TeamInfo:
    """Normalized team descriptor returned by a provider's get_teams call."""

    team_id: int
    name: str
    code: str | None = None
    city: str | None = None
