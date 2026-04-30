"""Team-scoped simulations.

Mirrors :mod:`app.routers.simulations` with three differences:

1. Authorization gates on team role, not just authentication.
2. Tokens are debited from the team pool (:class:`TeamAccountRepository`).
3. The active-simulation block is per-team — running a personal simulation
   doesn't block a team simulation, and vice versa, because the queues
   correspond to independent credit pools.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.teams import require_team_role
from app.credits.estimator import estimate_tokens
from app.repositories.team import (
    TeamAccountRepository,
    TeamInsufficientBalanceError,
)
from app.repositories.team_resources import (
    TeamCostRepository,
    TeamMotorRepository,
    TeamSimulationRepository,
)
from app.schemas.credits import CreditAccount, SimulationCostRecord, current_period_utc
from app.schemas.simulation import (
    IBSimParamsSchema,
    SimulationDetailsResponse,
    SimulationJobConfig,
    SimulationStatusRecord,
    SimulationSummary,
)
from app.schemas.team import TeamMembership
from app.worker.dispatch import trigger_simulation

logger = logging.getLogger(__name__)

router = APIRouter()

# Same set used by the user-scoped router. Centralisation can wait until a
# third caller appears.
ACTIVE_STATUSES = ("pending", "running", "retried")


class CreateSimulationRequest(BaseModel):
    motor_id: str
    params: IBSimParamsSchema = IBSimParamsSchema()


class CreateSimulationResponse(BaseModel):
    simulation_id: str
    estimated_tokens: int


class EstimateSimulationResponse(BaseModel):
    estimated_tokens: int
    credits: CreditAccount
    can_afford: bool


@router.post("", response_model=CreateSimulationResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_team_simulation(
    team_id: str,
    body: CreateSimulationRequest,
    membership: TeamMembership = Depends(require_team_role("editor")),
    motor_repo: TeamMotorRepository = Depends(TeamMotorRepository),
    simulation_repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
    cost_repo: TeamCostRepository = Depends(TeamCostRepository),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> CreateSimulationResponse:
    motor = await motor_repo.get(team_id, body.motor_id)
    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{body.motor_id}' not found.",
        )

    simulation_id = str(uuid.uuid4())

    # Snapshot the requesting user as the job's ``user_id`` for audit purposes;
    # the run itself is team-scoped via the dispatch ``owner_kind``.
    job_config = SimulationJobConfig(
        simulation_id=simulation_id,
        user_id=membership.user_id,
        motor_id=body.motor_id,
        motor_config=motor.config,
        params=body.params,
    )

    estimated = estimate_tokens(job_config)
    period = current_period_utc()

    account = await account_repo.get_or_create(team_id)
    existing = await simulation_repo.list_summaries(team_id)

    active = next((s for s in existing if s.status in ACTIVE_STATUSES), None)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A team simulation is already {active.status} (id: {active.simulation_id}). "
                "Wait for it to finish before submitting another."
            ),
        )

    if account.simulation_limit is not None and len(existing) >= account.simulation_limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Team simulation limit reached ({account.simulation_limit}). "
                "Delete an existing simulation before submitting another."
            ),
        )

    try:
        tokens_charged = await account_repo.debit(team_id, estimated)
    except TeamInsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient team tokens. Run estimated at {estimated}; "
                f"only {exc.remaining} remaining this period."
            ),
        ) from exc

    initial_status = SimulationStatusRecord(simulation_id=simulation_id)
    cost_record = SimulationCostRecord(
        simulation_id=simulation_id,
        estimated_tokens=estimated,
        tokens_charged=tokens_charged,
        period=period,
        charged_to="team",
    )

    await simulation_repo.save_config(team_id, simulation_id, job_config)
    await simulation_repo.save_status(team_id, simulation_id, initial_status)
    await cost_repo.save(team_id, simulation_id, cost_record)

    await trigger_simulation(simulation_id, team_id, owner_kind="team")

    return CreateSimulationResponse(simulation_id=simulation_id, estimated_tokens=estimated)


@router.post("/estimate", response_model=EstimateSimulationResponse)
async def estimate_team_simulation(
    team_id: str,
    body: CreateSimulationRequest,
    membership: TeamMembership = Depends(require_team_role("viewer")),
    motor_repo: TeamMotorRepository = Depends(TeamMotorRepository),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> EstimateSimulationResponse:
    motor = await motor_repo.get(team_id, body.motor_id)
    if motor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motor '{body.motor_id}' not found.",
        )
    job_config = SimulationJobConfig(
        simulation_id="dry-run",
        user_id=membership.user_id,
        motor_id=body.motor_id,
        motor_config=motor.config,
        params=body.params,
    )
    estimated = estimate_tokens(job_config)
    account = await account_repo.get_or_create(team_id)
    return EstimateSimulationResponse(
        estimated_tokens=estimated,
        credits=account.credits,
        can_afford=account.credits.can_afford(estimated),
    )


@router.get("", response_model=list[SimulationSummary])
async def list_team_simulations(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
) -> list[SimulationSummary]:
    return await repo.list_summaries(team_id)


@router.get("/{simulation_id}/status", response_model=SimulationStatusRecord)
async def get_team_simulation_status(
    team_id: str,
    simulation_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
) -> SimulationStatusRecord:
    record = await repo.get_status(team_id, simulation_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    return record


@router.get("/{simulation_id}/results", response_model=SimulationDetailsResponse)
async def get_team_simulation_results(
    team_id: str,
    simulation_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
) -> SimulationDetailsResponse:
    sim_status = await repo.get_status(team_id, simulation_id)
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

    results = await repo.get_results(team_id, simulation_id)
    if results is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Results not found. The simulation may still be processing.",
        )

    job_config = await repo.get_config(team_id, simulation_id)
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
async def get_team_simulation_cost(
    team_id: str,
    simulation_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    cost_repo: TeamCostRepository = Depends(TeamCostRepository),
    sim_repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
) -> SimulationCostRecord:
    if await sim_repo.get_status(team_id, simulation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    record = await cost_repo.get(team_id, simulation_id)
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
async def retry_team_simulation(
    team_id: str,
    simulation_id: str,
    _: TeamMembership = Depends(require_team_role("editor")),
    simulation_repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
    cost_repo: TeamCostRepository = Depends(TeamCostRepository),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> CreateSimulationResponse:
    record = await simulation_repo.get_status(team_id, simulation_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    if record.status not in ("done", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Simulation is {record.status}; only terminal runs can be retried.",
        )

    job_config = await simulation_repo.get_config(team_id, simulation_id)
    if job_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Simulation config not found.",
        )

    summaries = await simulation_repo.list_summaries(team_id)
    other_active = next(
        (s for s in summaries if s.simulation_id != simulation_id and s.status in ACTIVE_STATUSES),
        None,
    )
    if other_active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A team simulation is already {other_active.status} "
                f"(id: {other_active.simulation_id}). "
                "Wait for it to finish before retrying."
            ),
        )

    estimated = estimate_tokens(job_config)
    period = current_period_utc()
    await account_repo.get_or_create(team_id)

    try:
        tokens_charged = await account_repo.debit(team_id, estimated)
    except TeamInsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient team tokens. Run estimated at {estimated}; "
                f"only {exc.remaining} remaining this period."
            ),
        ) from exc

    cost_record = SimulationCostRecord(
        simulation_id=simulation_id,
        estimated_tokens=estimated,
        tokens_charged=tokens_charged,
        period=period,
        charged_to="team",
    )
    await cost_repo.save(team_id, simulation_id, cost_record)

    await simulation_repo.append_status_event(team_id, simulation_id, "retried")
    await trigger_simulation(simulation_id, team_id, owner_kind="team")

    return CreateSimulationResponse(simulation_id=simulation_id, estimated_tokens=estimated)


@router.delete("/{simulation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team_simulation(
    team_id: str,
    simulation_id: str,
    _: TeamMembership = Depends(require_team_role("editor")),
    repo: TeamSimulationRepository = Depends(TeamSimulationRepository),
) -> None:
    if await repo.get_status(team_id, simulation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    await repo.delete(team_id, simulation_id)
