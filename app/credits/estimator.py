"""Cost estimation engine.

Two entry points: ``estimate_tokens`` runs at submit time from the job
config; ``compute_actual_tokens`` runs in the worker once the real iteration
count is known. Both convert iteration count → tokens via the same shared
formula so estimate and actual are directly comparable.

Solid motor estimate
--------------------

For solids we solve the steady-state chamber pressure (Saint-Robert's law
balanced against nozzle mass-flow) using each propellant's pressure-segmented
burn-rate map and thermochemical properties (γ, T_c, M_w → c*), then divide
the largest grain web by the resulting burn rate. This lands within ~5–10%
of actual machwave runs for typical sugar propellants — vs. the previous
``web ÷ 5 mm/s`` heuristic which over-estimated by 50–80%.

Liquid motor estimate
---------------------

Still a placeholder — total propellant mass ÷ nominal mass flow. Replace
with a feed-system steady-state solver when liquid telemetry warrants it.

Overshoot
---------

A small safety factor (``OVERSHOOT``) inflates the burn time so the
pre-charge sits slightly above actual on average. This keeps the user from
being credited fractional tokens after a reconcile and makes "out of tokens"
errors deterministic at submit time.

Pricing constant — 1 token per integration step. Adjust by editing
``TOKENS_PER_ITERATION``; existing cost records are not back-filled, so any
price change is forward-only.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.schemas.motor import (
    SOLID_FORMULATIONS,
    BatesSegmentSchema,
    MotorConfigSchema,
    SolidMotorConfigSchema,
)
from app.schemas.simulation import IBSimParamsSchema, SimulationJobConfig

logger = logging.getLogger(__name__)


_R_UNIVERSAL = 8.314  # J/(mol·K)

# Fallbacks used when the steady-state inputs aren't available (unknown
# propellant id, missing burn-rate map / thermochemistry, etc).
SOLID_FALLBACK_BURN_RATE_M_S = 0.008  # ~8 mm/s, reasonable for sugar propellants
SOLID_FALLBACK_DENSITY_KG_M3 = 1750.0  # KNSB-ish
SOLID_FALLBACK_C_STAR_M_S = 900.0  # KNSB-ish

LIQUID_NOMINAL_MASS_FLOW_KG_S = 1.0  # placeholder for pressure-fed bipropellants

# 1 token per integration step.
TOKENS_PER_ITERATION = 1

# Slight inflation of the estimated burn time so the charge lands above the
# eventual actual on average. Tune with telemetry — current target is ~5–10%
# overshoot for typical KNSB motors.
OVERSHOOT = 1.10


# ---------------------------------------------------------------------------
# Solid: steady-state burn-time model
# ---------------------------------------------------------------------------


def _bates_initial_burn_area(seg: BatesSegmentSchema) -> float:
    """Inner cylindrical surface + 2 annular ends. Ignores inhibition geometry
    — close enough for the initial-Kn estimate."""
    inner = math.pi * seg.core_diameter * seg.length
    end = math.pi * (seg.outer_diameter**2 - seg.core_diameter**2) / 4
    return inner + 2 * end


def _max_web(motor_config: SolidMotorConfigSchema) -> float:
    """The web on the slowest-burning segment dominates total burn time."""
    return max((seg.outer_diameter - seg.core_diameter) / 2 for seg in motor_config.grain.segments)


def _initial_burn_area(motor_config: SolidMotorConfigSchema) -> float:
    return sum(_bates_initial_burn_area(seg) for seg in motor_config.grain.segments)


def _throat_area(motor_config: SolidMotorConfigSchema) -> float:
    d = motor_config.thrust_chamber.nozzle.throat_diameter
    return math.pi * d**2 / 4


def _propellant_density(propellant: Any) -> float:
    try:
        return float(propellant.ideal_density)
    except Exception:
        return SOLID_FALLBACK_DENSITY_KG_M3


def _propellant_c_star(propellant: Any) -> float:
    """Compute c* from γ_chamber, adiabatic flame temperature, and chamber
    molecular weight. Falls back to a generic sugar-propellant value when
    properties are missing."""
    props = getattr(propellant, "properties", None)
    if props is None:
        return SOLID_FALLBACK_C_STAR_M_S
    try:
        gamma = float(props.gamma_chamber)
        t_c = float(props.adiabatic_flame_temperature)
        m_w = float(props.molecular_weight_chamber)
        r_specific = _R_UNIVERSAL / m_w
        # Vandenkerckhove (Γ-style) c* = sqrt(R T_c) / sqrt(γ * (2/(γ+1))^((γ+1)/(γ-1)))
        ratio = (2 / (gamma + 1)) ** ((gamma + 1) / (gamma - 1))
        return math.sqrt(r_specific * t_c) / math.sqrt(gamma * ratio)
    except Exception:
        return SOLID_FALLBACK_C_STAR_M_S


def _equilibrium_pressure_pa(
    *,
    density: float,
    kn: float,
    c_star: float,
    burn_rate_map: list[dict[str, float | int]],
) -> float | None:
    """Find the equilibrium chamber pressure by walking the propellant's
    burn-rate map.

    Each map entry covers a pressure range and supplies Saint-Robert
    coefficients ``a`` (in mm/s/MPa^n) and ``n``. Mass-flow balance gives:

        ρ · A_burn · r = P · A_throat / c*
        ⇒ P_MPa^(1-n) = ρ · a · c* · Kn · 1e-9

    A range is the solution iff its closed-form P_MPa lands inside its own
    [min, max] pressure window. Returns the first such P (in Pa), or None.
    """
    for entry in burn_rate_map:
        a = float(entry["a"])
        n = float(entry["n"])
        if n >= 1.0:  # singular / unphysical
            continue
        rhs = density * a * c_star * kn * 1e-9
        if rhs <= 0:
            continue
        p_mpa = rhs ** (1 / (1 - n))
        p_pa = p_mpa * 1e6
        if float(entry["min"]) <= p_pa <= float(entry["max"]):
            return p_pa
    return None


def _solid_burn_time(motor_config: SolidMotorConfigSchema) -> float:
    web = _max_web(motor_config)

    propellant = SOLID_FORMULATIONS.get(motor_config.propellant_id)
    burn_rate_map = getattr(propellant, "burn_rate_map", None) if propellant else None
    if not propellant or not burn_rate_map:
        return web / SOLID_FALLBACK_BURN_RATE_M_S

    a_burn = _initial_burn_area(motor_config)
    a_throat = _throat_area(motor_config)
    if a_throat <= 0 or a_burn <= 0:
        return web / SOLID_FALLBACK_BURN_RATE_M_S

    kn = a_burn / a_throat
    p_eq = _equilibrium_pressure_pa(
        density=_propellant_density(propellant),
        kn=kn,
        c_star=_propellant_c_star(propellant),
        burn_rate_map=burn_rate_map,
    )
    if p_eq is None:
        logger.debug(
            "No equilibrium pressure found for %s (Kn=%.0f); falling back",
            motor_config.propellant_id,
            kn,
        )
        return web / SOLID_FALLBACK_BURN_RATE_M_S

    try:
        r = float(propellant.get_burn_rate(p_eq))
    except Exception:
        return web / SOLID_FALLBACK_BURN_RATE_M_S

    if r <= 0:
        return web / SOLID_FALLBACK_BURN_RATE_M_S

    return web / r


# ---------------------------------------------------------------------------
# Liquid: still rough — placeholder for a future feed-system solver
# ---------------------------------------------------------------------------


def _liquid_burn_time(motor_config: Any) -> float:
    feed = motor_config.feed_system
    total_mass = feed.fuel_tank.initial_fluid_mass + feed.oxidizer_tank.initial_fluid_mass
    return total_mass / LIQUID_NOMINAL_MASS_FLOW_KG_S


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _estimate_burn_time(motor_config: MotorConfigSchema) -> float:
    if motor_config.motor_type == "solid":
        return _solid_burn_time(motor_config)
    return _liquid_burn_time(motor_config)


def _iterations_to_tokens(iterations: float) -> int:
    if iterations <= 0:
        return 1
    return max(1, math.ceil(iterations * TOKENS_PER_ITERATION))


def estimate_iterations(motor_config: MotorConfigSchema, params: IBSimParamsSchema) -> int:
    burn_time = _estimate_burn_time(motor_config) * OVERSHOOT
    return max(1, math.ceil(burn_time / params.d_t))


def estimate_tokens(job_config: SimulationJobConfig) -> int:
    iterations = estimate_iterations(job_config.motor_config, job_config.params)
    return _iterations_to_tokens(iterations)


def compute_actual_tokens(iteration_count: int) -> int:
    return _iterations_to_tokens(iteration_count)
