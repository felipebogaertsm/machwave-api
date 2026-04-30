"""Schemas for team collaboration.

Mirrors the per-user shape in :mod:`app.schemas.credits` — ``TeamAccount`` is
structurally identical to ``UserAccount`` keyed on ``team_id``. Membership is
stored once under the team prefix and once as a reverse pointer under
``users/{uid}/teams/`` so listing a user's teams is a prefix scan rather than
a full bucket walk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.credits import CreditAccount

TeamRole = Literal["owner", "editor", "viewer"]

_ROLE_RANK: dict[TeamRole, int] = {"viewer": 0, "editor": 1, "owner": 2}

# Default invite TTL — kept short so dangling tokens can't be replayed forever.
_INVITE_TTL = timedelta(days=7)


def role_rank(role: TeamRole) -> int:
    """Numeric rank for ``role >= min_role`` comparisons."""
    return _ROLE_RANK[role]


# ---------------------------------------------------------------------------
# Team metadata
# ---------------------------------------------------------------------------


class Team(BaseModel):
    """Stored at ``teams/{team_id}/team.json``."""

    team_id: str
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


class TeamMembership(BaseModel):
    """Source of truth at ``teams/{team_id}/members/{user_id}.json``.

    A copy is also written to ``users/{user_id}/teams/{team_id}.json`` so the
    "list my teams" path is a prefix scan under the user — auth checks always
    read the canonical team-scoped blob.
    """

    team_id: str
    user_id: str
    email: str | None = None
    role: TeamRole
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class TeamInvite(BaseModel):
    """Stored at ``teams/{team_id}/invites/{token}.json``.

    A secondary index ``invites/{token}.json`` carries just ``{team_id}`` so
    the accept flow can resolve a token to its team in O(1).
    """

    model_config = ConfigDict(validate_assignment=True)

    token: str
    team_id: str
    role: TeamRole
    invitee_email: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + _INVITE_TTL)
    accepted_by: str | None = None
    accepted_at: datetime | None = None
    revoked: bool = False

    @field_validator("role")
    @classmethod
    def _no_owner_invites(cls, value: TeamRole) -> TeamRole:
        # Owner is granted only at team creation. Promotion to owner happens
        # via PATCH /teams/{tid}/members/{uid} — never via invite.
        if value == "owner":
            raise ValueError("Invites cannot grant the owner role.")
        return value

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.accepted_by is not None

    @property
    def is_usable(self) -> bool:
        return not (self.revoked or self.is_consumed or self.is_expired)


class TeamInviteIndex(BaseModel):
    """Tiny pointer doc at ``invites/{token}.json``."""

    team_id: str


# ---------------------------------------------------------------------------
# Team account / credits
# ---------------------------------------------------------------------------


class TeamAccount(BaseModel):
    """Per-team billing & quota record at ``teams/{team_id}/account.json``.

    Same shape as :class:`app.schemas.credits.UserAccount`, keyed on
    ``team_id``. Reuses :class:`app.schemas.credits.CreditAccount`.
    """

    team_id: str

    motor_limit: int | None
    simulation_limit: int | None
    credits: CreditAccount

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class TeamSummary(BaseModel):
    """Listing item for ``GET /teams``."""

    team_id: str
    name: str
    description: str | None
    role: TeamRole
    created_at: datetime
    updated_at: datetime


class TeamMemberSummary(BaseModel):
    user_id: str
    email: str | None
    role: TeamRole
    joined_at: datetime


class CreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class UpdateTeamRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class CreateInviteRequest(BaseModel):
    role: TeamRole
    invitee_email: str | None = None

    @field_validator("role")
    @classmethod
    def _no_owner_invites(cls, value: TeamRole) -> TeamRole:
        if value == "owner":
            raise ValueError("Invites cannot grant the owner role.")
        return value


class TeamInviteResponse(BaseModel):
    token: str
    team_id: str
    role: TeamRole
    invitee_email: str | None
    created_at: datetime
    expires_at: datetime
    accepted_by: str | None
    accepted_at: datetime | None
    revoked: bool


class InviteInspectResponse(BaseModel):
    """Pre-accept inspection — what the invitee sees before clicking accept."""

    team_id: str
    team_name: str
    role: TeamRole
    invitee_email: str | None
    expires_at: datetime
    is_usable: bool


class ChangeMemberRoleRequest(BaseModel):
    role: TeamRole


class TeamAccountSnapshot(BaseModel):
    """Returned by ``GET /teams/{tid}/account``."""

    team_id: str
    motor_limit: int | None
    simulation_limit: int | None
    credits: CreditAccount
    motor_count: int | None = None
    simulation_count: int | None = None


class TeamUsageSnapshot(BaseModel):
    """Returned by ``GET /teams/{tid}/usage``."""

    team_id: str
    motor_count: int
    motor_limit: int | None
    motors_remaining: int | None

    simulation_count: int
    simulation_limit: int | None
    simulations_remaining: int | None

    credits: CreditAccount
