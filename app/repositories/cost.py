from __future__ import annotations

from app.repositories.base import GCSRepository
from app.schemas.credits import SimulationCostRecord


def _cost_blob(user_id: str, simulation_id: str) -> str:
    return f"users/{user_id}/simulations/{simulation_id}/cost.json"


class CostRepository(GCSRepository):
    """Reads and writes per-simulation cost records.

    Lives under the simulation prefix so it is wiped automatically by the
    simulation repository's prefix-delete on cleanup.
    """

    async def get(self, user_id: str, simulation_id: str) -> SimulationCostRecord | None:
        data = await self._read(_cost_blob(user_id, simulation_id))
        if data is None:
            return None
        return SimulationCostRecord.model_validate(data)

    async def save(self, user_id: str, simulation_id: str, record: SimulationCostRecord) -> None:
        await self._write(_cost_blob(user_id, simulation_id), record.model_dump(mode="json"))
