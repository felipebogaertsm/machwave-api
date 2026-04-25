"""Simulations router."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.firebase import get_current_user
from app.config import get_settings
from app.repositories.motor import MotorRepository
from app.repositories.simulation import SimulationRepository
from app.schemas.simulation import (
    IBSimParamsSchema,
    SimulationJobConfig,
    SimulationResultsSchema,
    SimulationStatusRecord,
    SimulationSummary,
)

router = APIRouter()


async def _trigger_cloud_run_job(simulation_id: str, user_id: str) -> None:
    """Submit a Cloud Run Job execution with SIM_ID and USER_ID env overrides."""
    import asyncio

    settings = get_settings()

    def _submit() -> None:
        from google.cloud import run_v2

        client = run_v2.JobsClient()
        job_name = (
            f"projects/{settings.gcp_project_id}"
            f"/locations/{settings.cloud_run_job_region}"
            f"/jobs/{settings.cloud_run_job_name}"
        )
        request = run_v2.RunJobRequest(
            name=job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="SIM_ID", value=simulation_id),
                            run_v2.EnvVar(name="USER_ID", value=user_id),
                        ]
                    )
                ]
            ),
        )
        client.run_job(request=request)

    await asyncio.to_thread(_submit)


class CreateSimulationRequest(BaseModel):
    motor_id: str
    params: IBSimParamsSchema = IBSimParamsSchema()


class CreateSimulationResponse(BaseModel):
    simulation_id: str


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

    await _trigger_cloud_run_job(simulation_id, user_id)

    return CreateSimulationResponse(simulation_id=simulation_id)


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


@router.get("/{simulation_id}/results", response_model=SimulationResultsSchema)
async def get_simulation_results(
    simulation_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: SimulationRepository = Depends(SimulationRepository),
) -> SimulationResultsSchema:
    """Only available once status is 'done'."""
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
    return results


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
