"""Pydantic schema tests for ``app.schemas.motor``.

Covers: per-field validation rules, model-level invariants
(``throat < inlet``, ``core < outer``, ``outer > inner``), the propellant-id
allow-list, JSON round-trip stability, the ``MotorConfigSchema`` discriminated
union, and the ``to_machwave`` translation layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.motor import (
    BILIQUID_FORMULATIONS,
    SOLID_FORMULATIONS,
    BatesSegmentSchema,
    BipropellantInjectorSchema,
    CombustionChamberSchema,
    GrainSchema,
    LiquidEngineConfigSchema,
    LiquidEngineThrustChamberSchema,
    MotorConfigSchema,
    MotorRecord,
    MotorSummary,
    NozzleSchema,
    SolidMotorConfigSchema,
    SolidMotorThrustChamberSchema,
    StackedTankPressureFedFeedSystemSchema,
    TankSchema,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal valid instances of every schema, reused across tests.
# ---------------------------------------------------------------------------


@pytest.fixture()
def bates_segment() -> BatesSegmentSchema:
    return BatesSegmentSchema(outer_diameter=0.069, core_diameter=0.025, length=0.120)


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
    nozzle_schema: NozzleSchema, chamber_schema: CombustionChamberSchema
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
    grain_schema: GrainSchema, thrust_chamber_schema: SolidMotorThrustChamberSchema
) -> SolidMotorConfigSchema:
    return SolidMotorConfigSchema(
        propellant_id=next(iter(SOLID_FORMULATIONS)),
        grain=grain_schema,
        thrust_chamber=thrust_chamber_schema,
    )


@pytest.fixture()
def lre_oxidizer_tank() -> TankSchema:
    return TankSchema(fluid_name="Oxygen", volume=0.05, temperature=90.0, initial_fluid_mass=30.0)


@pytest.fixture()
def lre_fuel_tank() -> TankSchema:
    return TankSchema(fluid_name="Hydrogen", volume=0.07, temperature=22.0, initial_fluid_mass=5.0)


@pytest.fixture()
def lre_feed_system(
    lre_oxidizer_tank: TankSchema, lre_fuel_tank: TankSchema
) -> StackedTankPressureFedFeedSystemSchema:
    return StackedTankPressureFedFeedSystemSchema(
        oxidizer_line_diameter=0.012,
        oxidizer_line_length=0.5,
        fuel_line_diameter=0.012,
        fuel_line_length=0.5,
        fuel_tank=lre_fuel_tank,
        oxidizer_tank=lre_oxidizer_tank,
        piston_loss=50_000.0,
    )


@pytest.fixture()
def lre_injector() -> BipropellantInjectorSchema:
    return BipropellantInjectorSchema(
        discharge_coefficient_fuel=0.7,
        discharge_coefficient_oxidizer=0.7,
        area_fuel=2.0e-5,
        area_ox=4.0e-5,
    )


@pytest.fixture()
def lre_thrust_chamber(
    nozzle_schema: NozzleSchema,
    chamber_schema: CombustionChamberSchema,
    lre_injector: BipropellantInjectorSchema,
) -> LiquidEngineThrustChamberSchema:
    return LiquidEngineThrustChamberSchema(
        nozzle=nozzle_schema,
        injector=lre_injector,
        combustion_chamber=chamber_schema,
        dry_mass=8.0,
        center_of_gravity_coordinate=(0.30, 0.0, 0.0),
    )


@pytest.fixture()
def lre_config(
    lre_thrust_chamber: LiquidEngineThrustChamberSchema,
    lre_feed_system: StackedTankPressureFedFeedSystemSchema,
) -> LiquidEngineConfigSchema:
    return LiquidEngineConfigSchema(
        propellant_id=next(iter(BILIQUID_FORMULATIONS)),
        thrust_chamber=lre_thrust_chamber,
        feed_system=lre_feed_system,
        oxidizer_tank_cog=0.80,
        fuel_tank_cog=1.20,
    )


# ---------------------------------------------------------------------------
# Nozzle
# ---------------------------------------------------------------------------


class TestNozzleSchema:
    def test_valid(self, nozzle_schema: NozzleSchema) -> None:
        assert nozzle_schema.throat_diameter < nozzle_schema.inlet_diameter

    def test_throat_equal_to_inlet_rejected(self) -> None:
        with pytest.raises(ValidationError, match="throat_diameter"):
            NozzleSchema(
                inlet_diameter=0.020,
                throat_diameter=0.020,
                divergent_angle=12,
                convergent_angle=45,
                expansion_ratio=8,
            )

    def test_throat_larger_than_inlet_rejected(self) -> None:
        with pytest.raises(ValidationError, match="throat_diameter"):
            NozzleSchema(
                inlet_diameter=0.010,
                throat_diameter=0.020,
                divergent_angle=12,
                convergent_angle=45,
                expansion_ratio=8,
            )

    def test_non_positive_dimensions_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NozzleSchema(
                inlet_diameter=0,
                throat_diameter=0.005,
                divergent_angle=12,
                convergent_angle=45,
                expansion_ratio=8,
            )

    @pytest.mark.parametrize("angle", [0, -5, 91])
    def test_angles_out_of_range_rejected(self, angle: float) -> None:
        with pytest.raises(ValidationError):
            NozzleSchema(
                inlet_diameter=0.060,
                throat_diameter=0.015,
                divergent_angle=angle,
                convergent_angle=45,
                expansion_ratio=8,
            )

    def test_expansion_ratio_must_be_at_least_one(self) -> None:
        with pytest.raises(ValidationError):
            NozzleSchema(
                inlet_diameter=0.060,
                throat_diameter=0.015,
                divergent_angle=12,
                convergent_angle=45,
                expansion_ratio=0.5,
            )

    def test_to_machwave_round_trips_fields(self, nozzle_schema: NozzleSchema) -> None:
        nozzle = nozzle_schema.to_machwave()
        assert nozzle.throat_diameter == nozzle_schema.throat_diameter
        assert nozzle.expansion_ratio == nozzle_schema.expansion_ratio


# ---------------------------------------------------------------------------
# Combustion chamber
# ---------------------------------------------------------------------------


class TestCombustionChamberSchema:
    def test_outer_must_exceed_inner(self) -> None:
        with pytest.raises(ValidationError, match="casing_outer_diameter"):
            CombustionChamberSchema(
                casing_inner_diameter=0.05,
                casing_outer_diameter=0.05,
                internal_length=0.28,
            )

    def test_negative_thermal_liner_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CombustionChamberSchema(
                casing_inner_diameter=0.05,
                casing_outer_diameter=0.06,
                internal_length=0.28,
                thermal_liner_thickness=-0.001,
            )

    def test_to_machwave_preserves_dimensions(
        self, chamber_schema: CombustionChamberSchema
    ) -> None:
        cc = chamber_schema.to_machwave()
        assert cc.casing_inner_diameter == chamber_schema.casing_inner_diameter
        assert cc.casing_outer_diameter == chamber_schema.casing_outer_diameter


# ---------------------------------------------------------------------------
# Bates segment / Grain
# ---------------------------------------------------------------------------


class TestBatesSegmentSchema:
    def test_valid_round_trip(self, bates_segment: BatesSegmentSchema) -> None:
        raw = bates_segment.model_dump()
        assert BatesSegmentSchema.model_validate(raw) == bates_segment

    def test_core_equal_to_outer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="core_diameter"):
            BatesSegmentSchema(outer_diameter=0.04, core_diameter=0.04, length=0.10)

    def test_core_larger_than_outer_rejected(self) -> None:
        with pytest.raises(ValidationError, match="core_diameter"):
            BatesSegmentSchema(outer_diameter=0.03, core_diameter=0.04, length=0.10)

    @pytest.mark.parametrize("ratio", [-0.1, 1.01])
    def test_density_ratio_out_of_range_rejected(self, ratio: float) -> None:
        with pytest.raises(ValidationError):
            BatesSegmentSchema(
                outer_diameter=0.04,
                core_diameter=0.02,
                length=0.10,
                density_ratio=ratio,
            )

    def test_type_discriminator_value_locked(self) -> None:
        """The ``type`` field is the discriminator — only ``"bates"`` is valid."""
        with pytest.raises(ValidationError):
            BatesSegmentSchema(
                type="star",  # type: ignore[arg-type]
                outer_diameter=0.04,
                core_diameter=0.02,
                length=0.10,
            )


class TestGrainSchema:
    def test_empty_segments_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GrainSchema(segments=[], spacing=0.005)

    def test_negative_spacing_rejected(self, bates_segment: BatesSegmentSchema) -> None:
        with pytest.raises(ValidationError):
            GrainSchema(segments=[bates_segment], spacing=-0.001)

    def test_to_machwave_preserves_segment_count(self, grain_schema: GrainSchema) -> None:
        grain = grain_schema.to_machwave()
        assert grain.segment_count == 2
        assert grain.spacing == grain_schema.spacing


# ---------------------------------------------------------------------------
# Solid motor config + thrust chamber
# ---------------------------------------------------------------------------


class TestSolidMotorThrustChamberSchema:
    def test_dry_mass_must_be_positive(
        self, nozzle_schema: NozzleSchema, chamber_schema: CombustionChamberSchema
    ) -> None:
        with pytest.raises(ValidationError):
            SolidMotorThrustChamberSchema(
                nozzle=nozzle_schema,
                combustion_chamber=chamber_schema,
                dry_mass=0,
                nozzle_exit_to_grain_port_distance=0.01,
            )

    def test_cog_optional(
        self, nozzle_schema: NozzleSchema, chamber_schema: CombustionChamberSchema
    ) -> None:
        tc = SolidMotorThrustChamberSchema(
            nozzle=nozzle_schema,
            combustion_chamber=chamber_schema,
            dry_mass=1.5,
            nozzle_exit_to_grain_port_distance=0.01,
        )
        assert tc.center_of_gravity_coordinate is None


class TestSolidMotorConfigSchema:
    def test_invalid_propellant_id_rejected(
        self,
        grain_schema: GrainSchema,
        thrust_chamber_schema: SolidMotorThrustChamberSchema,
    ) -> None:
        with pytest.raises(ValidationError, match="Unknown solid propellant_id"):
            SolidMotorConfigSchema(
                propellant_id="DEFINITELY_NOT_REAL",
                grain=grain_schema,
                thrust_chamber=thrust_chamber_schema,
            )

    def test_motor_type_default(self, motor_config_schema: SolidMotorConfigSchema) -> None:
        assert motor_config_schema.motor_type == "solid"

    def test_to_machwave(self, motor_config_schema: SolidMotorConfigSchema) -> None:
        from machwave.models.motors.solid import SolidMotor

        assert isinstance(motor_config_schema.to_machwave(), SolidMotor)


# ---------------------------------------------------------------------------
# Tank / feed system / injector / LRE
# ---------------------------------------------------------------------------


class TestTankSchema:
    def test_blank_fluid_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TankSchema(fluid_name="", volume=0.05, temperature=90.0, initial_fluid_mass=30.0)

    @pytest.mark.parametrize(
        "field,value",
        [("volume", 0), ("volume", -1), ("temperature", 0), ("initial_fluid_mass", 0)],
    )
    def test_non_positive_fields_rejected(self, field: str, value: float) -> None:
        kwargs = {
            "fluid_name": "Oxygen",
            "volume": 0.05,
            "temperature": 90.0,
            "initial_fluid_mass": 30.0,
        }
        kwargs[field] = value
        with pytest.raises(ValidationError):
            TankSchema(**kwargs)


class TestStackedTankPressureFedFeedSystemSchema:
    def test_negative_piston_loss_rejected(
        self, lre_fuel_tank: TankSchema, lre_oxidizer_tank: TankSchema
    ) -> None:
        with pytest.raises(ValidationError):
            StackedTankPressureFedFeedSystemSchema(
                oxidizer_line_diameter=0.012,
                oxidizer_line_length=0.5,
                fuel_line_diameter=0.012,
                fuel_line_length=0.5,
                fuel_tank=lre_fuel_tank,
                oxidizer_tank=lre_oxidizer_tank,
                piston_loss=-1.0,
            )

    def test_to_machwave_preserves_tanks(
        self, lre_feed_system: StackedTankPressureFedFeedSystemSchema
    ) -> None:
        feed = lre_feed_system.to_machwave()
        assert feed.fuel_tank.fluid_name == "Hydrogen"
        assert feed.oxidizer_tank.fluid_name == "Oxygen"


class TestBipropellantInjectorSchema:
    @pytest.mark.parametrize("cd", [0, 1.01, -0.1])
    def test_discharge_coefficient_bounds(self, cd: float) -> None:
        with pytest.raises(ValidationError):
            BipropellantInjectorSchema(
                discharge_coefficient_fuel=cd,
                discharge_coefficient_oxidizer=0.7,
                area_fuel=2e-5,
                area_ox=4e-5,
            )

    def test_to_machwave_areas_preserved(self, lre_injector: BipropellantInjectorSchema) -> None:
        inj = lre_injector.to_machwave()
        assert inj.area_fuel == pytest.approx(2.0e-5)
        assert inj.area_ox == pytest.approx(4.0e-5)


class TestLiquidEngineConfigSchema:
    def test_motor_type_default(self, lre_config: LiquidEngineConfigSchema) -> None:
        assert lre_config.motor_type == "liquid"

    def test_invalid_propellant_id_rejected(
        self,
        lre_thrust_chamber: LiquidEngineThrustChamberSchema,
        lre_feed_system: StackedTankPressureFedFeedSystemSchema,
    ) -> None:
        with pytest.raises(ValidationError, match="Unknown biliquid propellant_id"):
            LiquidEngineConfigSchema(
                propellant_id="DEFINITELY_NOT_REAL",
                thrust_chamber=lre_thrust_chamber,
                feed_system=lre_feed_system,
            )

    def test_json_round_trip(self, lre_config: LiquidEngineConfigSchema) -> None:
        raw = lre_config.model_dump(mode="json")
        assert LiquidEngineConfigSchema.model_validate(raw) == lre_config

    def test_to_machwave(self, lre_config: LiquidEngineConfigSchema) -> None:
        from machwave.models.motors.liquid import LiquidEngine

        engine = lre_config.to_machwave()
        assert isinstance(engine, LiquidEngine)
        assert engine.oxidizer_tank_cog == lre_config.oxidizer_tank_cog
        assert engine.fuel_tank_cog == lre_config.fuel_tank_cog


# ---------------------------------------------------------------------------
# Discriminated MotorConfigSchema
# ---------------------------------------------------------------------------


class TestMotorConfigDiscriminator:
    def test_solid_dispatches_to_solid_schema(
        self, motor_config_schema: SolidMotorConfigSchema
    ) -> None:
        adapter = TypeAdapter(MotorConfigSchema)
        restored = adapter.validate_python(motor_config_schema.model_dump(mode="json"))
        assert isinstance(restored, SolidMotorConfigSchema)

    def test_liquid_dispatches_to_liquid_schema(self, lre_config: LiquidEngineConfigSchema) -> None:
        adapter = TypeAdapter(MotorConfigSchema)
        restored = adapter.validate_python(lre_config.model_dump(mode="json"))
        assert isinstance(restored, LiquidEngineConfigSchema)

    def test_unknown_motor_type_rejected(self) -> None:
        adapter = TypeAdapter(MotorConfigSchema)
        with pytest.raises(ValidationError):
            adapter.validate_python({"motor_type": "hybrid", "propellant_id": "x"})


# ---------------------------------------------------------------------------
# MotorRecord / MotorSummary
# ---------------------------------------------------------------------------


class TestMotorRecord:
    def test_blank_name_rejected(self, motor_config_schema: SolidMotorConfigSchema) -> None:
        with pytest.raises(ValidationError):
            MotorRecord(motor_id="m1", name="", config=motor_config_schema)

    def test_long_name_rejected(self, motor_config_schema: SolidMotorConfigSchema) -> None:
        with pytest.raises(ValidationError):
            MotorRecord(motor_id="m1", name="x" * 101, config=motor_config_schema)

    def test_default_timestamps_within_recent_window(
        self, motor_config_schema: SolidMotorConfigSchema
    ) -> None:
        before = datetime.now(UTC)
        record = MotorRecord(motor_id="m1", name="Olympus", config=motor_config_schema)
        after = datetime.now(UTC) + timedelta(seconds=1)
        assert before - timedelta(seconds=1) <= record.created_at <= after
        assert record.created_at <= record.updated_at <= after

    def test_summary_round_trip(self) -> None:
        summary = MotorSummary(
            motor_id="m1",
            name="Olympus",
            motor_type="solid",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert MotorSummary.model_validate(summary.model_dump(mode="json")) == summary
