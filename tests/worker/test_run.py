"""Tests for ``app.worker.run`` — the Cloud Run Jobs entrypoint that pulls
a config from GCS, runs the simulation, and writes status + results back.

The actual ``machwave`` simulation engine is stubbed: we don't care here
that it produces correct numbers, only that the worker advances state
machine through ``pending → running → done``, persists results, and on each
failure mode flips status to ``failed`` with an error message.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories.simulation import SimulationRepository
from app.schemas.motor import (
    SOLID_FORMULATIONS,
    BatesSegmentSchema,
    CombustionChamberSchema,
    GrainSchema,
    NozzleSchema,
    SolidMotorConfigSchema,
    SolidMotorThrustChamberSchema,
)
from app.schemas.simulation import (
    IBSimParamsSchema,
    SimulationJobConfig,
)
from app.worker import run as worker_run
from tests.conftest import FakeGCS  # noqa: F401  (used in type hints)


def _job_config(simulation_id: str, user_id: str) -> SimulationJobConfig:
    return SimulationJobConfig(
        simulation_id=simulation_id,
        user_id=user_id,
        motor_id="motor-1",
        motor_config=SolidMotorConfigSchema(
            propellant_id=next(iter(SOLID_FORMULATIONS)),
            grain=GrainSchema(
                segments=[
                    BatesSegmentSchema(outer_diameter=0.069, core_diameter=0.025, length=0.12)
                ]
            ),
            thrust_chamber=SolidMotorThrustChamberSchema(
                nozzle=NozzleSchema(
                    inlet_diameter=0.060,
                    throat_diameter=0.015,
                    divergent_angle=12,
                    convergent_angle=45,
                    expansion_ratio=8,
                ),
                combustion_chamber=CombustionChamberSchema(
                    casing_inner_diameter=0.0702,
                    casing_outer_diameter=0.0762,
                    internal_length=0.280,
                ),
                dry_mass=1.5,
                nozzle_exit_to_grain_port_distance=0.010,
            ),
        ),
        params=IBSimParamsSchema(),
    )


# ---------------------------------------------------------------------------
# Stub simulation pipeline — replaces the real ``machwave`` engine.
# ---------------------------------------------------------------------------


def _make_solid_state(thrust_time: float = 0.1) -> object:
    """Build a real ``SolidMotorState`` instance without invoking ``__init__``
    (which requires a valid motor / pressures / etc.). Setting
    ``_thrust_time`` directly satisfies the ``thrust_time`` property the
    worker reads after ``sim.run()``."""
    from machwave.states.solid_motor import SolidMotorState

    state = SolidMotorState.__new__(SolidMotorState)
    state._thrust_time = thrust_time
    return state


class _StubSim:
    """Stand-in for ``InternalBallisticsSimulation`` returning a fake state."""

    def __init__(self, motor: object, params: object) -> None:
        pass

    def run(self) -> tuple[object, object]:
        return None, _make_solid_state()


def _patch_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    import machwave.simulation as machwave_sim

    from app.schemas.simulation import SolidSimulationResultsSchema

    monkeypatch.setattr(machwave_sim, "InternalBallisticsSimulation", _StubSim)

    def fake_from_machwave(simulation_id: str, motor_state: object) -> SolidSimulationResultsSchema:
        return _minimal_results(simulation_id)

    monkeypatch.setattr(SolidSimulationResultsSchema, "from_machwave", fake_from_machwave)


def _minimal_results(simulation_id: str) -> Any:
    """Construct a valid ``SolidSimulationResultsSchema`` directly."""
    from app.schemas.simulation import SolidSimulationResultsSchema

    return SolidSimulationResultsSchema(
        simulation_id=simulation_id,
        t=[0.0, 0.1],
        thrust=[0.0, 100.0],
        P_0=[101_325.0, 1_000_000.0],
        P_exit=[101_325.0, 200_000.0],
        m_prop=[1.0, 0.9],
        burn_area=[0.01, 0.01],
        propellant_volume=[0.001, 0.0009],
        free_chamber_volume=[0.002, 0.0021],
        web=[0.0, 0.001],
        burn_rate=[0.001, 0.001],
        C_f=[1.5, 1.5],
        C_f_ideal=[1.6, 1.6],
        nozzle_efficiency=[0.95, 0.95],
        overall_efficiency=[0.9, 0.9],
        eta_div=[99.0, 99.0],
        eta_kin=[98.0, 98.0],
        eta_bl=[97.0, 97.0],
        eta_2p=[96.0, 96.0],
        grain_mass_flux=[[1.0], [1.1]],
        propellant_cog=[[0.1, 0.0, 0.0], [0.1, 0.0, 0.0]],
        propellant_moi=[[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]] * 2,
        total_impulse=10.0,
        specific_impulse=100.0,
        thrust_time=0.1,
        burn_time=0.1,
        max_thrust=100.0,
        avg_thrust=50.0,
        max_chamber_pressure=1_000_000.0,
        avg_chamber_pressure=500_000.0,
        avg_nozzle_efficiency=0.95,
        avg_overall_efficiency=0.9,
        initial_propellant_mass=1.0,
        volumetric_efficiency=0.5,
        mean_klemmung=100.0,
        max_klemmung=110.0,
        initial_to_final_klemmung_ratio=1.1,
        max_mass_flux=1.1,
        burn_profile="neutral",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkerRun:
    @pytest.mark.asyncio
    async def test_happy_path(self, fake_gcs: FakeGCS, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("sim-1", "u1"))

        _patch_simulation(monkeypatch)
        await worker_run.run("sim-1", "u1")

        # status = done, results blob written, error is None.
        status = await repo.get_status("u1", "sim-1")
        assert status is not None
        assert status.status == "done"
        assert status.error is None

        results = await repo.get_results("u1", "sim-1")
        assert results is not None
        assert results.simulation_id == "sim-1"

    @pytest.mark.asyncio
    async def test_missing_config_marks_failed(
        self, fake_gcs: FakeGCS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = SimulationRepository()

        with pytest.raises(SystemExit):
            await worker_run.run("sim-1", "u1")

        status = await repo.get_status("u1", "sim-1")
        assert status is not None
        assert status.status == "failed"
        assert status.error and "Config not found" in status.error

    @pytest.mark.asyncio
    async def test_to_machwave_failure_marks_failed(
        self, fake_gcs: FakeGCS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("sim-1", "u1"))

        # Force a translation-layer crash.
        monkeypatch.setattr(
            SolidMotorConfigSchema,
            "to_machwave",
            lambda self: (_ for _ in ()).throw(RuntimeError("translate boom")),
        )

        with pytest.raises(SystemExit):
            await worker_run.run("sim-1", "u1")

        status = await repo.get_status("u1", "sim-1")
        assert status is not None
        assert status.status == "failed"
        assert "translate boom" in (status.error or "")

    @pytest.mark.asyncio
    async def test_simulation_failure_marks_failed(
        self, fake_gcs: FakeGCS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("sim-1", "u1"))

        import machwave.simulation as machwave_sim

        class _ExplodingSim:
            def __init__(self, *a: object, **kw: object) -> None:
                pass

            def run(self) -> tuple[object, object]:
                raise RuntimeError("solver diverged")

        monkeypatch.setattr(machwave_sim, "InternalBallisticsSimulation", _ExplodingSim)

        with pytest.raises(SystemExit):
            await worker_run.run("sim-1", "u1")

        status = await repo.get_status("u1", "sim-1")
        assert status is not None
        assert status.status == "failed"
        assert "solver diverged" in (status.error or "")

    @pytest.mark.asyncio
    async def test_unsupported_motor_state_marks_failed(
        self, fake_gcs: FakeGCS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised motor-state class falls through to a TypeError that
        the worker catches and reports as ``failed`` rather than letting the
        process exit cleanly with status still ``running``."""
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("sim-1", "u1"))

        import machwave.simulation as machwave_sim

        class _AlienState:
            # ``thrust_time`` is read by the worker for logging; provide it so
            # we reach the type-dispatch step we actually want to exercise.
            thrust_time = 0.0

        class _AlienSim:
            def __init__(self, *a: object, **kw: object) -> None:
                pass

            def run(self) -> tuple[object, object]:
                return None, _AlienState()

        monkeypatch.setattr(machwave_sim, "InternalBallisticsSimulation", _AlienSim)

        with pytest.raises(SystemExit):
            await worker_run.run("sim-1", "u1")

        status = await repo.get_status("u1", "sim-1")
        assert status is not None
        assert status.status == "failed"
        assert "Unsupported motor state" in (status.error or "")
