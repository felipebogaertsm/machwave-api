"""Pydantic v2 schemas for simulation jobs and results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.motor import SolidMotorConfigSchema


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
    motor_config: SolidMotorConfigSchema
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


class SimulationResultsSchema(BaseModel):
    """Serialised time-series results from a completed SRM simulation."""

    simulation_id: str

    # Time-series arrays
    t: list[float] = Field(description="Time [s]")
    thrust: list[float] = Field(description="Thrust [N]")
    P_0: list[float] = Field(description="Chamber stagnation pressure [Pa]")
    P_exit: list[float] = Field(description="Nozzle exit pressure [Pa]")
    m_prop: list[float] = Field(description="Propellant mass [kg]")
    burn_area: list[float] = Field(description="Instantaneous burn area [m²]")
    propellant_volume: list[float] = Field(description="Remaining propellant volume [m³]")
    web: list[float] = Field(description="Web regression distance [m]")
    burn_rate: list[float] = Field(description="Instantaneous burn rate [m/s]")
    nozzle_efficiency: list[float] = Field(description="Overall nozzle efficiency [0–1]")
    overall_efficiency: list[float] = Field(description="Overall motor efficiency [0–1]")
    eta_div: list[float] = Field(description="Divergent nozzle efficiency factor [%]")
    eta_kin: list[float] = Field(description="Kinetics efficiency factor [%]")
    eta_bl: list[float] = Field(description="Boundary layer efficiency factor [%]")
    eta_2p: list[float] = Field(description="Two-phase flow efficiency factor [%]")

    # Scalar summary metrics
    total_impulse: float = Field(description="Total impulse [N·s]")
    specific_impulse: float = Field(description="Specific impulse [s]")
    thrust_time: float = Field(description="Total thrust time [s]")
    max_thrust: float = Field(description="Peak thrust [N]")
    avg_thrust: float = Field(description="Average thrust [N]")
    max_chamber_pressure: float = Field(description="Peak chamber pressure [Pa]")
    burn_profile: str = Field(description="Burn profile: regressive / neutral / progressive")

    @classmethod
    def from_machwave(cls, simulation_id: str, motor_state) -> SimulationResultsSchema:  # noqa: ANN001
        """Build from a machwave ``SolidMotorState`` after simulation.run()."""

        def _to_list(arr) -> list[float]:  # noqa: ANN001
            return [float(v) for v in np.asarray(arr)]

        return cls(
            simulation_id=simulation_id,
            t=_to_list(motor_state.t),
            thrust=_to_list(motor_state.thrust),
            P_0=_to_list(motor_state.P_0),
            P_exit=_to_list(motor_state.P_exit),
            m_prop=_to_list(motor_state.m_prop),
            burn_area=_to_list(motor_state.burn_area),
            propellant_volume=_to_list(motor_state.propellant_volume),
            web=_to_list(motor_state.web),
            burn_rate=_to_list(motor_state.burn_rate),
            nozzle_efficiency=_to_list(motor_state.nozzle_efficiency),
            overall_efficiency=_to_list(motor_state.overall_efficiency),
            eta_div=_to_list(motor_state.eta_div),
            eta_kin=_to_list(motor_state.eta_kin),
            eta_bl=_to_list(motor_state.eta_bl),
            eta_2p=_to_list(motor_state.eta_2p),
            total_impulse=float(motor_state.total_impulse),
            specific_impulse=float(motor_state.specific_impulse),
            thrust_time=float(motor_state.thrust_time),
            max_thrust=float(np.max(motor_state.thrust)),
            avg_thrust=float(np.mean(motor_state.thrust)),
            max_chamber_pressure=float(np.max(motor_state.P_0)),
            burn_profile=motor_state.burn_profile,
        )


class SimulationSummary(BaseModel):
    """Lightweight simulation listing item."""

    simulation_id: str
    motor_id: str
    status: SimulationStatus
    created_at: datetime
    updated_at: datetime
