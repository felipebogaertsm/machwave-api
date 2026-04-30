"""Teams, memberships, invites, and team account/usage endpoints.

All authorization runs through :mod:`app.auth.teams`. Membership reads are
the source of truth — the user-side reverse-index pointer exists only to make
``GET /teams`` a prefix scan.

The invite flow does not depend on email — a token is returned to the
inviter, shared out-of-band, and accepted by any signed-in user via
``POST /invites/{token}/accept``.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.firebase import get_current_user
from app.auth.rbac import get_user_role, require_role
from app.auth.teams import require_team_role
from app.config import get_settings
from app.repositories.team import (
    TeamAccountRepository,
    TeamInviteRepository,
    TeamMembershipRepository,
    TeamRepository,
)
from app.repositories.team_resources import (
    TeamMotorRepository,
    TeamSimulationRepository,
)
from app.schemas.team import (
    ChangeMemberRoleRequest,
    CreateInviteRequest,
    CreateTeamRequest,
    InviteInspectResponse,
    Team,
    TeamAccountSnapshot,
    TeamInvite,
    TeamInviteResponse,
    TeamMembership,
    TeamMemberSummary,
    TeamSummary,
    TeamUsageSnapshot,
    UpdateTeamRequest,
)
from app.storage import gcs

router = APIRouter()
invites_router = APIRouter()
admin_router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _remaining(limit: int | None, used: int) -> int | None:
    if limit is None:
        return None
    return max(0, limit - used)


async def _ensure_team_membership_capacity(
    user: dict[str, Any], membership_repo: TeamMembershipRepository
) -> None:
    """Raise 409 if a non-admin user is already at the membership cap."""
    if get_user_role(user) == "admin":
        return
    limit = get_settings().default_team_membership_limit
    count = await membership_repo.count_for_user(user["uid"])
    if count >= limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Team-membership limit reached ({limit}). Leave a team before joining another."
            ),
        )


def _to_invite_response(invite: TeamInvite) -> TeamInviteResponse:
    return TeamInviteResponse(
        token=invite.token,
        team_id=invite.team_id,
        role=invite.role,
        invitee_email=invite.invitee_email,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        accepted_by=invite.accepted_by,
        accepted_at=invite.accepted_at,
        revoked=invite.revoked,
    )


# ---------------------------------------------------------------------------
# Teams CRUD
# ---------------------------------------------------------------------------


class CreateTeamResponse(BaseModel):
    team_id: str


@router.post("", response_model=CreateTeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: CreateTeamRequest,
    user: dict[str, Any] = Depends(get_current_user),
    team_repo: TeamRepository = Depends(TeamRepository),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> CreateTeamResponse:
    """Create a team. The caller becomes the team's first owner."""
    user_id: str = user["uid"]
    await _ensure_team_membership_capacity(user, membership_repo)

    team_id = str(uuid.uuid4())
    team = Team(
        team_id=team_id,
        name=body.name,
        description=body.description,
        created_by=user_id,
    )
    await team_repo.save(team)
    await membership_repo.save(
        TeamMembership(
            team_id=team_id,
            user_id=user_id,
            email=user.get("email"),
            role="owner",
        )
    )
    await account_repo.get_or_create(team_id)
    return CreateTeamResponse(team_id=team_id)


