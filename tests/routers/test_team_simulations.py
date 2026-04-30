"""Tests for ``app.routers.team_simulations``.

The high-value invariants: team sims debit the team pool (not the user's),
refund logic uses ``charged_to``, the active-sim block is per-pool, and
existing dispatch tests still see ``("sim", "user")`` while team sims are
captured separately.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.account import AccountRepository
from app.repositories.team import TeamAccountRepository
from app.repositories.team_resources import TeamSimulationRepository
from tests.conftest import FakeGCS, login_as, make_team
from tests.routers.test_motors import _solid_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_team_with_motor(
    app: FastAPI,
    client: TestClient,
    fake_gcs: FakeGCS,
    *,
    team_id: str = "t1",
    user_id: str = "u1",
) -> str:
    """Seed a team with the user as owner and create one team motor.

    Returns the motor_id.
    """
    await make_team(fake_gcs, team_id=team_id, owner_uid=user_id)
    login_as(app, uid=user_id)
    create = client.post(
        f"/teams/{team_id}/motors",
        json={"name": "m", "config": _solid_config()},
    )
    assert create.status_code == 201
    return create.json()["motor_id"]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestTeamSimulationAccess:
    @pytest.mark.asyncio
    async def test_viewer_cannot_create_simulation(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        # Re-assign the caller to a viewer member.
        await make_team(
            fake_gcs,
            team_id="t1",
            owner_uid="u1",
            members=[("u2", "viewer")],
        )
        login_as(app, uid="u2")
        resp = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_non_member_gets_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        login_as(app, uid="stranger")
        resp = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Charging
# ---------------------------------------------------------------------------


class TestTeamSimulationCharging:
    @pytest.mark.asyncio
    async def test_team_simulation_debits_team_pool(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        # u1 must already exist as an account too — fetch it to capture its
        # current usage so we can assert it didn't move.
        user_account_repo = AccountRepository()
        await user_account_repo.get_or_create("u1")

        team_account_repo = TeamAccountRepository()
        before_team = await team_account_repo.get_or_create("t1")
        before_user = await user_account_repo.get_or_create("u1")

        resp = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        assert resp.status_code == 202
        estimated = resp.json()["estimated_tokens"]
        assert estimated > 0

        after_team = await team_account_repo.get_or_create("t1")
        after_user = await user_account_repo.get_or_create("u1")

        assert after_team.credits.tokens_used - before_team.credits.tokens_used == estimated
        assert after_user.credits.tokens_used == before_user.credits.tokens_used

        # Dispatch went to the team pool, recorded under ``team_calls``.
        assert dispatch_recorder.team_calls == [(resp.json()["simulation_id"], "t1")]
        assert dispatch_recorder.calls == []

    @pytest.mark.asyncio
    async def test_team_simulation_402_when_team_pool_empty(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        team_account_repo = TeamAccountRepository()
        await team_account_repo.update_limits("t1", {"monthly_token_limit": 0})

        # Personal account has plenty of credits — proves the check is on the
        # team pool, not the user's.
        resp = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        assert resp.status_code == 402
        assert "team tokens" in resp.json()["detail"].lower()
        assert dispatch_recorder.team_calls == []

    @pytest.mark.asyncio
    async def test_cost_record_is_charged_to_team(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        resp = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        sim_id = resp.json()["simulation_id"]

        cost_blob = fake_gcs.blobs[f"teams/t1/simulations/{sim_id}/cost.json"]
        assert cost_blob["charged_to"] == "team"


# ---------------------------------------------------------------------------
# Active-sim block scoping
# ---------------------------------------------------------------------------


class TestActiveSimulationBlockScoping:
    @pytest.mark.asyncio
    async def test_team_active_sim_does_not_block_personal_sim(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        team_motor_id = await _make_team_with_motor(app, client, fake_gcs)
        first = client.post("/teams/t1/simulations", json={"motor_id": team_motor_id})
        assert first.status_code == 202

        # Now create a personal motor and submit a personal sim. Should NOT be
        # blocked by the team sim still being pending.
        personal_motor = client.post("/motors", json={"name": "p", "config": _solid_config()})
        assert personal_motor.status_code == 201
        personal = client.post("/simulations", json={"motor_id": personal_motor.json()["motor_id"]})
        assert personal.status_code == 202

    @pytest.mark.asyncio
    async def test_second_team_sim_is_blocked_by_active_team_sim(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        first = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        assert first.status_code == 202

        second = client.post("/teams/t1/simulations", json={"motor_id": motor_id})
        assert second.status_code == 409
        assert "team simulation" in second.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Status / results / cost / delete / retry
# ---------------------------------------------------------------------------


class TestTeamSimulationLifecycle:
    @pytest.mark.asyncio
    async def test_status_returns_pending_after_create(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        sim_id = client.post("/teams/t1/simulations", json={"motor_id": motor_id}).json()[
            "simulation_id"
        ]

        resp = client.get(f"/teams/t1/simulations/{sim_id}/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    @pytest.mark.asyncio
    async def test_delete_removes_simulation(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        sim_id = client.post("/teams/t1/simulations", json={"motor_id": motor_id}).json()[
            "simulation_id"
        ]

        # Need the sim to be terminal to be deletable? No — delete is allowed
        # in any state for the user-scope router; mirrors that.
        resp = client.delete(f"/teams/t1/simulations/{sim_id}")
        assert resp.status_code == 204
        repo = TeamSimulationRepository()
        assert await repo.get_status("t1", sim_id) is None

    @pytest.mark.asyncio
    async def test_retry_blocked_while_sibling_active(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder,  # noqa: ANN001
    ) -> None:
        from app.schemas.simulation import SimulationStatusRecord

        motor_id = await _make_team_with_motor(app, client, fake_gcs)
        sim_id = client.post("/teams/t1/simulations", json={"motor_id": motor_id}).json()[
            "simulation_id"
        ]

        # Force the original to a terminal state so retry is otherwise
        # eligible, then plant a sibling pending sim by reusing the original
        # config — this trips the "another sim still active" guard in retry.
        repo = TeamSimulationRepository()
        await repo.append_status_event("t1", sim_id, "done")

        config = await repo.get_config("t1", sim_id)
        assert config is not None
        sibling_id = "sibling-sim"
        await repo.save_config(
            "t1",
            sibling_id,
            config.model_copy(update={"simulation_id": sibling_id}),
        )
        await repo.save_status("t1", sibling_id, SimulationStatusRecord(simulation_id=sibling_id))

        resp = client.post(f"/teams/t1/simulations/{sim_id}/retry")
        assert resp.status_code == 409
