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
    from datetime import UTC, datetime

    from app.credits.estimator import compute_actual_tokens
    from app.repositories.account import AccountRepository, InsufficientBalanceError
    from app.repositories.cost import CostRepository
    from app.repositories.simulation import SimulationRepository
    from app.schemas.credits import current_period_utc
    from app.schemas.simulation import (
        LiquidSimulationResultsSchema,
        SimulationStatus,
        SolidSimulationResultsSchema,
    )

    repo = SimulationRepository()
    cost_repo = CostRepository()
    account_repo = AccountRepository()

    async def set_status(status: SimulationStatus, error: str | None = None) -> None:
        await repo.append_status_event(user_id, simulation_id, status, error=error)
        logger.info("Status: %s  simulation_id=%s", status, simulation_id)

    async def fail(error: str) -> None:
        """Mark failed and refund the pre-charged tokens — failed runs cost nothing."""
        try:
            cost_record = await cost_repo.get(user_id, simulation_id)
            if (
                cost_record is not None
                and cost_record.actual_tokens is None
                and not cost_record.refunded
                and cost_record.tokens_charged > 0
                and cost_record.period == current_period_utc()
            ):
                await account_repo.credit(user_id, cost_record.tokens_charged)
                cost_record.refunded = True
                await cost_repo.save(user_id, simulation_id, cost_record)
        except Exception:
            logger.exception("Failed to refund tokens on failure path")
        await set_status("failed", error=error)

    logger.info("Starting worker  simulation_id=%s  user_id=%s", simulation_id, user_id)

    # 1. Read simulation config
    try:
        job_config = await repo.get_config(user_id, simulation_id)
        if job_config is None:
            raise ValueError(f"Config not found for simulation_id={simulation_id}")
    except Exception as exc:
        logger.exception("Failed to read simulation config")
        await fail(str(exc))
        sys.exit(1)

    # 2. Mark as running
    await set_status("running")

    # 3. Build machwave objects
    try:
        motor = job_config.motor_config.to_machwave()
        params = job_config.params.to_machwave()
    except Exception as exc:
        logger.exception("Failed to build machwave motor/params objects")
        await fail(str(exc))
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
        await fail(str(exc))
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
        await fail(str(exc))
        sys.exit(1)

    # 6. Post-charge: reconcile estimate against actual.
    # Best-effort: billing failure should not fail an otherwise-successful run.
    try:
        iteration_count = int(len(motor_state.t))
        actual = compute_actual_tokens(iteration_count)
        cost_record = await cost_repo.get(user_id, simulation_id)
        if cost_record is not None and cost_record.actual_tokens is None:
            estimate = cost_record.estimated_tokens
            delta = actual - estimate

            # Charge the overage when actual exceeds estimate. We never refund
            # the difference when actual is *less* than estimate — refunds
            # only happen on the failure path. The estimate is the floor.
            if delta > 0:
                try:
                    charged = await account_repo.debit(user_id, delta)
                    cost_record.tokens_charged += charged
                except InsufficientBalanceError as exc:
                    logger.warning(
                        "Could not charge full overage of %d tokens for %s; "
                        "remaining=%d. Cost record reflects partial charge.",
                        delta,
                        simulation_id,
                        exc.remaining,
                    )
                    if exc.remaining > 0:
                        charged = await account_repo.debit(user_id, exc.remaining)
                        cost_record.tokens_charged += charged

            cost_record.actual_tokens = actual
            cost_record.iterations = iteration_count
            cost_record.completed_at = datetime.now(UTC)
            await cost_repo.save(user_id, simulation_id, cost_record)
            logger.info(
                "Reconciled simulation_id=%s estimate=%d actual=%d charged=%d",
                simulation_id,
                estimate,
                actual,
                cost_record.tokens_charged,
            )
    except Exception:
        logger.exception("Post-charge bookkeeping failed (simulation results already saved)")

    # 7. Done
    await set_status("done")
    logger.info("Worker finished successfully for simulation_id=%s", simulation_id)


if __name__ == "__main__":
    simulation_id = os.environ.get("SIM_ID")
    user_id = os.environ.get("USER_ID")

    if not simulation_id or not user_id:
        logger.error("SIM_ID and USER_ID environment variables are required.")
        sys.exit(1)

    asyncio.run(run(simulation_id, user_id))
