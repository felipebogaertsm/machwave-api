"""Simulations router."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.firebase import get_current_user
from app.auth.rbac import require_role
from app.repositories.motor import MotorRepository
from app.repositories.simulation import SimulationRepository
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


class CreateSimulationRequest(BaseModel):
    motor_id: str
    params: IBSimParamsSchema = IBSimParamsSchema()


class CreateSimulationResponse(BaseModel):
    simulation_id: str


class RerunAllResponse(BaseModel):
    triggered: int
    simulation_ids: list[str]


@router.post("", response_model=CreateSimulationResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_simulation(
    body: CreateSimulationRequest,
    user: dict[str, Any] = Depends(get_current_user),
    motor_repo: MotorRepository = Depends(MotorRepository),
    simulation_repo: SimulationRepository = Depends(SimulationRepository),
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
    initial_status = SimulationStatusRecord(simulation_id=simulation_id, status="pending")

    await simulation_repo.save_config(user_id, simulation_id, job_config)
    await simulation_repo.save_status(user_id, simulation_id, initial_status)

    await trigger_simulation(simulation_id, user_id)

    return CreateSimulationResponse(simulation_id=simulation_id)


@router.post(
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
        await repo.save_status(
            user_id,
            simulation_id,
            SimulationStatusRecord(simulation_id=simulation_id, status="pending"),
        )
        await trigger_simulation(simulation_id, user_id)
        triggered.append(simulation_id)
    return RerunAllResponse(triggered=len(triggered), simulation_ids=triggered)


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


@router.delete("/{simulation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_simulation(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> None:
    user_id: str = user["uid"]
    if await repo.get_status(user_id, simulation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Simulation not found.")
    await repo.delete(user_id, simulation_id)
