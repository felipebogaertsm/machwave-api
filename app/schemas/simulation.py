"""Pydantic v2 schemas for simulation jobs and results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.motor import MotorConfigSchema


class IBSimParamsSchema(BaseModel):
    """Internal ballistics simulation parameters."""

    model_config = ConfigDict(frozen=True)

    d_t: float = Field(
        default=0.01,
        gt=0,
        le=0.1,
        description="Integration time step [s]",
    )
    igniter_pressure: float = Field(
        default=1_000_000.0,
        gt=0,
        description="Initial igniter pressure [Pa]",
    )
    external_pressure: float = Field(
        default=101_325.0,
        gt=0,
        description="Ambient (external) pressure [Pa]",
    )
    other_losses: float = Field(
        default=12.0,
        ge=0.0,
        le=100.0,
        description="Additional efficiency losses not covered by specific models [%]",
    )

    def to_machwave(self):  # noqa: ANN201
        from machwave.simulation import InternalBallisticsSimulationParams

        return InternalBallisticsSimulationParams(
            d_t=self.d_t,
            igniter_pressure=self.igniter_pressure,
            external_pressure=self.external_pressure,
            other_losses=self.other_losses,
        )


class SimulationJobConfig(BaseModel):
    """Full simulation job config as stored in GCS.

    Embeds the motor config at submission time so the worker is self-contained
    even if the motor record is later modified or deleted.
    """

    simulation_id: str
    user_id: str
    motor_id: str
    motor_config: MotorConfigSchema
    params: IBSimParamsSchema
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

SimulationStatus = Literal["pending", "running", "done", "failed"]


class SimulationStatusRecord(BaseModel):
    """Status document stored at users/{user_id}/simulations/{simulation_id}/status.json."""

    simulation_id: str
    status: SimulationStatus = "pending"
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Results — one schema per motor type, discriminated on ``motor_type``
# ---------------------------------------------------------------------------


def _to_list(arr) -> list[float]:  # noqa: ANN001
    return [float(v) for v in np.asarray(arr)]


def _to_nested_list(arr) -> list:  # noqa: ANN001, ANN401
    return np.asarray(arr).tolist()


class SolidSimulationResultsSchema(BaseModel):
    """Serialised time-series results from a completed SRM simulation."""

    motor_type: Literal["solid"] = "solid"
    simulation_id: str

    # Time-series arrays
    t: list[float] = Field(description="Time [s]")
    thrust: list[float] = Field(description="Thrust [N]")
    P_0: list[float] = Field(description="Chamber stagnation pressure [Pa]")
    P_exit: list[float] = Field(description="Nozzle exit pressure [Pa]")
    m_prop: list[float] = Field(description="Propellant mass [kg]")
    burn_area: list[float] = Field(description="Instantaneous burn area [m²]")
    propellant_volume: list[float] = Field(description="Remaining propellant volume [m³]")
    free_chamber_volume: list[float] = Field(description="Free (gas) chamber volume [m³]")
    web: list[float] = Field(description="Web regression distance [m]")
    burn_rate: list[float] = Field(description="Instantaneous burn rate [m/s]")
    C_f: list[float] = Field(description="Thrust coefficient (corrected) [-]")
    C_f_ideal: list[float] = Field(description="Ideal thrust coefficient [-]")
    nozzle_efficiency: list[float] = Field(description="Overall nozzle efficiency [0–1]")
    overall_efficiency: list[float] = Field(description="Overall motor efficiency [0–1]")
    eta_div: list[float] = Field(description="Divergent nozzle efficiency factor [%]")
    eta_kin: list[float] = Field(description="Kinetics efficiency factor [%]")
    eta_bl: list[float] = Field(description="Boundary layer efficiency factor [%]")
    eta_2p: list[float] = Field(description="Two-phase flow efficiency factor [%]")
    grain_mass_flux: list[list[float]] = Field(
        description="Mass flux per grain segment per timestep [kg/(s·m²)]",
    )
    propellant_cog: list[list[float]] = Field(
        description="Propellant center of gravity per timestep, [x, y, z] [m]",
    )
    propellant_moi: list[list[list[float]]] = Field(
        description="Propellant moment of inertia tensor per timestep (3×3) [kg·m²]",
    )

    # Scalar summary metrics
    total_impulse: float = Field(description="Total impulse [N·s]")
    specific_impulse: float = Field(description="Specific impulse [s]")
    thrust_time: float = Field(description="Total thrust time [s]")
    burn_time: float = Field(description="Time at which propellant burned out [s]")
    max_thrust: float = Field(description="Peak thrust [N]")
    avg_thrust: float = Field(description="Average thrust [N]")
    max_chamber_pressure: float = Field(description="Peak chamber pressure [Pa]")
    avg_chamber_pressure: float = Field(description="Average chamber pressure [Pa]")
    avg_nozzle_efficiency: float = Field(description="Average overall nozzle efficiency [0–1]")
    avg_overall_efficiency: float = Field(description="Average overall motor efficiency [0–1]")
    initial_propellant_mass: float = Field(description="Initial propellant mass [kg]")
    volumetric_efficiency: float = Field(description="Volumetric efficiency [0–1]")
    mean_klemmung: float = Field(description="Mean Kn (burn area / throat area) [-]")
    max_klemmung: float = Field(description="Peak Kn [-]")
    initial_to_final_klemmung_ratio: float = Field(description="Initial/final Kn ratio [-]")
    max_mass_flux: float = Field(description="Peak grain mass flux [kg/(s·m²)]")
    burn_profile: str = Field(description="Burn profile: regressive / neutral / progressive")

    @classmethod
    def from_machwave(cls, simulation_id: str, motor_state) -> SolidSimulationResultsSchema:  # noqa: ANN001
        """Build from a machwave ``SolidMotorState`` after simulation.run()."""
        return cls(
            simulation_id=simulation_id,
            t=_to_list(motor_state.t),
            thrust=_to_list(motor_state.thrust),
            P_0=_to_list(motor_state.P_0),
            P_exit=_to_list(motor_state.P_exit),
            m_prop=_to_list(motor_state.m_prop),
            burn_area=_to_list(motor_state.burn_area),
            propellant_volume=_to_list(motor_state.propellant_volume),
            free_chamber_volume=_to_list(motor_state.V_0),
            web=_to_list(motor_state.web),
            burn_rate=_to_list(motor_state.burn_rate),
            C_f=_to_list(motor_state.C_f),
            C_f_ideal=_to_list(motor_state.C_f_ideal),
            nozzle_efficiency=_to_list(motor_state.nozzle_efficiency),
            overall_efficiency=_to_list(motor_state.overall_efficiency),
            eta_div=_to_list(motor_state.eta_div),
            eta_kin=_to_list(motor_state.eta_kin),
            eta_bl=_to_list(motor_state.eta_bl),
            eta_2p=_to_list(motor_state.eta_2p),
            grain_mass_flux=_to_nested_list(motor_state.grain_mass_flux),
            propellant_cog=_to_nested_list(motor_state.propellant_cog),
            propellant_moi=_to_nested_list(motor_state.propellant_moi),
            total_impulse=float(motor_state.total_impulse),
            specific_impulse=float(motor_state.specific_impulse),
            thrust_time=float(motor_state.thrust_time),
            burn_time=float(motor_state.burn_time),
            max_thrust=float(np.max(motor_state.thrust)),
            avg_thrust=float(np.mean(motor_state.thrust)),
            max_chamber_pressure=float(np.max(motor_state.P_0)),
            avg_chamber_pressure=float(np.mean(motor_state.P_0)),
            avg_nozzle_efficiency=float(np.mean(motor_state.nozzle_efficiency)),
            avg_overall_efficiency=float(np.mean(motor_state.overall_efficiency)),
            initial_propellant_mass=float(motor_state.initial_propellant_mass),
            volumetric_efficiency=float(motor_state.volumetric_efficiency),
            mean_klemmung=float(np.mean(motor_state.klemmung)),
            max_klemmung=float(np.max(motor_state.klemmung)),
            initial_to_final_klemmung_ratio=float(motor_state.initial_to_final_klemmung_ratio),
            max_mass_flux=float(motor_state.max_mass_flux),
            burn_profile=motor_state.burn_profile,
        )


class LiquidSimulationResultsSchema(BaseModel):
    """Serialised time-series results from a completed LRE simulation."""

    motor_type: Literal["liquid"] = "liquid"
    simulation_id: str

    # Time-series arrays
    t: list[float] = Field(description="Time [s]")
    thrust: list[float] = Field(description="Thrust [N]")
    P_0: list[float] = Field(description="Chamber stagnation pressure [Pa]")
    P_exit: list[float] = Field(description="Nozzle exit pressure [Pa]")
    m_prop: list[float] = Field(description="Total propellant mass remaining [kg]")
    fuel_mass: list[float] = Field(description="Fuel mass remaining [kg]")
    oxidizer_mass: list[float] = Field(description="Oxidizer mass remaining [kg]")
    fuel_tank_pressure: list[float] = Field(description="Fuel tank pressure [Pa]")
    oxidizer_tank_pressure: list[float] = Field(description="Oxidizer tank pressure [Pa]")
    C_f: list[float] = Field(description="Thrust coefficient (corrected) [-]")
    C_f_ideal: list[float] = Field(description="Ideal thrust coefficient [-]")
    n_cf: list[float] = Field(description="Thrust coefficient correction factor [-]")

    # Scalar summary metrics
    total_impulse: float = Field(description="Total impulse [N·s]")
    specific_impulse: float = Field(description="Specific impulse [s]")
    thrust_time: float = Field(description="Total thrust time [s]")
    burn_time: float | None = Field(
        default=None,
        description="Time at which a propellant tank emptied [s], if reached",
    )
    max_thrust: float = Field(description="Peak thrust [N]")
    avg_thrust: float = Field(description="Average thrust [N]")
    max_chamber_pressure: float = Field(description="Peak chamber pressure [Pa]")
    avg_chamber_pressure: float = Field(description="Average chamber pressure [Pa]")
    initial_propellant_mass: float = Field(description="Initial propellant mass [kg]")
    initial_oxidizer_mass: float = Field(description="Initial oxidizer mass [kg]")
    initial_fuel_mass: float = Field(description="Initial fuel mass [kg]")
    of_ratio: float = Field(description="Oxidizer-to-fuel mass ratio [-]")

    @classmethod
    def from_machwave(cls, simulation_id: str, motor_state) -> LiquidSimulationResultsSchema:  # noqa: ANN001
        """Build from a machwave ``LiquidEngineState`` after simulation.run()."""
        thrust = np.asarray(motor_state.thrust)
        t = np.asarray(motor_state.t)
        m_prop_initial = float(motor_state.m_prop[0])
        total_impulse = float(np.trapezoid(thrust, t))
        specific_impulse = total_impulse / (m_prop_initial * 9.81) if m_prop_initial > 0 else 0.0
        burn_time = getattr(motor_state, "burn_time", None)

        return cls(
            simulation_id=simulation_id,
            t=_to_list(motor_state.t),
            thrust=_to_list(motor_state.thrust),
            P_0=_to_list(motor_state.P_0),
            P_exit=_to_list(motor_state.P_exit),
            m_prop=_to_list(motor_state.m_prop),
            fuel_mass=_to_list(motor_state.fuel_mass),
            oxidizer_mass=_to_list(motor_state.oxidizer_mass),
            fuel_tank_pressure=_to_list(motor_state.fuel_tank_pressure),
            oxidizer_tank_pressure=_to_list(motor_state.oxidizer_tank_pressure),
            C_f=_to_list(motor_state.C_f),
            C_f_ideal=_to_list(motor_state.C_f_ideal),
            n_cf=_to_list(motor_state.n_cf),
            total_impulse=total_impulse,
            specific_impulse=specific_impulse,
            thrust_time=float(motor_state.thrust_time),
            burn_time=float(burn_time) if burn_time is not None else None,
            max_thrust=float(np.max(thrust)),
            avg_thrust=float(np.mean(thrust)),
            max_chamber_pressure=float(np.max(motor_state.P_0)),
            avg_chamber_pressure=float(np.mean(motor_state.P_0)),
            initial_propellant_mass=m_prop_initial,
            initial_oxidizer_mass=float(motor_state.oxidizer_mass[0]),
            initial_fuel_mass=float(motor_state.fuel_mass[0]),
            of_ratio=float(motor_state.motor.propellant.of_ratio),
        )


SimulationResultsSchema = Annotated[
    SolidSimulationResultsSchema | LiquidSimulationResultsSchema,
    Field(discriminator="motor_type"),
]


class SimulationDetailsResponse(BaseModel):
    """Full simulation payload for the frontend: results + the inputs that produced them."""

    simulation_id: str
    motor_id: str
    motor_config: MotorConfigSchema
    params: IBSimParamsSchema
    results: SimulationResultsSchema


class SimulationSummary(BaseModel):
    """Lightweight simulation listing item."""

    simulation_id: str
    motor_id: str
    motor_type: Literal["solid", "liquid"]
    status: SimulationStatus
    created_at: datetime
    updated_at: datetime
