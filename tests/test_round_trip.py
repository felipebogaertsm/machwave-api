"""Round-trip tests: schemas → machwave objects → run simulation → results.

These tests exercise the full serialisation / deserialisation stack without
any GCS or Firebase dependencies.  They use the bundled KNSB_NAKKA propellant
and a simple two-segment BATES grain (mirrors the Olympus motor example).

Run with:
    uv run pytest tests/ -v
"""

from __future__ import annotations

import pytest

from app.schemas.motor import (
    BatesSegmentSchema,
    CombustionChamberSchema,
    GrainSchema,
    NozzleSchema,
    SolidMotorConfigSchema,
    SolidMotorThrustChamberSchema,
)
from app.schemas.simulation import (
    IBSimParamsSchema,
    SimulationResultsSchema,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bates_segment() -> BatesSegmentSchema:
    return BatesSegmentSchema(
        outer_diameter=0.069,
        core_diameter=0.025,
        length=0.120,
    )


@pytest.fixture()
def grain_schema(bates_segment: BatesSegmentSchema) -> GrainSchema:
    return GrainSchema(segments=[bates_segment, bates_segment], spacing=0.005)


@pytest.fixture()
def nozzle_schema() -> NozzleSchema:
    return NozzleSchema(
        inlet_diameter=0.060,
        throat_diameter=0.015,
        divergent_angle=12,
        convergent_angle=45,
        expansion_ratio=8,
    )


@pytest.fixture()
def chamber_schema() -> CombustionChamberSchema:
    return CombustionChamberSchema(
        casing_inner_diameter=0.0702,
        casing_outer_diameter=0.0762,
        internal_length=0.280,
        thermal_liner_thickness=0.003,
    )


@pytest.fixture()
def thrust_chamber_schema(
    nozzle_schema: NozzleSchema,
    chamber_schema: CombustionChamberSchema,
) -> SolidMotorThrustChamberSchema:
    return SolidMotorThrustChamberSchema(
        nozzle=nozzle_schema,
        combustion_chamber=chamber_schema,
        dry_mass=1.5,
        nozzle_exit_to_grain_port_distance=0.010,
        center_of_gravity_coordinate=(0.15, 0.0, 0.0),
    )


@pytest.fixture()
def motor_config_schema(
    grain_schema: GrainSchema,
    thrust_chamber_schema: SolidMotorThrustChamberSchema,
) -> SolidMotorConfigSchema:
    return SolidMotorConfigSchema(
        propellant_id="KNSB_NAKKA",
        grain=grain_schema,
        thrust_chamber=thrust_chamber_schema,
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
# Schema unit tests
# ---------------------------------------------------------------------------


class TestBatesSegmentSchema:
    def test_valid_segment_roundtrip(self, bates_segment: BatesSegmentSchema) -> None:
        raw = bates_segment.model_dump()
        restored = BatesSegmentSchema.model_validate(raw)
        assert restored == bates_segment

    def test_core_larger_than_outer_raises(self) -> None:
        with pytest.raises(Exception, match="core_diameter"):
            BatesSegmentSchema(
                outer_diameter=0.030,
                core_diameter=0.040,  # core > outer — must fail
                length=0.100,
            )

    def test_to_machwave(self, bates_segment: BatesSegmentSchema) -> None:
        from machwave.models.grain.geometries.bates import BatesSegment

        seg = bates_segment.to_machwave()
        assert isinstance(seg, BatesSegment)
        assert seg.outer_diameter == bates_segment.outer_diameter
        assert seg.core_diameter == bates_segment.core_diameter
        assert seg.length == bates_segment.length


class TestGrainSchema:
    def test_to_machwave(self, grain_schema: GrainSchema) -> None:
        from machwave.models.grain.base import Grain

        grain = grain_schema.to_machwave()
        assert isinstance(grain, Grain)
        assert grain.segment_count == 2
        assert grain.spacing == grain_schema.spacing


class TestNozzleSchema:
    def test_throat_larger_than_inlet_raises(self) -> None:
        with pytest.raises(Exception, match="throat_diameter"):
            NozzleSchema(
                inlet_diameter=0.010,
                throat_diameter=0.020,  # throat > inlet — must fail
                divergent_angle=12,
                convergent_angle=45,
                expansion_ratio=8,
            )


class TestSolidMotorConfigSchema:
    def test_invalid_propellant_id_raises(
        self,
        grain_schema: GrainSchema,
        thrust_chamber_schema: SolidMotorThrustChamberSchema,
    ) -> None:
        with pytest.raises(Exception, match="Unknown propellant_id"):
            SolidMotorConfigSchema(
                propellant_id="NOT_A_REAL_PROPELLANT",
                grain=grain_schema,
                thrust_chamber=thrust_chamber_schema,
            )

    def test_to_machwave(self, motor_config_schema: SolidMotorConfigSchema) -> None:
        from machwave.models.motors.solid import SolidMotor

        motor = motor_config_schema.to_machwave()
        assert isinstance(motor, SolidMotor)


# ---------------------------------------------------------------------------
# Integration test — full simulation round-trip
# ---------------------------------------------------------------------------


class TestSimulationRoundTrip:
    def test_run_and_serialise(
        self,
        motor_config_schema: SolidMotorConfigSchema,
        sim_params: IBSimParamsSchema,
    ) -> None:
        """Build motor → run simulation → serialise results → validate schema."""
        from machwave.simulation import InternalBallisticsSimulation

        motor = motor_config_schema.to_machwave()
        params = sim_params.to_machwave()

        sim = InternalBallisticsSimulation(motor=motor, params=params)
        _t, motor_state = sim.run()

        results = SimulationResultsSchema.from_machwave("test-sim-id", motor_state)

        # Basic sanity checks
        assert results.simulation_id == "test-sim-id"
        assert results.total_impulse > 0
        assert results.thrust_time > 0
        assert results.specific_impulse > 0
        assert results.max_thrust > 0
        assert len(results.t) == len(results.thrust)
        assert len(results.t) == len(results.P_0)
        assert results.burn_profile in ("regressive", "neutral", "progressive")

        # Verify JSON round-trip
        raw = results.model_dump(mode="json")
        restored = SimulationResultsSchema.model_validate(raw)
        assert restored.total_impulse == pytest.approx(results.total_impulse, rel=1e-6)
