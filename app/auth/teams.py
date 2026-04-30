"""Team-scoped authorization dependencies.

Mirrors :mod:`app.auth.rbac` — instead of gating on a Firebase custom claim,
``require_team_role`` resolves the caller's :class:`TeamMembership` from the
membership repo and checks its role rank. Routes type the dep as
``membership: TeamMembership = Depends(require_team_role("editor"))`` so the
membership is available without a second lookup.

Non-membership returns 404 (not 403) so we don't leak whether a team exists
to callers who can't see it — same idiom the user routers use for missing
resources.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from fastapi import Depends, HTTPException, status

from app.auth.firebase import get_current_user
from app.repositories.team import TeamMembershipRepository
from app.schemas.team import TeamMembership, TeamRole, role_rank


async def get_team_membership(
    team_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: TeamMembershipRepository = Depends(TeamMembershipRepository),
) -> TeamMembership:
    membership = await repo.get(team_id, user["uid"])
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")
    return membership


@lru_cache(maxsize=3)
def require_team_role(min_role: TeamRole) -> Callable[..., Awaitable[TeamMembership]]:
    """FastAPI dep factory — resolves the caller's membership and 403s when
    the membership rank is below ``min_role``."""
    required = role_rank(min_role)

    async def _dependency(
        membership: TeamMembership = Depends(get_team_membership),
    ) -> TeamMembership:
        if role_rank(membership.role) < required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires team role '{min_role}' or higher.",
            )
        return membership

    return _dependency
