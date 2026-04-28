"""Simulations router."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth.firebase import get_current_user
from app.auth.rbac import get_user_role, require_role
from app.credits.estimator import estimate_tokens
from app.repositories.account import AccountRepository, InsufficientBalanceError
from app.repositories.cost import CostRepository
from app.repositories.motor import MotorRepository
from app.repositories.simulation import SimulationRepository
from app.schemas.credits import CreditAccount, SimulationCostRecord, current_period_utc
from app.schemas.simulation import (
    IBSimParamsSchema,
    SimulationDetailsResponse,
    SimulationJobConfig,
    SimulationStatusRecord,
    SimulationSummary,
)
from app.worker.dispatch import trigger_simulation

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()

ACTIVE_STATUSES = ("pending", "running", "retried")


class CreateSimulationRequest(BaseModel):
    motor_id: str
    params: IBSimParamsSchema = IBSimParamsSchema()


class CreateSimulationResponse(BaseModel):
    simulation_id: str
    estimated_tokens: int


class EstimateSimulationResponse(BaseModel):
    """Dry-run estimate — does not create or charge anything.

    ``credits`` mirrors the caller's current credit account. ``can_afford``
    is True for unlimited accounts and for finite accounts where the
    estimate fits in the remaining budget.
    """

    estimated_tokens: int
    credits: CreditAccount
    can_afford: bool


class RerunAllResponse(BaseModel):
    triggered: int
    simulation_ids: list[str]


class ClearAllSimulationsResponse(BaseModel):
    deleted: int
    user_ids: list[str]


@router.post("", response_model=CreateSimulationResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_simulation(
    body: CreateSimulationRequest,
    user: dict[str, Any] = Depends(get_current_user),
    motor_repo: MotorRepository = Depends(MotorRepository),
    simulation_repo: SimulationRepository = Depends(SimulationRepository),
    cost_repo: CostRepository = Depends(CostRepository),
    account_repo: AccountRepository = Depends(AccountRepository),
) -> CreateSimulationResponse:
    user_id: str = user["uid"]

    motor = await motor_repo.get(user_id, body.motor_id)
    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{body.motor_id}' not found.",
        )

    simulation_id = str(uuid.uuid4())

    job_config = SimulationJobConfig(
        simulation_id=simulation_id,
        user_id=user_id,
        motor_id=body.motor_id,
        motor_config=motor.config,
        params=body.params,
    )

    estimated = estimate_tokens(job_config)
    period = current_period_utc()
    role = get_user_role(user)

    account = await account_repo.get_or_create(user_id, role=role)
    existing = await simulation_repo.list_summaries(user_id)

    active = next((s for s in existing if s.status in ACTIVE_STATUSES), None)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A simulation is already {active.status} (id: {active.simulation_id}). "
                "Wait for it to finish before submitting another."
            ),
        )

    if account.simulation_limit is not None and len(existing) >= account.simulation_limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Simulation limit reached ({account.simulation_limit}). "
                "Delete an existing simulation before submitting another."
            ),
        )
    try:
        tokens_charged = await account_repo.debit(user_id, estimated)
    except InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient tokens. Run estimated at {estimated}; "
                f"only {exc.remaining} remaining this period."
            ),
        ) from exc

    initial_status = SimulationStatusRecord(simulation_id=simulation_id, status="pending")
    cost_record = SimulationCostRecord(
        simulation_id=simulation_id,
        estimated_tokens=estimated,
        tokens_charged=tokens_charged,
        period=period,
    )

    await simulation_repo.save_config(user_id, simulation_id, job_config)
    await simulation_repo.save_status(user_id, simulation_id, initial_status)
    await cost_repo.save(user_id, simulation_id, cost_record)

    await trigger_simulation(simulation_id, user_id)

    return CreateSimulationResponse(simulation_id=simulation_id, estimated_tokens=estimated)


@router.post("/estimate", response_model=EstimateSimulationResponse)
async def estimate_simulation(
    body: CreateSimulationRequest,
    user: dict[str, Any] = Depends(get_current_user),
    motor_repo: MotorRepository = Depends(MotorRepository),
    account_repo: AccountRepository = Depends(AccountRepository),
) -> EstimateSimulationResponse:
    """Dry-run cost estimate. Does not create a simulation or debit tokens."""
    user_id: str = user["uid"]
    motor = await motor_repo.get(user_id, body.motor_id)
    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{body.motor_id}' not found.",
        )
    job_config = SimulationJobConfig(
        simulation_id="dry-run",
        user_id=user_id,
        motor_id=body.motor_id,
        motor_config=motor.config,
        params=body.params,
    )
    estimated = estimate_tokens(job_config)
    role = get_user_role(user)
    account = await account_repo.get_or_create(user_id, role=role)
    return EstimateSimulationResponse(
        estimated_tokens=estimated,
        credits=account.credits,
        can_afford=account.credits.can_afford(estimated),
    )


@admin_router.post(
    "/rerun-all",
    response_model=RerunAllResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_all_simulations(
    _: dict[str, Any] = Depends(require_role("admin")),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> RerunAllResponse:
    """Re-dispatch every simulation in the bucket. Admin-only."""
    pairs = await repo.list_all_simulation_pairs()
    triggered: list[str] = []
    for user_id, simulation_id in pairs:
        config = await repo.get_config(user_id, simulation_id)
        if config is None:
            logger.warning("Skipping %s/%s — no config.json", user_id, simulation_id)
            continue
        await repo.append_status_event(user_id, simulation_id, "retried")
        await trigger_simulation(simulation_id, user_id)
        triggered.append(simulation_id)
    return RerunAllResponse(triggered=len(triggered), simulation_ids=triggered)


@admin_router.delete("/clear-all", response_model=ClearAllSimulationsResponse)
async def admin_clear_all_simulations(
    user_id: str | None = Query(default=None),
    _: dict[str, Any] = Depends(require_role("admin")),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> ClearAllSimulationsResponse:
    """Delete every simulation record. Admin-only.

    With ``user_id``, scoped to that user; without it, every user's
    simulations are wiped.
    """
    target_users = (
        [user_id] if user_id is not None else await repo.list_all_users_with_simulations()
    )
    deleted = 0
    cleared: list[str] = []
    for uid in target_users:
        count = await repo.delete_all_for_user(uid)
        deleted += count
        cleared.append(uid)
    return ClearAllSimulationsResponse(deleted=deleted, user_ids=cleared)


@router.get("", response_model=list[SimulationSummary])
async def list_simulations(
    user: dict[str, Any] = Depends(get_current_user),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> list[SimulationSummary]:
    user_id: str = user["uid"]
    return await repo.list_summaries(user_id)


@router.get("/{simulation_id}/status", response_model=SimulationStatusRecord)
async def get_simulation_status(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> SimulationStatusRecord:
    user_id: str = user["uid"]
    record = await repo.get_status(user_id, simulation_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    return record


@router.get("/{simulation_id}/results", response_model=SimulationDetailsResponse)
async def get_simulation_results(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> SimulationDetailsResponse:
    """Only available once status is 'done'.

    Returns the full simulation payload — the time-series/scalar results plus
    the motor config and IB parameters that produced them — so the frontend
    can render charts and inputs side-by-side without a second round trip.
    """
    user_id: str = user["uid"]

    sim_status = await repo.get_status(user_id, simulation_id)
    if sim_status is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")

    if sim_status.status == "failed":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Simulation failed: {sim_status.error}",
        )
    if sim_status.status != "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Simulation is not yet complete (status: {sim_status.status}).",
        )

    results = await repo.get_results(user_id, simulation_id)
    if results is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Results not found. The simulation may still be processing.",
        )

    job_config = await repo.get_config(user_id, simulation_id)
    if job_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Simulation config not found.",
        )

    return SimulationDetailsResponse(
        simulation_id=simulation_id,
        motor_id=job_config.motor_id,
        motor_config=job_config.motor_config,
        params=job_config.params,
        results=results,
    )


@router.get("/{simulation_id}/cost", response_model=SimulationCostRecord)
async def get_simulation_cost(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    cost_repo: CostRepository = Depends(CostRepository),
    sim_repo: SimulationRepository = Depends(SimulationRepository),
) -> SimulationCostRecord:
    user_id: str = user["uid"]
    if await sim_repo.get_status(user_id, simulation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    record = await cost_repo.get(user_id, simulation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cost record not found.",
        )
    return record


@router.post(
    "/{simulation_id}/retry",
    response_model=CreateSimulationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_simulation(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    simulation_repo: SimulationRepository = Depends(SimulationRepository),
    cost_repo: CostRepository = Depends(CostRepository),
    account_repo: AccountRepository = Depends(AccountRepository),
) -> CreateSimulationResponse:
    """Re-dispatch a terminal simulation, appending a 'retried' status event.

    Charges a fresh estimate (failed runs were refunded; done runs already paid
    for the prior work). Blocked while any other simulation is active.
    """
    user_id: str = user["uid"]

    record = await simulation_repo.get_status(user_id, simulation_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    if record.status not in ("done", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Simulation is {record.status}; only terminal runs can be retried.",
        )

    job_config = await simulation_repo.get_config(user_id, simulation_id)
    if job_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Simulation config not found.",
        )

    summaries = await simulation_repo.list_summaries(user_id)
    other_active = next(
        (s for s in summaries if s.simulation_id != simulation_id and s.status in ACTIVE_STATUSES),
        None,
    )
    if other_active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A simulation is already {other_active.status} "
                f"(id: {other_active.simulation_id}). "
                "Wait for it to finish before retrying."
            ),
        )

    estimated = estimate_tokens(job_config)
    period = current_period_utc()
    role = get_user_role(user)
    await account_repo.get_or_create(user_id, role=role)

    try:
        tokens_charged = await account_repo.debit(user_id, estimated)
    except InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient tokens. Run estimated at {estimated}; "
                f"only {exc.remaining} remaining this period."
            ),
        ) from exc

    cost_record = SimulationCostRecord(
        simulation_id=simulation_id,
        estimated_tokens=estimated,
        tokens_charged=tokens_charged,
        period=period,
    )
    await cost_repo.save(user_id, simulation_id, cost_record)

    await simulation_repo.append_status_event(user_id, simulation_id, "retried")
    await trigger_simulation(simulation_id, user_id)

    return CreateSimulationResponse(simulation_id=simulation_id, estimated_tokens=estimated)


@router.delete("/{simulation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_simulation(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> None:
    """Deletes the simulation and its sibling blobs (status, results, cost).

    Does **not** refund tokens — once a run is submitted the user has paid
    for the work, regardless of whether they keep the result around. Refunds
    only happen via the worker's failure path (see ``app/worker/run.py``).
    """
    user_id: str = user["uid"]
    if await repo.get_status(user_id, simulation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    await repo.delete(user_id, simulation_id)