@router.get("", response_model=list[TeamSummary])
async def list_my_teams(
    user: dict[str, Any] = Depends(get_current_user),
    team_repo: TeamRepository = Depends(TeamRepository),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> list[TeamSummary]:
    user_id: str = user["uid"]
    memberships = await membership_repo.list_for_user(user_id)
    summaries: list[TeamSummary] = []
    for m in memberships:
        team = await team_repo.get(m.team_id)
        if team is None:
            continue
        summaries.append(
            TeamSummary(
                team_id=team.team_id,
                name=team.name,
                description=team.description,
                role=m.role,
                created_at=team.created_at,
                updated_at=team.updated_at,
            )
        )
    summaries.sort(key=lambda s: s.updated_at, reverse=True)
    return summaries


@router.get("/{team_id}", response_model=TeamSummary)
async def get_team(
    team_id: str,
    membership: TeamMembership = Depends(require_team_role("viewer")),
    team_repo: TeamRepository = Depends(TeamRepository),
) -> TeamSummary:
    team = await team_repo.get(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")
    return TeamSummary(
        team_id=team.team_id,
        name=team.name,
        description=team.description,
        role=membership.role,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


@router.patch("/{team_id}", response_model=TeamSummary)
async def update_team(
    team_id: str,
    body: UpdateTeamRequest,
    membership: TeamMembership = Depends(require_team_role("owner")),
    team_repo: TeamRepository = Depends(TeamRepository),
) -> TeamSummary:
    team = await team_repo.get(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided.",
        )
    updated = team.model_copy(update={**updates, "updated_at": datetime.now(UTC)})
    await team_repo.save(updated)
    return TeamSummary(
        team_id=updated.team_id,
        name=updated.name,
        description=updated.description,
        role=membership.role,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("owner")),
    team_repo: TeamRepository = Depends(TeamRepository),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> None:
    """Hard delete: cascades the entire ``teams/{tid}/`` prefix and clears
    every member's reverse-index pointer."""
    members = await membership_repo.list_for_team(team_id)
    await team_repo.delete(team_id)
    # Sweep the user-side reverse-index pointers; the canonical records are
    # already gone via the team-prefix cascade.
    for m in members:
        await gcs.delete_prefix(f"users/{m.user_id}/teams/{team_id}.json")


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{team_id}/members", response_model=list[TeamMemberSummary])
async def list_members(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> list[TeamMemberSummary]:
    members = await membership_repo.list_for_team(team_id)
    members.sort(key=lambda m: m.joined_at)
    return [
        TeamMemberSummary(user_id=m.user_id, email=m.email, role=m.role, joined_at=m.joined_at)
        for m in members
    ]


@router.patch("/{team_id}/members/{user_id}", response_model=TeamMemberSummary)
async def change_member_role(
    team_id: str,
    user_id: str,
    body: ChangeMemberRoleRequest,
    _: TeamMembership = Depends(require_team_role("owner")),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> TeamMemberSummary:
    target = await membership_repo.get(team_id, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    if target.role == "owner" and body.role != "owner":
        # Block demotion when this is the last remaining owner.
        owner_count = await membership_repo.count_owners(team_id)
        if owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot demote the last owner. Promote another member first.",
            )
    updated = target.model_copy(update={"role": body.role})
    await membership_repo.save(updated)
    return TeamMemberSummary(
        user_id=updated.user_id,
        email=updated.email,
        role=updated.role,
        joined_at=updated.joined_at,
    )


@router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    team_id: str,
    user_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> None:
    """Owner can remove anyone; any member can remove themselves (self-leave).

    Last-owner removal is blocked unconditionally — promote another member
    to owner first.
    """
    caller_id: str = user["uid"]
    caller_membership = await membership_repo.get(team_id, caller_id)
    if caller_membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")

    is_self = caller_id == user_id
    if not is_self and caller_membership.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners can remove other members.",
        )

    target = await membership_repo.get(team_id, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    if target.role == "owner":
        owner_count = await membership_repo.count_owners(team_id)
        if owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot remove the last owner. Promote another member first.",
            )
    await membership_repo.delete(team_id, user_id)


# ---------------------------------------------------------------------------
# Invites — team-scoped management + token-keyed accept namespace
# ---------------------------------------------------------------------------


@router.post(
    "/{team_id}/invites",
    response_model=TeamInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    team_id: str,
    body: CreateInviteRequest,
    membership: TeamMembership = Depends(require_team_role("owner")),
    invite_repo: TeamInviteRepository = Depends(TeamInviteRepository),
) -> TeamInviteResponse:
    token = secrets.token_urlsafe(32)
    invite = TeamInvite(
        token=token,
        team_id=team_id,
        role=body.role,
        invitee_email=body.invitee_email,
        created_by=membership.user_id,
    )
    await invite_repo.save(invite)
    return _to_invite_response(invite)


@router.get("/{team_id}/invites", response_model=list[TeamInviteResponse])
async def list_invites(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("owner")),
    invite_repo: TeamInviteRepository = Depends(TeamInviteRepository),
) -> list[TeamInviteResponse]:
    invites = await invite_repo.list_for_team(team_id)
    invites.sort(key=lambda i: i.created_at, reverse=True)
    return [_to_invite_response(i) for i in invites]


@router.delete("/{team_id}/invites/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    team_id: str,
    token: str,
    _: TeamMembership = Depends(require_team_role("owner")),
    invite_repo: TeamInviteRepository = Depends(TeamInviteRepository),
) -> None:
    invite = await invite_repo.get(team_id, token)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    await invite_repo.delete(team_id, token)


@invites_router.get("/{token}", response_model=InviteInspectResponse)
async def inspect_invite(
    token: str,
    _user: dict[str, Any] = Depends(get_current_user),
    invite_repo: TeamInviteRepository = Depends(TeamInviteRepository),
    team_repo: TeamRepository = Depends(TeamRepository),
) -> InviteInspectResponse:
    """Pre-accept inspection — what the invitee sees before clicking accept."""
    invite = await invite_repo.get_by_token(token)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    team = await team_repo.get(invite.team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")
    return InviteInspectResponse(
        team_id=team.team_id,
        team_name=team.name,
        role=invite.role,
        invitee_email=invite.invitee_email,
        expires_at=invite.expires_at,
        is_usable=invite.is_usable,
    )


@invites_router.post(
    "/{token}/accept",
    response_model=TeamMemberSummary,
    status_code=status.HTTP_201_CREATED,
)
async def accept_invite(
    token: str,
    user: dict[str, Any] = Depends(get_current_user),
    invite_repo: TeamInviteRepository = Depends(TeamInviteRepository),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> TeamMemberSummary:
    invite = await invite_repo.get_by_token(token)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    if invite.revoked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invite has been revoked.")
    if invite.is_consumed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Invite has already been used."
        )
    if invite.is_expired:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invite has expired.")

    user_id: str = user["uid"]
    existing = await membership_repo.get(invite.team_id, user_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already a member of this team.",
        )

    await _ensure_team_membership_capacity(user, membership_repo)

    membership = TeamMembership(
        team_id=invite.team_id,
        user_id=user_id,
        email=user.get("email"),
        role=invite.role,
    )
    await membership_repo.save(membership)

    invite.accepted_by = user_id
    invite.accepted_at = datetime.now(UTC)
    await invite_repo.save(invite)

    return TeamMemberSummary(
        user_id=membership.user_id,
        email=membership.email,
        role=membership.role,
        joined_at=membership.joined_at,
    )


# ---------------------------------------------------------------------------
# Account / usage
# ---------------------------------------------------------------------------


@router.get("/{team_id}/account", response_model=TeamAccountSnapshot)
async def get_team_account(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> TeamAccountSnapshot:
    account = await account_repo.get_or_create(team_id)
    return TeamAccountSnapshot(
        team_id=account.team_id,
        motor_limit=account.motor_limit,
        simulation_limit=account.simulation_limit,
        credits=account.credits,
    )


@router.get("/{team_id}/usage", response_model=TeamUsageSnapshot)
async def get_team_usage(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
    motor_repo: TeamMotorRepository = Depends(TeamMotorRepository),
    sim_repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
) -> TeamUsageSnapshot:
    account = await account_repo.get_or_create(team_id)
    motors = await motor_repo.list(team_id)
    sims = await sim_repo.list_summaries(team_id)
    return TeamUsageSnapshot(
        team_id=team_id,
        motor_count=len(motors),
        motor_limit=account.motor_limit,
        motors_remaining=_remaining(account.motor_limit, len(motors)),
        simulation_count=len(sims),
        simulation_limit=account.simulation_limit,
        simulations_remaining=_remaining(account.simulation_limit, len(sims)),
        credits=account.credits,
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class AdminUpdateTeamLimitsRequest(BaseModel):
    motor_limit: int | None = None
    simulation_limit: int | None = None
    monthly_token_limit: int | None = None


@admin_router.get("", response_model=list[TeamSummary])
async def admin_list_teams(
    _: dict[str, Any] = Depends(require_role("admin")),
    team_repo: TeamRepository = Depends(TeamRepository),
) -> list[TeamSummary]:
    teams = await team_repo.list_all()
    teams.sort(key=lambda t: t.updated_at, reverse=True)
    # ``role`` is meaningless to an admin viewing all teams; surface "owner" as
    # a stable placeholder so the wire model stays one shape.
    return [
        TeamSummary(
            team_id=t.team_id,
            name=t.name,
            description=t.description,
            role="owner",
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in teams
    ]


@admin_router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_team(
    team_id: str,
    _: dict[str, Any] = Depends(require_role("admin")),
    team_repo: TeamRepository = Depends(TeamRepository),
    membership_repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> None:
    members = await membership_repo.list_for_team(team_id)
    await team_repo.delete(team_id)
    for m in members:
        await gcs.delete_prefix(f"users/{m.user_id}/teams/{team_id}.json")


@admin_router.patch("/{team_id}/account/limits", response_model=TeamAccountSnapshot)
async def admin_update_team_limits(
    team_id: str,
    body: AdminUpdateTeamLimitsRequest,
    _: dict[str, Any] = Depends(require_role("admin")),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> TeamAccountSnapshot:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one limit must be provided.",
        )
    await account_repo.get_or_create(team_id)
    account = await account_repo.update_limits(team_id, updates)
    return TeamAccountSnapshot(
        team_id=account.team_id,
        motor_limit=account.motor_limit,
        simulation_limit=account.simulation_limit,
        credits=account.credits,
    )
