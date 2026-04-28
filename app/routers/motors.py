"""Motor CRUD router."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.auth.firebase import get_current_user
from app.auth.rbac import require_role
from app.repositories.motor import MotorRepository
from app.schemas.motor import MotorConfigSchema, MotorRecord, MotorSummary

router = APIRouter()
admin_router = APIRouter()


class CreateMotorRequest(BaseModel):
    name: str
    config: MotorConfigSchema


class CreateMotorResponse(BaseModel):
    motor_id: str


class UpdateMotorRequest(BaseModel):
    name: str | None = None
    config: MotorConfigSchema | None = None


class ClearAllMotorsResponse(BaseModel):
    deleted: int
    user_ids: list[str]


@router.post("", response_model=CreateMotorResponse, status_code=status.HTTP_201_CREATED)
async def create_motor(
    body: CreateMotorRequest,
    user: dict[str, Any] = Depends(get_current_user),
    repo: MotorRepository = Depends(MotorRepository),
) -> CreateMotorResponse:
    user_id: str = user["uid"]
    motor_id = str(uuid.uuid4())
    record = MotorRecord(motor_id=motor_id, name=body.name, config=body.config)
    await repo.save(user_id, motor_id, record)
    return CreateMotorResponse(motor_id=motor_id)


@router.get("", response_model=list[MotorSummary])
async def list_motors(
    user: dict[str, Any] = Depends(get_current_user),
    repo: MotorRepository = Depends(MotorRepository),
) -> list[MotorSummary]:
    user_id: str = user["uid"]
    records = await repo.list(user_id)
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
async def get_motor(
    motor_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: MotorRepository = Depends(MotorRepository),
) -> MotorRecord:
    user_id: str = user["uid"]
    record = await repo.get(user_id, motor_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motor not found.")
    return record


@router.put("/{motor_id}", response_model=MotorRecord)
async def update_motor(
    motor_id: str,
    body: UpdateMotorRequest,
    user: dict[str, Any] = Depends(get_current_user),
    repo: MotorRepository = Depends(MotorRepository),
) -> MotorRecord:
    user_id: str = user["uid"]
    record = await repo.get(user_id, motor_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motor not found.")
    updated = record.model_copy(
        update={
            "name": body.name if body.name is not None else record.name,
            "config": body.config if body.config is not None else record.config,
            "updated_at": datetime.now(UTC),
        }
    )
    await repo.save(user_id, motor_id, updated)
    return updated


@router.delete("/{motor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_motor(
    motor_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    repo: MotorRepository = Depends(MotorRepository),
) -> None:
    user_id: str = user["uid"]
    record = await repo.get(user_id, motor_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motor not found.")
    await repo.delete(user_id, motor_id)


@admin_router.delete("/clear-all", response_model=ClearAllMotorsResponse)
async def admin_clear_all_motors(
    user_id: str | None = Query(default=None),
    _: dict[str, Any] = Depends(require_role("admin")),
    repo: MotorRepository = Depends(MotorRepository),
) -> ClearAllMotorsResponse:
    """Delete every motor record. Admin-only.

    With ``user_id``, scoped to that user; without it, every user's motors
    are wiped.
    """
    target_users = [user_id] if user_id is not None else await repo.list_all_users_with_motors()
    deleted = 0
    cleared: list[str] = []
    for uid in target_users:
        count = await repo.delete_all_for_user(uid)
        deleted += count
        cleared.append(uid)
    return ClearAllMotorsResponse(deleted=deleted, user_ids=cleared)
