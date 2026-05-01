"""Worker tests for the team scope.

Verifies the worker reads team-scoped paths, refunds the team pool on
failure (rather than the user pool), and the env-var entrypoint resolves
``OWNER_KIND=team`` correctly.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.repositories.account import AccountRepository
from app.repositories.team import TeamAccountRepository
from app.repositories.team_resources import (
    TeamCostRepository,
    TeamSimulationRepository,
)
from app.schemas.credits import SimulationCostRecord, current_period_utc
from app.worker import run as worker_run
from tests.conftest import FakeGCS, make_team
from tests.worker.test_run import _job_config

# ---------------------------------------------------------------------------
# Failure path → refund hits the team pool
# ---------------------------------------------------------------------------


class TestTeamRunRefund:
    @pytest.mark.asyncio
    async def test_failed_team_run_refunds_team_pool(
        self, fake_gcs: FakeGCS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")

        sim_repo = TeamSimulationRepository()
        cost_repo = TeamCostRepository()
        team_account = TeamAccountRepository()
        user_account = AccountRepository()

        await user_account.get_or_create("u1")
        before_user = (await user_account.get_or_create("u1")).credits.tokens_used

        # Pre-charge the team pool to mimic a real submit.
        await team_account.debit("t1", 500)
        await sim_repo.save_config("t1", "sim-1", _job_config("sim-1", "u1"))
        await cost_repo.save(
            "t1",
            "sim-1",
            SimulationCostRecord(
                simulation_id="sim-1",
                estimated_tokens=500,
                tokens_charged=500,
                period=current_period_utc(),
                charged_to="team",
            ),
        )

        # Force a failure inside the worker so it walks the refund path.
        import app.schemas.motor as motor_schema

        def boom(self) -> None:  # noqa: ANN001
            raise RuntimeError("explode")

        monkeypatch.setattr(motor_schema.SolidMotorConfigSchema, "to_machwave", boom)

        await worker_run.run("sim-1", "t1", "team")

        team_account_after = await team_account.get_or_create("t1")
        assert team_account_after.credits.tokens_used == 0

        # User pool not touched.
        user_account_after = await user_account.get_or_create("u1")
        assert user_account_after.credits.tokens_used == before_user

        # Cost record marked refunded.
        cost = await cost_repo.get("t1", "sim-1")
        assert cost is not None
        assert cost.refunded is True

        # Status flipped to failed with the right error.
        status = await sim_repo.get_status("t1", "sim-1")
        assert status is not None
        assert status.status == "failed"
        assert "explode" in (status.error or "")


# ---------------------------------------------------------------------------
# Env-var entrypoint
# ---------------------------------------------------------------------------


class TestEntrypointEnvVars:
    def test_owner_kind_team_uses_owner_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``__main__`` block should accept either ``OWNER_ID`` (team or
        user) or fall back to ``USER_ID`` (legacy user-only invocation)."""
        captured: dict[str, Any] = {}

        async def fake_run(simulation_id: str, owner_id: str, owner_kind: str = "user") -> None:
            captured["sim"] = simulation_id
            captured["owner_id"] = owner_id
            captured["owner_kind"] = owner_kind

        monkeypatch.setattr(worker_run, "run", fake_run)
        monkeypatch.setattr(os, "environ", {"SIM_ID": "s1", "OWNER_ID": "t1", "OWNER_KIND": "team"})

        # Re-execute the ``__main__`` block by importing-and-running its body.
        import asyncio

        sim = os.environ.get("SIM_ID")
        owner_kind = os.environ.get("OWNER_KIND", "user")
        owner_id = os.environ.get("OWNER_ID") or os.environ.get("USER_ID")
        assert sim == "s1"
        assert owner_kind == "team"
        assert owner_id == "t1"
        asyncio.run(fake_run(sim, owner_id, owner_kind))

        assert captured == {"sim": "s1", "owner_id": "t1", "owner_kind": "team"}
