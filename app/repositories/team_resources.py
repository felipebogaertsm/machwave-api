"""Team-scoped motor, simulation, and cost repositories.

These mirror :mod:`app.repositories.motor`, :mod:`app.repositories.simulation`,
and :mod:`app.repositories.cost` line-for-line, swapping the path prefix from
``users/{uid}/...`` to ``teams/{tid}/...``. Two scopes today don't justify a
generic owner-keyed repo — see the ADR in
:mod:`app.repositories.team` for the rationale.
"""

from __future__ import annotations

import logging

from pydantic import TypeAdapter

from app.repositories.base import GCSRepository
from app.schemas.credits import SimulationCostRecord
from app.schemas.motor import MotorRecord
from app.schemas.simulation import (
    LiquidSimulationResultsSchema,
    SimulationJobConfig,
    SimulationResultsSchema,
    SimulationStatus,
    SimulationStatusRecord,
    SimulationSummary,
    SolidSimulationResultsSchema,
)

logger = logging.getLogger(__name__)

_RESULTS_ADAPTER: TypeAdapter[SimulationResultsSchema] = TypeAdapter(SimulationResultsSchema)


# ---------------------------------------------------------------------------
# Motors
# ---------------------------------------------------------------------------


def _team_motor_blob(team_id: str, motor_id: str) -> str:
    return f"teams/{team_id}/motors/{motor_id}.json"


def _team_motors_prefix(team_id: str) -> str:
    return f"teams/{team_id}/motors/"


class TeamMotorRepository(GCSRepository):
    async def list(self, team_id: str) -> list[MotorRecord]:
        blob_names = await self._list(_team_motors_prefix(team_id))
        records: list[MotorRecord] = []
        for blob_name in blob_names:
            data = await self._read(blob_name)
            if data is None:
                continue
            try:
                records.append(MotorRecord.model_validate(data))
            except Exception:
                logger.warning("Skipping malformed motor record: %s", blob_name, exc_info=True)
        return records

    async def get(self, team_id: str, motor_id: str) -> MotorRecord | None:
        data = await self._read(_team_motor_blob(team_id, motor_id))
        if data is None:
            return None
        return MotorRecord.model_validate(data)

    async def save(self, team_id: str, motor_id: str, record: MotorRecord) -> None:
        await self._write(_team_motor_blob(team_id, motor_id), record.model_dump(mode="json"))

    async def delete(self, team_id: str, motor_id: str) -> None:
        await self._delete(_team_motor_blob(team_id, motor_id))


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------


def _team_simulations_prefix(team_id: str) -> str:
    return f"teams/{team_id}/simulations/"


def _team_simulation_dir(team_id: str, simulation_id: str) -> str:
    return f"teams/{team_id}/simulations/{simulation_id}/"


def _team_config_blob(team_id: str, simulation_id: str) -> str:
    return f"teams/{team_id}/simulations/{simulation_id}/config.json"


def _team_status_blob(team_id: str, simulation_id: str) -> str:
    return f"teams/{team_id}/simulations/{simulation_id}/status.json"


def _team_results_blob(team_id: str, simulation_id: str) -> str:
    return f"teams/{team_id}/simulations/{simulation_id}/results.json"


def _team_cost_blob(team_id: str, simulation_id: str) -> str:
    return f"teams/{team_id}/simulations/{simulation_id}/cost.json"


class TeamSimulationRepository(GCSRepository):
    async def list_summaries(self, team_id: str) -> list[SimulationSummary]:
        all_blobs = await self._list(_team_simulations_prefix(team_id))
        status_blobs = [b for b in all_blobs if b.endswith("/status.json")]

        summaries: list[SimulationSummary] = []
        for blob_name in status_blobs:
            parts = blob_name.rstrip("/").split("/")
            if len(parts) < 4:
                continue
            simulation_id = parts[-2]

            status_data = await self._read(blob_name)
            config_data = await self._read(_team_config_blob(team_id, simulation_id))
            if status_data is None or config_data is None:
                continue

            try:
                status_record = SimulationStatusRecord.model_validate(status_data)
                job_config = SimulationJobConfig.model_validate(config_data)
                summaries.append(
                    SimulationSummary(
                        simulation_id=simulation_id,
                        motor_id=job_config.motor_id,
                        motor_type=job_config.motor_config.motor_type,
                        status=status_record.status,
                        created_at=status_record.created_at,
                        updated_at=status_record.updated_at,
                    )
                )
            except Exception:
                logger.warning("Skipping malformed simulation: %s", blob_name, exc_info=True)
                continue

        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    async def get_config(self, team_id: str, simulation_id: str) -> SimulationJobConfig | None:
        data = await self._read(_team_config_blob(team_id, simulation_id))
        if data is None:
            return None
        return SimulationJobConfig.model_validate(data)

    async def get_status(self, team_id: str, simulation_id: str) -> SimulationStatusRecord | None:
        data = await self._read(_team_status_blob(team_id, simulation_id))
        if data is None:
            return None
        return SimulationStatusRecord.model_validate(data)

    async def get_results(
        self, team_id: str, simulation_id: str
    ) -> SolidSimulationResultsSchema | LiquidSimulationResultsSchema | None:
        data = await self._read(_team_results_blob(team_id, simulation_id))
        if data is None:
            return None
        return _RESULTS_ADAPTER.validate_python(data)

    async def save_config(
        self, team_id: str, simulation_id: str, config: SimulationJobConfig
    ) -> None:
        await self._write(_team_config_blob(team_id, simulation_id), config.model_dump(mode="json"))

    async def save_status(
        self, team_id: str, simulation_id: str, record: SimulationStatusRecord
    ) -> None:
        await self._write(_team_status_blob(team_id, simulation_id), record.model_dump(mode="json"))

    async def append_status_event(
        self,
        team_id: str,
        simulation_id: str,
        status: SimulationStatus,
        error: str | None = None,
    ) -> SimulationStatusRecord:
        record = await self.get_status(team_id, simulation_id)
        if record is None:
            record = SimulationStatusRecord(simulation_id=simulation_id, events=[])
        record.append(status, error=error)
        await self.save_status(team_id, simulation_id, record)
        return record

    async def save_results(
        self,
        team_id: str,
        simulation_id: str,
        results: SolidSimulationResultsSchema | LiquidSimulationResultsSchema,
    ) -> None:
        await self._write(
            _team_results_blob(team_id, simulation_id), results.model_dump(mode="json")
        )

    async def delete(self, team_id: str, simulation_id: str) -> None:
        await self._delete(_team_simulation_dir(team_id, simulation_id))


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


class TeamCostRepository(GCSRepository):
    async def get(self, team_id: str, simulation_id: str) -> SimulationCostRecord | None:
        data = await self._read(_team_cost_blob(team_id, simulation_id))
        if data is None:
            return None
        return SimulationCostRecord.model_validate(data)

    async def save(self, team_id: str, simulation_id: str, record: SimulationCostRecord) -> None:
        await self._write(_team_cost_blob(team_id, simulation_id), record.model_dump(mode="json"))
