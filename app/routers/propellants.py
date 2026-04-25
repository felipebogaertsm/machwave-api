from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.schemas.motor import SOLID_FORMULATIONS

router = APIRouter()


class PropellantItem(BaseModel):
    id: str
    name: str


@router.get("", response_model=list[PropellantItem])
async def list_propellants() -> list[PropellantItem]:
    """Return the list of built-in solid propellant formulations."""
    return [
        PropellantItem(id=propellant_id, name=propellant.name)
        for propellant_id, propellant in sorted(SOLID_FORMULATIONS.items())
    ]
