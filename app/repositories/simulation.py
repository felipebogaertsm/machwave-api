from __future__ import annotations

import logging

from app.repositories.base import GCSRepository
from app.schemas.simulation import (
    SimulationJobConfig,
    SimulationResultsSchema,
    SimulationStatusRecord,
    SimulationSummary,
)

logger = logging.getLogger(__name__)


def _simulations_prefix(user_id: str) -> str:
    return f"users/{user_id}/simulations/"


def _simulation_dir(user_id: str, simulation_id: str) -> str:
    return f"users/{user_id}/simulations/{simulation_id}/"


def _config_blob(user_id: str, simulation_id: str) -> str:
    return f"users/{user_id}/simulations/{simulation_id}/config.json"


def _status_blob(user_id: str, simulation_id: str) -> str:
    return f"users/{user_id}/simulations/{simulation_id}/status.json"


def _results_blob(user_id: str, simulation_id: str) -> str:
    return f"users/{user_id}/simulations/{simulation_id}/results.json"


class SimulationRepository(GCSRepository):
    async def list_all_simulation_pairs(self) -> list[tuple[str, str]]:
        """Return ``(user_id, simulation_id)`` for every simulation in the bucket."""
        all_blobs = await self._list("users/")
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for blob_name in all_blobs:
            # Path: users/{user_id}/simulations/{simulation_id}/...
            parts = blob_name.split("/")
            if len(parts) < 5 or parts[0] != "users" or parts[2] != "simulations":
                continue
            pair = (parts[1], parts[3])
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
        return pairs

    async def list_summaries(self, user_id: str) -> list[SimulationSummary]:
        all_blobs = await self._list(_simulations_prefix(user_id))
        status_blobs = [b for b in all_blobs if b.endswith("/status.json")]

        summaries: list[SimulationSummary] = []
        for blob_name in status_blobs:
            # Path: users/{user_id}/simulations/{simulation_id}/status.json
            parts = blob_name.rstrip("/").split("/")
            if len(parts) < 4:
                continue
            simulation_id = parts[-2]

            status_data = await self._read(blob_name)
            config_data = await self._read(_config_blob(user_id, simulation_id))
            if status_data is None or config_data is None:
                continue

            try:
                status_record = SimulationStatusRecord.model_validate(status_data)
                job_config = SimulationJobConfig.model_validate(config_data)
                summaries.append(
                    SimulationSummary(
                        simulation_id=simulation_id,
                        motor_id=job_config.motor_id,
                        status=status_record.status,
                        created_at=status_record.created_at,
                        updated_at=status_record.updated_at,
                    )
                )
            except Exception:
                logger.warning("Skipping malformed simulation record: %s", blob_name, exc_info=True)
                continue

        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    async def get_config(self, user_id: str, simulation_id: str) -> SimulationJobConfig | None:
        data = await self._read(_config_blob(user_id, simulation_id))
        if data is None:
            return None
        return SimulationJobConfig.model_validate(data)

    async def get_status(self, user_id: str, simulation_id: str) -> SimulationStatusRecord | None:
        data = await self._read(_status_blob(user_id, simulation_id))
        if data is None:
            return None
        return SimulationStatusRecord.model_validate(data)

    async def get_results(self, user_id: str, simulation_id: str) -> SimulationResultsSchema | None:
        data = await self._read(_results_blob(user_id, simulation_id))
        if data is None:
            return None
        return SimulationResultsSchema.model_validate(data)

    async def save_config(
        self, user_id: str, simulation_id: str, config: SimulationJobConfig
    ) -> None:
        await self._write(_config_blob(user_id, simulation_id), config.model_dump(mode="json"))

    async def save_status(
        self, user_id: str, simulation_id: str, record: SimulationStatusRecord
    ) -> None:
        await self._write(_status_blob(user_id, simulation_id), record.model_dump(mode="json"))

    async def save_results(
        self, user_id: str, simulation_id: str, results: SimulationResultsSchema
    ) -> None:
        await self._write(_results_blob(user_id, simulation_id), results.model_dump(mode="json"))

    async def delete(self, user_id: str, simulation_id: str) -> None:
        await self._delete(_simulation_dir(user_id, simulation_id))
