"""Tests for ``app.schemas.simulation``.

Schema unit tests + an end-to-end run of the bundled physics engine, which
exercises every ``to_machwave`` translation, the simulation loop, and the
``from_machwave`` serialisation back into a JSON-safe schema.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

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
    SimulationDetailsResponse,
    SimulationJobConfig,
    SimulationResultsSchema,
    SimulationStatusRecord,
    SimulationSummary,
    SolidSimulationResultsSchema,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal SRM that the ``machwave`` engine can simulate quickly.
# ---------------------------------------------------------------------------


@pytest.fixture()
def motor_config() -> SolidMotorConfigSchema:
    return SolidMotorConfigSchema(
        propellant_id=next(iter(SOLID_FORMULATIONS)),
        grain=GrainSchema(
            segments=[
                BatesSegmentSchema(outer_diameter=0.069, core_diameter=0.025, length=0.120),
                BatesSegmentSchema(outer_diameter=0.069, core_diameter=0.025, length=0.120),
            ],
            spacing=0.005,
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
                thermal_liner_thickness=0.003,
            ),
            dry_mass=1.5,
            nozzle_exit_to_grain_port_distance=0.010,
            center_of_gravity_coordinate=(0.15, 0.0, 0.0),
        ),
    )


@pytest.fixture()
def sim_params() -> IBSimParamsSchema:
    return IBSimParamsSchema(
        d_t=0.001,
        igniter_pressure=1_000_000.0,
        external_pressure=101_325.0,
        other_losses=12.0,
    )


# ---------------------------------------------------------------------------
# IBSimParamsSchema validation
# ---------------------------------------------------------------------------


class TestIBSimParamsSchema:
    def test_defaults_pass(self) -> None:
        params = IBSimParamsSchema()
        assert params.d_t == 0.01
        assert params.external_pressure == pytest.approx(101_325.0)

    @pytest.mark.parametrize("d_t", [0, -0.001, 0.11])
    def test_d_t_bounds(self, d_t: float) -> None:
        with pytest.raises(ValidationError):
            IBSimParamsSchema(d_t=d_t)

    @pytest.mark.parametrize("losses", [-0.1, 100.1])
    def test_other_losses_out_of_range(self, losses: float) -> None:
        with pytest.raises(ValidationError):
            IBSimParamsSchema(other_losses=losses)

    @pytest.mark.parametrize("field", ["igniter_pressure", "external_pressure"])
    def test_pressures_must_be_positive(self, field: str) -> None:
        with pytest.raises(ValidationError):
            IBSimParamsSchema(**{field: 0})

    def test_to_machwave_round_trips(self) -> None:
        params = IBSimParamsSchema(d_t=0.005, other_losses=5.0)
        translated = params.to_machwave()
        assert translated.d_t == 0.005
        assert translated.other_losses == 5.0


# ---------------------------------------------------------------------------
# SimulationStatusRecord
# ---------------------------------------------------------------------------


class TestSimulationStatusRecord:
    def test_default_status_pending(self) -> None:
        record = SimulationStatusRecord(simulation_id="sim-1")
        assert record.status == "pending"
        assert record.error is None

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SimulationStatusRecord(simulation_id="sim-1", status="weird")  # type: ignore[arg-type]

    def test_failed_with_error_message(self) -> None:
        record = SimulationStatusRecord(simulation_id="sim-1", status="failed", error="boom")
        assert record.error == "boom"
        # JSON round-trip preserves error
        restored = SimulationStatusRecord.model_validate(record.model_dump(mode="json"))
        assert restored == record


# ---------------------------------------------------------------------------
# Job config
# ---------------------------------------------------------------------------


class TestSimulationJobConfig:
    def test_round_trip(
        self, motor_config: SolidMotorConfigSchema, sim_params: IBSimParamsSchema
    ) -> None:
        job = SimulationJobConfig(
            simulation_id="sim-1",
            user_id="user-1",
            motor_id="motor-1",
            motor_config=motor_config,
            params=sim_params,
        )
        raw = job.model_dump(mode="json")
        restored = SimulationJobConfig.model_validate(raw)
        # Discriminator survives the round-trip.
        assert restored.motor_config.motor_type == "solid"
        assert restored.simulation_id == "sim-1"


# ---------------------------------------------------------------------------
# Full simulation round-trip — schema → machwave → run → schema → JSON.
# ---------------------------------------------------------------------------


class TestSimulationRoundTrip:
    def test_run_and_serialise(
        self,
        motor_config: SolidMotorConfigSchema,
        sim_params: IBSimParamsSchema,
    ) -> None:
        from machwave.simulation import InternalBallisticsSimulation

        motor = motor_config.to_machwave()
        params = sim_params.to_machwave()
        sim = InternalBallisticsSimulation(motor=motor, params=params)
        _t, motor_state = sim.run()

        results = SolidSimulationResultsSchema.from_machwave("sim-1", motor_state)

        # Physical sanity
        assert results.motor_type == "solid"
        assert results.simulation_id == "sim-1"
        assert results.total_impulse > 0
        assert results.thrust_time > 0
        assert results.specific_impulse > 0
        assert results.max_thrust > 0
        assert results.avg_thrust <= results.max_thrust

        # Time-series arrays have matching lengths.
        assert len(results.t) == len(results.thrust) == len(results.P_0)

        # Burn profile is one of the documented values.
        assert results.burn_profile in ("regressive", "neutral", "progressive")

        # JSON round-trip preserves scalar metrics.
        raw = results.model_dump(mode="json")
        restored = SolidSimulationResultsSchema.model_validate(raw)
        assert restored.total_impulse == pytest.approx(results.total_impulse, rel=1e-6)
        assert restored.max_thrust == pytest.approx(results.max_thrust, rel=1e-6)


# ---------------------------------------------------------------------------
# Discriminated SimulationResultsSchema
# ---------------------------------------------------------------------------


class TestSimulationResultsDiscriminator:
    def test_solid_dispatch(
        self, motor_config: SolidMotorConfigSchema, sim_params: IBSimParamsSchema
    ) -> None:
        from machwave.simulation import InternalBallisticsSimulation

        sim = InternalBallisticsSimulation(
            motor=motor_config.to_machwave(), params=sim_params.to_machwave()
        )
        _t, motor_state = sim.run()
        results = SolidSimulationResultsSchema.from_machwave("sim-1", motor_state)

        adapter = TypeAdapter(SimulationResultsSchema)
        restored = adapter.validate_python(results.model_dump(mode="json"))
        assert isinstance(restored, SolidSimulationResultsSchema)

    def test_unknown_motor_type_rejected(self) -> None:
        adapter = TypeAdapter(SimulationResultsSchema)
        with pytest.raises(ValidationError):
            adapter.validate_python({"motor_type": "ion", "simulation_id": "x"})


# ---------------------------------------------------------------------------
# SimulationSummary / SimulationDetailsResponse
# ---------------------------------------------------------------------------


class TestSimulationSummary:
    def test_round_trip(self) -> None:
        summary = SimulationSummary(
            simulation_id="sim-1",
            motor_id="motor-1",
            motor_type="solid",
            status="done",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert SimulationSummary.model_validate(summary.model_dump(mode="json")) == summary

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SimulationSummary(
                simulation_id="sim-1",
                motor_id="motor-1",
                motor_type="solid",
                status="cancelled",  # type: ignore[arg-type]
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )


class TestSimulationDetailsResponse:
    def test_round_trip_carries_motor_config_and_params(
        self,
        motor_config: SolidMotorConfigSchema,
        sim_params: IBSimParamsSchema,
    ) -> None:
        from machwave.simulation import InternalBallisticsSimulation

        sim = InternalBallisticsSimulation(
            motor=motor_config.to_machwave(), params=sim_params.to_machwave()
        )
        _t, motor_state = sim.run()
        results = SolidSimulationResultsSchema.from_machwave("sim-1", motor_state)

        details = SimulationDetailsResponse(
            simulation_id="sim-1",
            motor_id="motor-1",
            motor_config=motor_config,
            params=sim_params,
            results=results,
        )
        raw = details.model_dump(mode="json")
        restored = SimulationDetailsResponse.model_validate(raw)
        assert restored.motor_config.motor_type == "solid"
        assert restored.params.d_t == sim_params.d_t
        assert restored.results.simulation_id == "sim-1"
