from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from machwave.models.propellants.categories.biliquid import (
    BiliquidPropellant as _BiliquidPropellant,
)
from machwave.models.propellants.categories.solid import SolidPropellant as _SolidPropellant
from machwave.models.propellants.formulations import biliquid as _biliquid_formulations_module
from machwave.models.propellants.formulations import solid as _solid_formulations_module
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Shared sub-schemas (used by SRM and LRE)
# ---------------------------------------------------------------------------


class NozzleSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    inlet_diameter: float = Field(gt=0, description="Nozzle inlet diameter [m]")
    throat_diameter: float = Field(gt=0, description="Throat diameter [m]")
    divergent_angle: float = Field(gt=0, le=90, description="Divergent half-angle [°]")
    convergent_angle: float = Field(gt=0, le=90, description="Convergent half-angle [°]")
    expansion_ratio: float = Field(ge=1.0, description="Area expansion ratio Ae/At")
    c_1: float = Field(default=0.00506, description="Boundary layer loss coefficient c1")
    c_2: float = Field(default=0.0, description="Boundary layer loss coefficient c2")

    @model_validator(mode="after")
    def _throat_smaller_than_inlet(self) -> NozzleSchema:
        if self.throat_diameter >= self.inlet_diameter:
            raise ValueError("throat_diameter must be less than inlet_diameter")
        return self

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.thrust_chamber.nozzle import Nozzle

        return Nozzle(
            inlet_diameter=self.inlet_diameter,
            throat_diameter=self.throat_diameter,
            divergent_angle=self.divergent_angle,
            convergent_angle=self.convergent_angle,
            expansion_ratio=self.expansion_ratio,
            c_1=self.c_1,
            c_2=self.c_2,
        )


class CombustionChamberSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    casing_inner_diameter: float = Field(gt=0, description="Casing inner diameter [m]")
    casing_outer_diameter: float = Field(gt=0, description="Casing outer diameter [m]")
    internal_length: float = Field(gt=0, description="Chamber internal length [m]")
    thermal_liner_thickness: float = Field(
        default=0.0,
        ge=0.0,
        description="Thermal liner thickness [m]",
    )

    @model_validator(mode="after")
    def _outer_larger_than_inner(self) -> CombustionChamberSchema:
        if self.casing_outer_diameter <= self.casing_inner_diameter:
            raise ValueError("casing_outer_diameter must be greater than casing_inner_diameter")
        return self

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.thrust_chamber.combustion_chamber import CombustionChamber

        return CombustionChamber(
            casing_inner_diameter=self.casing_inner_diameter,
            casing_outer_diameter=self.casing_outer_diameter,
            internal_length=self.internal_length,
            thermal_liner_thickness=self.thermal_liner_thickness,
        )


# ---------------------------------------------------------------------------
# Solid Rocket Motor (SRM)
# ---------------------------------------------------------------------------


class BatesSegmentSchema(BaseModel):
    """Schema for a BATES cylindrical-port grain segment."""

    model_config = ConfigDict(frozen=True)

    type: Literal["bates"] = "bates"
    outer_diameter: float = Field(gt=0, description="Outer diameter [m]")
    core_diameter: float = Field(gt=0, description="Core (port) diameter [m]")
    length: float = Field(gt=0, description="Segment length [m]")
    density_ratio: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Packing density ratio (0–1)",
    )

    @model_validator(mode="after")
    def _core_smaller_than_outer(self) -> BatesSegmentSchema:
        if self.core_diameter >= self.outer_diameter:
            raise ValueError("core_diameter must be strictly less than outer_diameter")
        return self

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.grain.geometries.bates import BatesSegment

        return BatesSegment(
            outer_diameter=self.outer_diameter,
            core_diameter=self.core_diameter,
            length=self.length,
            density_ratio=self.density_ratio,
        )


# Discriminated union — extend with more segment types in future versions.
GrainSegmentSchema = Annotated[BatesSegmentSchema, Field(discriminator="type")]


class GrainSchema(BaseModel):
    """Schema for a multi-segment propellant grain assembly."""

    model_config = ConfigDict(frozen=True)

    segments: list[GrainSegmentSchema] = Field(
        min_length=1,
        description="Ordered list of grain segments (nozzle → bulkhead)",
    )
    spacing: float = Field(
        default=0.0,
        ge=0.0,
        description="Uniform inter-segment spacing [m]",
    )

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.grain.base import Grain

        grain = Grain(spacing=self.spacing)
        for seg in self.segments:
            grain.add_segment(seg.to_machwave())
        return grain


class SolidMotorThrustChamberSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    nozzle: NozzleSchema
    combustion_chamber: CombustionChamberSchema
    dry_mass: float = Field(gt=0, description="Dry mass of the thrust chamber [kg]")
    nozzle_exit_to_grain_port_distance: float = Field(
        ge=0.0,
        description="Axial distance from nozzle exit to grain port [m]",
    )
    center_of_gravity_coordinate: tuple[float, float, float] | None = Field(
        default=None,
        description="Dry-mass CoG position (x, y, z) from nozzle exit [m]",
    )

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.thrust_chamber.base import SolidMotorThrustChamber

        return SolidMotorThrustChamber(
            nozzle=self.nozzle.to_machwave(),
            combustion_chamber=self.combustion_chamber.to_machwave(),
            dry_mass=self.dry_mass,
            nozzle_exit_to_grain_port_distance=self.nozzle_exit_to_grain_port_distance,
            center_of_gravity_coordinate=self.center_of_gravity_coordinate,
        )


SOLID_FORMULATIONS: dict[str, _SolidPropellant] = {
    key: value
    for key, value in vars(_solid_formulations_module).items()
    if isinstance(value, _SolidPropellant)
}


class SolidMotorConfigSchema(BaseModel):
    """Full SRM configuration — stored inside every solid MotorRecord."""

    model_config = ConfigDict(frozen=True)

    motor_type: Literal["solid"] = "solid"
    propellant_id: str = Field(description="Built-in solid propellant identifier")
    grain: GrainSchema
    thrust_chamber: SolidMotorThrustChamberSchema

    @model_validator(mode="after")
    def _valid_propellant_id(self) -> SolidMotorConfigSchema:
        if self.propellant_id not in SOLID_FORMULATIONS:
            raise ValueError(
                f"Unknown solid propellant_id '{self.propellant_id}'. "
                f"Valid options: {sorted(SOLID_FORMULATIONS)}"
            )
        return self

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.motors.solid import SolidMotor

        return SolidMotor(
            grain=self.grain.to_machwave(),
            propellant=SOLID_FORMULATIONS[self.propellant_id],
            thrust_chamber=self.thrust_chamber.to_machwave(),
        )


# ---------------------------------------------------------------------------
# Liquid Rocket Engine (LRE)
# ---------------------------------------------------------------------------


class BipropellantInjectorSchema(BaseModel):
    """Schema mirroring ``machwave.models.thrust_chamber.injector.BipropellantInjector``."""

    model_config = ConfigDict(frozen=True)

    discharge_coefficient_fuel: float = Field(
        gt=0, le=1.0, description="Discharge coefficient for the fuel side [-]"
    )
    discharge_coefficient_oxidizer: float = Field(
        gt=0, le=1.0, description="Discharge coefficient for the oxidizer side [-]"
    )
    area_fuel: float = Field(gt=0, description="Effective fuel injector flow area [m²]")
    area_ox: float = Field(gt=0, description="Effective oxidizer injector flow area [m²]")

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.thrust_chamber.injector import BipropellantInjector

        return BipropellantInjector(
            discharge_coefficient_fuel=self.discharge_coefficient_fuel,
            discharge_coefficient_oxidizer=self.discharge_coefficient_oxidizer,
            area_fuel=self.area_fuel,
            area_ox=self.area_ox,
        )


class TankSchema(BaseModel):
    """Two-phase tank model — fluid identified by CoolProp name."""

    model_config = ConfigDict(frozen=True)

    fluid_name: str = Field(
        min_length=1,
        description="CoolProp fluid name (e.g. 'Oxygen', 'Hydrogen', 'N2O', 'Ethanol')",
    )
    volume: float = Field(gt=0, description="Internal tank volume [m³]")
    temperature: float = Field(gt=0, description="Tank temperature [K] (assumed constant)")
    initial_fluid_mass: float = Field(gt=0, description="Initial fluid mass in the tank [kg]")

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.feed_systems.tanks import Tank

        return Tank(
            fluid_name=self.fluid_name,
            volume=self.volume,
            temperature=self.temperature,
            initial_fluid_mass=self.initial_fluid_mass,
        )


