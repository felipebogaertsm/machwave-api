from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("machwave-worker")


async def run(simulation_id: str, user_id: str) -> None:
    from app.repositories.simulation import SimulationRepository
    from app.schemas.simulation import (
        LiquidSimulationResultsSchema,
        SimulationStatus,
        SimulationStatusRecord,
        SolidSimulationResultsSchema,
    )

    repo = SimulationRepository()

    async def set_status(status: SimulationStatus, error: str | None = None) -> None:
        record = SimulationStatusRecord(simulation_id=simulation_id, status=status, error=error)
        await repo.save_status(user_id, simulation_id, record)
        logger.info("Status: %s  simulation_id=%s", status, simulation_id)

    logger.info("Starting worker  simulation_id=%s  user_id=%s", simulation_id, user_id)

    # 1. Read simulation config
    try:
        job_config = await repo.get_config(user_id, simulation_id)
        if job_config is None:
            raise ValueError(f"Config not found for simulation_id={simulation_id}")
    except Exception as exc:
        logger.exception("Failed to read simulation config")
        await set_status("failed", error=str(exc))
        sys.exit(1)

    # 2. Mark as running
    await set_status("running")

    # 3. Build machwave objects
    try:
        motor = job_config.motor_config.to_machwave()
        params = job_config.params.to_machwave()
    except Exception as exc:
        logger.exception("Failed to build machwave motor/params objects")
        await set_status("failed", error=str(exc))
        sys.exit(1)

    # 4. Run simulation
    try:
        from machwave.simulation import InternalBallisticsSimulation
        from machwave.states.liquid_engine import LiquidEngineState
        from machwave.states.solid_motor import SolidMotorState

        sim = InternalBallisticsSimulation(motor=motor, params=params)
        _t, motor_state = sim.run()
        logger.info(
            "Simulation complete: thrust_time=%.3f s",
            motor_state.thrust_time,
        )
    except Exception as exc:
        logger.exception("Simulation failed")
        await set_status("failed", error=str(exc))
        sys.exit(1)

    # 5. Store results
    try:
        if isinstance(motor_state, SolidMotorState):
            results = SolidSimulationResultsSchema.from_machwave(simulation_id, motor_state)
        elif isinstance(motor_state, LiquidEngineState):
            results = LiquidSimulationResultsSchema.from_machwave(simulation_id, motor_state)
        else:
            raise TypeError(f"Unsupported motor state type: {type(motor_state).__name__}")
        await repo.save_results(user_id, simulation_id, results)
    except Exception as exc:
        logger.exception("Failed to serialise/store results")
        await set_status("failed", error=str(exc))
        sys.exit(1)

    # 6. Done
    await set_status("done")
    logger.info("Worker finished successfully for simulation_id=%s", simulation_id)


if __name__ == "__main__":
    simulation_id = os.environ.get("SIM_ID")
    user_id = os.environ.get("USER_ID")

    if not simulation_id or not user_id:
        logger.error("SIM_ID and USER_ID environment variables are required.")
        sys.exit(1)

    asyncio.run(run(simulation_id, user_id))
