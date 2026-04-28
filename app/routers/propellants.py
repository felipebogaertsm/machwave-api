from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.schemas.motor import BILIQUID_FORMULATIONS, SOLID_FORMULATIONS

router = APIRouter()


class PropellantItem(BaseModel):
    id: str
    name: str
    motor_type: Literal["solid", "liquid"]


@router.get("", response_model=list[PropellantItem])
async def list_propellants() -> list[PropellantItem]:
    """Return the list of built-in propellant formulations (solid and biliquid)."""
    items: list[PropellantItem] = [
        PropellantItem(id=propellant_id, name=propellant.name, motor_type="solid")
        for propellant_id, propellant in sorted(SOLID_FORMULATIONS.items())
    ]
    items.extend(
        PropellantItem(id=propellant_id, name=propellant.name, motor_type="liquid")
        for propellant_id, propellant in sorted(BILIQUID_FORMULATIONS.items())
    )
    return items
