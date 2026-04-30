"""Team-scoped motor CRUD.

Mirrors :mod:`app.routers.motors` but every endpoint is gated by
:func:`app.auth.teams.require_team_role`. Storage caps come from the team
account, not the caller's personal account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.teams import require_team_role
from app.repositories.team import TeamAccountRepository
from app.repositories.team_resources import TeamMotorRepository
from app.schemas.motor import MotorConfigSchema, MotorRecord, MotorSummary
from app.schemas.team import TeamMembership

router = APIRouter()


class CreateMotorRequest(BaseModel):
    name: str
    config: MotorConfigSchema


class CreateMotorResponse(BaseModel):
    motor_id: str


class UpdateMotorRequest(BaseModel):
    name: str | None = None
    config: MotorConfigSchema | None = None


@router.post("", response_model=CreateMotorResponse, status_code=status.HTTP_201_CREATED)
async def create_team_motor(
    team_id: str,
    body: CreateMotorRequest,
    _: TeamMembership = Depends(require_team_role("editor")),
    repo: TeamMotorRepository = Depends(TeamMotorRepository),
    account_repo: TeamAccountRepository = Depends(TeamAccountRepository),
) -> CreateMotorResponse:
    account = await account_repo.get_or_create(team_id)
    if account.motor_limit is not None:
        existing = await repo.list(team_id)
        if len(existing) >= account.motor_limit:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Team motor limit reached ({account.motor_limit}). "
                    "Delete an existing motor before creating another."
                ),
            )
    motor_id = str(uuid.uuid4())
    record = MotorRecord(motor_id=motor_id, name=body.name, config=body.config)
    await repo.save(team_id, motor_id, record)
    return CreateMotorResponse(motor_id=motor_id)


@router.get("", response_model=list[MotorSummary])
async def list_team_motors(
    team_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    repo: TeamMotorRepository = Depends(TeamMotorRepository),
) -> list[MotorSummary]:
    records = await repo.list(team_id)
    summaries = [
        MotorSummary(
            motor_id=r.motor_id,
            name=r.name,
            motor_type=r.config.motor_type,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in records
    ]
    summaries.sort(key=lambda m: m.updated_at, reverse=True)
    return summaries


@router.get("/{motor_id}", response_model=MotorRecord)
async def get_team_motor(
    team_id: str,
    motor_id: str,
    _: TeamMembership = Depends(require_team_role("viewer")),
    repo: TeamMotorRepository = Depends(TeamMotorRepository),
) -> MotorRecord:
    record = await repo.get(team_id, motor_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motor not found.")
    return record


@router.put("/{motor_id}", response_model=MotorRecord)
async def update_team_motor(
    team_id: str,
    motor_id: str,
    body: UpdateMotorRequest,
    _: TeamMembership = Depends(require_team_role("editor")),
    repo: TeamMotorRepository = Depends(TeamMotorRepository),
) -> MotorRecord:
    record = await repo.get(team_id, motor_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motor not found.")
    updated = record.model_copy(
        update={
            "name": body.name if body.name is not None else record.name,
            "config": body.config if body.config is not None else record.config,
            "updated_at": datetime.now(UTC),
        }
    )
    await repo.save(team_id, motor_id, updated)
    return updated


@router.delete("/{motor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team_motor(
    team_id: str,
    motor_id: str,
    _: TeamMembership = Depends(require_team_role("editor")),
    repo: TeamMotorRepository = Depends(TeamMotorRepository),
) -> None:
    record = await repo.get(team_id, motor_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motor not found.")
    await repo.delete(team_id, motor_id)
