"""Per-user account & usage endpoints, plus admin-only limit overrides."""

from __future__ import annotations

import asyncio
from typing import Any

import firebase_admin
from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import auth as firebase_auth
from pydantic import BaseModel, Field

from app.auth.firebase import get_current_user, get_firebase_app
from app.auth.rbac import get_user_role, require_role
from app.repositories.account import AccountRepository
from app.repositories.motor import MotorRepository
from app.repositories.simulation import SimulationRepository
from app.schemas.credits import AccountSnapshot, UsageSnapshot

router = APIRouter()
admin_router = APIRouter()


async def _target_role(user_id: str, app: firebase_admin.App) -> str:
    """Look up a user's role from Firebase custom claims.

    Returns ``"member"`` on any failure (missing user, claim absent, transport
    flake). Used by admin endpoints to decide what role to apply when lazily
    creating an account for a target user.
    """
    try:
        record = await asyncio.to_thread(firebase_auth.get_user, user_id, app=app)
    except firebase_auth.UserNotFoundError:
        return "member"
    except Exception:
        return "member"
    claims = record.custom_claims or {}
    return "admin" if claims.get("role") == "admin" else "member"


def _to_snapshot(
    account: Any,
    *,
    is_admin: bool,
    motor_count: int | None = None,
    simulation_count: int | None = None,
) -> AccountSnapshot:
    """Pass account state through verbatim — ``credits`` is the same nested
    shape the storage layer holds, ``None`` caps already mean unlimited."""
    return AccountSnapshot(
        user_id=account.user_id,
        motor_limit=account.motor_limit,
        simulation_limit=account.simulation_limit,
        credits=account.credits,
        is_admin=is_admin,
        motor_count=motor_count,
        simulation_count=simulation_count,
    )


def _remaining(limit: int | None, used: int) -> int | None:
    """Return how much of a cap is left, or None if unlimited."""
    if limit is None:
        return None
    return max(0, limit - used)


@router.get("/me/account", response_model=AccountSnapshot)
async def get_my_account(
    user: dict[str, Any] = Depends(get_current_user),
    account_repo: AccountRepository = Depends(AccountRepository),
) -> AccountSnapshot:
    """Full account state — caps, grant, balance, period."""
    user_id: str = user["uid"]
    role = get_user_role(user)
    account = await account_repo.get_or_create(user_id, role=role)
    return _to_snapshot(account, is_admin=role == "admin")


@router.get("/me/usage", response_model=UsageSnapshot)
async def get_my_usage(
    user: dict[str, Any] = Depends(get_current_user),
    motor_repo: MotorRepository = Depends(MotorRepository),
    sim_repo: SimulationRepository = Depends(SimulationRepository),
    account_repo: AccountRepository = Depends(AccountRepository),
) -> UsageSnapshot:
    """Snapshot of the caller's quotas — what the frontend renders as 'X / Y used'."""
    user_id: str = user["uid"]
    role = get_user_role(user)

    account = await account_repo.get_or_create(user_id, role=role)
    motors = await motor_repo.list(user_id)
    sims = await sim_repo.list_summaries(user_id)

    motor_count = len(motors)
    sim_count = len(sims)

    return UsageSnapshot(
        motor_count=motor_count,
        motor_limit=account.motor_limit,
        motors_remaining=_remaining(account.motor_limit, motor_count),
        simulation_count=sim_count,
        simulation_limit=account.simulation_limit,
        simulations_remaining=_remaining(account.simulation_limit, sim_count),
        credits=account.credits,
        is_admin=role == "admin",
    )


# ---------------------------------------------------------------------------
# Admin: per-user limit overrides.
# ---------------------------------------------------------------------------


class UpdateLimitsRequest(BaseModel):
    motor_limit: int | None = Field(default=None, ge=0)
    simulation_limit: int | None = Field(default=None, ge=0)
    monthly_token_limit: int | None = Field(default=None, ge=0)


async def _admin_snapshot(
    account: Any,
    motor_repo: MotorRepository,
    sim_repo: SimulationRepository,
    role: str,
) -> AccountSnapshot:
    motors = await motor_repo.list(account.user_id)
    sims = await sim_repo.list_summaries(account.user_id)
    return _to_snapshot(
        account,
        is_admin=role == "admin",
        motor_count=len(motors),
        simulation_count=len(sims),
    )


@admin_router.get("/{user_id}/account", response_model=AccountSnapshot)
async def admin_get_account(
    user_id: str,
    _: dict[str, Any] = Depends(require_role("admin")),
    account_repo: AccountRepository = Depends(AccountRepository),
    motor_repo: MotorRepository = Depends(MotorRepository),
    sim_repo: SimulationRepository = Depends(SimulationRepository),
    fb_app: firebase_admin.App = Depends(get_firebase_app),
) -> AccountSnapshot:
    role = await _target_role(user_id, fb_app)
    account = await account_repo.get_or_create(user_id, role=role)
    return await _admin_snapshot(account, motor_repo, sim_repo, role)


@admin_router.put("/{user_id}/limits", response_model=AccountSnapshot)
async def admin_update_limits(
    user_id: str,
    body: UpdateLimitsRequest,
    _: dict[str, Any] = Depends(require_role("admin")),
    account_repo: AccountRepository = Depends(AccountRepository),
    motor_repo: MotorRepository = Depends(MotorRepository),
    sim_repo: SimulationRepository = Depends(SimulationRepository),
    fb_app: firebase_admin.App = Depends(get_firebase_app),
) -> AccountSnapshot:
    """Override per-user caps. Send ``null`` to mark a field unlimited; omit
    a field from the body to leave it unchanged."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one limit must be provided.",
        )
    role = await _target_role(user_id, fb_app)
    # Make sure the account exists with the right role-defaults before patching.
    await account_repo.get_or_create(user_id, role=role)
    account = await account_repo.update_limits(user_id, updates)
    return await _admin_snapshot(account, motor_repo, sim_repo, role)
