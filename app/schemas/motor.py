from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from machwave.models.propellants.categories.solid import SolidPropellant as _SolidPropellant
from machwave.models.propellants.formulations import solid as _solid_formulations_module
from pydantic import BaseModel, ConfigDict, Field, model_validator


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

PROPELLANT_IDS = frozenset(SOLID_FORMULATIONS)


class SolidMotorConfigSchema(BaseModel):
    """Full SRM configuration — stored inside every MotorRecord."""

    model_config = ConfigDict(frozen=True)

    propellant_id: str = Field(description="Built-in propellant identifier")
    grain: GrainSchema
    thrust_chamber: SolidMotorThrustChamberSchema

    @model_validator(mode="after")
    def _valid_propellant_id(self) -> SolidMotorConfigSchema:
        if self.propellant_id not in PROPELLANT_IDS:
            raise ValueError(
                f"Unknown propellant_id '{self.propellant_id}'. "
                f"Valid options: {sorted(PROPELLANT_IDS)}"
            )
        return self

    def to_machwave(self):  # noqa: ANN201
        from machwave.models.motors.solid import SolidMotor

        propellant = _resolve_propellant(self.propellant_id)
        return SolidMotor(
            grain=self.grain.to_machwave(),
            propellant=propellant,
            thrust_chamber=self.thrust_chamber.to_machwave(),
        )


def _resolve_propellant(propellant_id: str):  # noqa: ANN201
    """Return the machwave SolidPropellant instance for a catalogue identifier."""
    if propellant_id not in SOLID_FORMULATIONS:
        raise ValueError(f"Unknown propellant_id: {propellant_id!r}")
    return SOLID_FORMULATIONS[propellant_id]


class MotorRecord(BaseModel):
    """Full motor record as stored in GCS (users/{uid}/motors/{motor_id}.json)."""

    motor_id: str
    name: str = Field(min_length=1, max_length=100)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    config: SolidMotorConfigSchema


class MotorSummary(BaseModel):
    """Lightweight motor listing item — no full config."""

    motor_id: str
    name: str
    created_at: datetime
    updated_at: datetime