class StackedTankPressureFedFeedSystemSchema(BaseModel):
    """Bipropellant stacked-tank pressure-fed feed system."""

    model_config = ConfigDict(frozen=True)

    type: Literal["stacked_tank_pressure_fed"] = "stacked_tank_pressure_fed"
    oxidizer_line_diameter: float = Field(gt=0, description="Oxidizer feedline diameter [m]")
    oxidizer_line_length: float = Field(gt=0, description="Oxidizer feedline length [m]")
    fuel_line_diameter: float = Field(gt=0, description="Fuel feedline diameter [m]")
    fuel_line_length: float = Field(gt=0, description="Fuel feedline length [m]")
    fuel_tank: TankSchema
    oxidizer_tank: TankSchema
    piston_loss: float = Field(
        default=0.0, ge=0.0, description="Pressure loss across the piston [Pa]"
    )

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.feed_systems.pressure_fed import (
            StackedTankPressureFedFeedSystem,
        )

        return StackedTankPressureFedFeedSystem(
            oxidizer_line_diameter=self.oxidizer_line_diameter,
            oxidizer_line_length=self.oxidizer_line_length,
            fuel_line_diameter=self.fuel_line_diameter,
            fuel_line_length=self.fuel_line_length,
            fuel_tank=self.fuel_tank.to_machwave(),
            oxidizer_tank=self.oxidizer_tank.to_machwave(),
            piston_loss=self.piston_loss,
        )


# Discriminated union — extend with more feed-system types in future versions.
FeedSystemSchema = Annotated[StackedTankPressureFedFeedSystemSchema, Field(discriminator="type")]


class LiquidEngineThrustChamberSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    nozzle: NozzleSchema
    injector: BipropellantInjectorSchema
    combustion_chamber: CombustionChamberSchema
    dry_mass: float = Field(gt=0, description="Dry mass of the thrust chamber [kg]")
    center_of_gravity_coordinate: tuple[float, float, float] | None = Field(
        default=None,
        description="Dry-mass CoG position (x, y, z) from nozzle exit [m]",
    )

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.thrust_chamber.base import LiquidEngineThrustChamber

        return LiquidEngineThrustChamber(
            nozzle=self.nozzle.to_machwave(),
            injector=self.injector.to_machwave(),
            combustion_chamber=self.combustion_chamber.to_machwave(),
            dry_mass=self.dry_mass,
            center_of_gravity_coordinate=self.center_of_gravity_coordinate,
        )


BILIQUID_FORMULATIONS: dict[str, _BiliquidPropellant] = {
    key: value
    for key, value in vars(_biliquid_formulations_module).items()
    if isinstance(value, _BiliquidPropellant)
}


class LiquidEngineConfigSchema(BaseModel):
    """Full LRE configuration — stored inside every liquid MotorRecord."""

    model_config = ConfigDict(frozen=True)

    motor_type: Literal["liquid"] = "liquid"
    propellant_id: str = Field(description="Built-in biliquid propellant identifier")
    thrust_chamber: LiquidEngineThrustChamberSchema
    feed_system: FeedSystemSchema
    oxidizer_tank_cog: float | None = Field(
        default=None,
        description="Axial position of the oxidizer tank CoG from nozzle exit [m]",
    )
    fuel_tank_cog: float | None = Field(
        default=None,
        description="Axial position of the fuel tank CoG from nozzle exit [m]",
    )

    @model_validator(mode="after")
    def _valid_propellant_id(self) -> LiquidEngineConfigSchema:
        if self.propellant_id not in BILIQUID_FORMULATIONS:
            raise ValueError(
                f"Unknown biliquid propellant_id '{self.propellant_id}'. "
                f"Valid options: {sorted(BILIQUID_FORMULATIONS)}"
            )
        return self

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.motors.liquid import LiquidEngine

        return LiquidEngine(
            propellant=BILIQUID_FORMULATIONS[self.propellant_id],
            thrust_chamber=self.thrust_chamber.to_machwave(),
            feed_system=self.feed_system.to_machwave(),
            oxidizer_tank_cog=self.oxidizer_tank_cog,
            fuel_tank_cog=self.fuel_tank_cog,
        )


# ---------------------------------------------------------------------------
# Discriminated motor config + records
# ---------------------------------------------------------------------------

MotorConfigSchema = Annotated[
    SolidMotorConfigSchema | LiquidEngineConfigSchema,
    Field(discriminator="motor_type"),
]


class MotorRecord(BaseModel):
    """Full motor record as stored in GCS (users/{uid}/motors/{motor_id}.json)."""

    motor_id: str
    name: str = Field(min_length=1, max_length=100)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    config: MotorConfigSchema


class MotorSummary(BaseModel):
    """Lightweight motor listing item — no full config."""

    motor_id: str
    name: str
    motor_type: Literal["solid", "liquid"]
    created_at: datetime
    updated_at: datetime
