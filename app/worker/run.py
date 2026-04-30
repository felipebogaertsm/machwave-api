from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Literal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("machwave-worker")


OwnerKind = Literal["user", "team"]


async def run(simulation_id: str, owner_id: str, owner_kind: OwnerKind = "user") -> None:
    from datetime import UTC, datetime

    from app.credits.estimator import compute_actual_tokens
    from app.repositories.account import AccountRepository, InsufficientBalanceError
    from app.repositories.cost import CostRepository
    from app.repositories.simulation import SimulationRepository
    from app.repositories.team import (
        TeamAccountRepository,
        TeamInsufficientBalanceError,
    )
    from app.repositories.team_resources import (
        TeamCostRepository,
        TeamSimulationRepository,
    )
    from app.schemas.credits import current_period_utc
    from app.schemas.simulation import (
        LiquidSimulationResultsSchema,
        SimulationStatus,
        SolidSimulationResultsSchema,
    )

    # Pick the repo set up front so the rest of this function stays scope-blind.
    # Insufficient-balance is raised by either repo's debit; we catch the
    # union here.
    insufficient_errors: tuple[type[Exception], ...]
    if owner_kind == "team":
        sim_repo = TeamSimulationRepository()
        cost_repo = TeamCostRepository()
        team_account = TeamAccountRepository()
        user_account = AccountRepository()
        insufficient_errors = (TeamInsufficientBalanceError, InsufficientBalanceError)

        async def _read_status():
            return await sim_repo.get_status(owner_id, simulation_id)

        async def _set_status(status: SimulationStatus, error: str | None = None) -> None:
            await sim_repo.append_status_event(owner_id, simulation_id, status, error=error)
            logger.info("Status: %s  simulation_id=%s", status, simulation_id)

        async def _get_config():
            return await sim_repo.get_config(owner_id, simulation_id)

        async def _save_results(results) -> None:  # noqa: ANN001
            await sim_repo.save_results(owner_id, simulation_id, results)

        async def _get_cost():
            return await cost_repo.get(owner_id, simulation_id)

        async def _save_cost(record) -> None:  # noqa: ANN001
            await cost_repo.save(owner_id, simulation_id, record)

        async def _credit(charged_to: str, tokens: int) -> None:
            if charged_to == "team":
                await team_account.credit(owner_id, tokens)
            else:
                await user_account.credit(owner_id, tokens)

        async def _debit(charged_to: str, tokens: int) -> int:
            if charged_to == "team":
                return await team_account.debit(owner_id, tokens)
            return await user_account.debit(owner_id, tokens)
    else:
        sim_repo_u = SimulationRepository()
        cost_repo_u = CostRepository()
        user_account = AccountRepository()
        insufficient_errors = (InsufficientBalanceError,)

        async def _read_status():
            return await sim_repo_u.get_status(owner_id, simulation_id)

        async def _set_status(status: SimulationStatus, error: str | None = None) -> None:
            await sim_repo_u.append_status_event(owner_id, simulation_id, status, error=error)
            logger.info("Status: %s  simulation_id=%s", status, simulation_id)

        async def _get_config():
            return await sim_repo_u.get_config(owner_id, simulation_id)

        async def _save_results(results) -> None:  # noqa: ANN001
            await sim_repo_u.save_results(owner_id, simulation_id, results)

        async def _get_cost():
            return await cost_repo_u.get(owner_id, simulation_id)

        async def _save_cost(record) -> None:  # noqa: ANN001
            await cost_repo_u.save(owner_id, simulation_id, record)

        async def _credit(charged_to: str, tokens: int) -> None:
            # User-scope: only the user account can have been charged.
            await user_account.credit(owner_id, tokens)

        async def _debit(charged_to: str, tokens: int) -> int:
            return await user_account.debit(owner_id, tokens)

    async def fail(error: str) -> None:
        """Mark failed and refund the pre-charged tokens — failed runs cost nothing."""
        try:
            cost_record = await _get_cost()
            if (
                cost_record is not None
                and cost_record.actual_tokens is None
                and not cost_record.refunded
                and cost_record.tokens_charged > 0
                and cost_record.period == current_period_utc()
            ):
                await _credit(cost_record.charged_to, cost_record.tokens_charged)
                cost_record.refunded = True
                await _save_cost(cost_record)
        except Exception:
            logger.exception("Failed to refund tokens on failure path")
        await _set_status("failed", error=error)

    logger.info(
        "Starting worker  simulation_id=%s  owner_kind=%s  owner_id=%s",
        simulation_id,
        owner_kind,
        owner_id,
    )

    # 1. Read simulation config
    try:
        job_config = await _get_config()
        if job_config is None:
            raise ValueError(f"Config not found for simulation_id={simulation_id}")
    except Exception as exc:
        logger.exception("Failed to read simulation config")
        await fail(str(exc))
        sys.exit(1)

    # 2. Mark as running
    await _set_status("running")

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
        await _save_results(results)
    except Exception as exc:
        logger.exception("Failed to serialise/store results")
        await fail(str(exc))
        sys.exit(1)

    # 6. Post-charge: reconcile estimate against actual.
    # Best-effort: billing failure should not fail an otherwise-successful run.
    try:
        iteration_count = int(len(motor_state.t))
        actual = compute_actual_tokens(iteration_count)
        cost_record = await _get_cost()
        if cost_record is not None and cost_record.actual_tokens is None:
            estimate = cost_record.estimated_tokens
            delta = actual - estimate

            # Charge the overage when actual exceeds estimate. We never refund
            # the difference when actual is *less* than estimate — refunds
            # only happen on the failure path. The estimate is the floor.
            if delta > 0:
                try:
                    charged = await _debit(cost_record.charged_to, delta)
                    cost_record.tokens_charged += charged
                except insufficient_errors as exc:
                    logger.warning(
                        "Could not charge full overage of %d tokens for %s; "
                        "remaining=%d. Cost record reflects partial charge.",
                        delta,
                        simulation_id,
                        exc.remaining,
                    )
                    if exc.remaining > 0:
                        charged = await _debit(cost_record.charged_to, exc.remaining)
                        cost_record.tokens_charged += charged

            cost_record.actual_tokens = actual
            cost_record.iterations = iteration_count
            cost_record.completed_at = datetime.now(UTC)
            await _save_cost(cost_record)
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
    await _set_status("done")
    logger.info("Worker finished successfully for simulation_id=%s", simulation_id)


if __name__ == "__main__":
    simulation_id = os.environ.get("SIM_ID")
    raw_owner_kind = os.environ.get("OWNER_KIND", "user")
    if raw_owner_kind not in ("user", "team"):
        logger.error("OWNER_KIND must be 'user' or 'team'; got %r", raw_owner_kind)
        sys.exit(1)
    owner_kind: OwnerKind = raw_owner_kind  # type: ignore[assignment]
    # Prefer OWNER_ID; fall back to USER_ID for legacy invocations.
    owner_id = os.environ.get("OWNER_ID") or os.environ.get("USER_ID")

    if not simulation_id or not owner_id:
        logger.error("SIM_ID and OWNER_ID (or USER_ID) environment variables are required.")
        sys.exit(1)

    asyncio.run(run(simulation_id, owner_id, owner_kind))
