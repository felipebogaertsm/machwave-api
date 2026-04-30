"""Tests for ``app.routers.team_motors``."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.team import TeamAccountRepository
from tests.conftest import FakeGCS, login_as, make_team
from tests.routers.test_motors import _solid_config


class TestTeamMotorAccess:
    @pytest.mark.asyncio
    async def test_non_member_gets_404(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="stranger")
        assert client.get("/teams/t1/motors").status_code == 404

    @pytest.mark.asyncio
    async def test_viewer_can_list_but_not_create(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "viewer")])
        login_as(app, uid="u2")
        assert client.get("/teams/t1/motors").status_code == 200
        resp = client.post("/teams/t1/motors", json={"name": "m", "config": _solid_config()})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_editor_can_full_crud(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u2")

        create = client.post("/teams/t1/motors", json={"name": "m", "config": _solid_config()})
        assert create.status_code == 201
        motor_id = create.json()["motor_id"]

        assert client.get(f"/teams/t1/motors/{motor_id}").status_code == 200
        assert (
            client.put(f"/teams/t1/motors/{motor_id}", json={"name": "renamed"}).status_code == 200
        )
        assert client.delete(f"/teams/t1/motors/{motor_id}").status_code == 204


class TestTeamMotorLimit:
    @pytest.mark.asyncio
    async def test_limit_is_enforced_against_team_account(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        repo = TeamAccountRepository()
        await repo.update_limits("t1", {"motor_limit": 1})

        login_as(app, uid="u1")
        first = client.post("/teams/t1/motors", json={"name": "m1", "config": _solid_config()})
        assert first.status_code == 201

        second = client.post("/teams/t1/motors", json={"name": "m2", "config": _solid_config()})
        assert second.status_code == 409
        assert "limit" in second.json()["detail"].lower()
