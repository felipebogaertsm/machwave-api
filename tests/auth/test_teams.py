"""Tests for ``app.auth.teams.require_team_role``."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.teams import require_team_role
from app.repositories.team import TeamMembershipRepository
from app.schemas.team import TeamMembership
from tests.conftest import FakeGCS, login_as


@pytest.fixture()
def auth_app() -> FastAPI:
    application = FastAPI()

    @application.get("/teams/{team_id}/viewer")
    async def viewer_route(
        team_id: str,
        m: TeamMembership = Depends(require_team_role("viewer")),
    ) -> dict[str, Any]:
        return {"team_id": team_id, "role": m.role}

    @application.get("/teams/{team_id}/editor")
    async def editor_route(
        team_id: str,
        m: TeamMembership = Depends(require_team_role("editor")),
    ) -> dict[str, Any]:
        return {"team_id": team_id, "role": m.role}

    @application.get("/teams/{team_id}/owner")
    async def owner_route(
        team_id: str,
        m: TeamMembership = Depends(require_team_role("owner")),
    ) -> dict[str, Any]:
        return {"team_id": team_id, "role": m.role}

    return application


@pytest.fixture()
def auth_client(auth_app: FastAPI) -> TestClient:
    return TestClient(auth_app)


async def _seed_member(fake_gcs: FakeGCS, team_id: str, user_id: str, role: str) -> None:
    repo = TeamMembershipRepository()
    await repo.save(
        TeamMembership(team_id=team_id, user_id=user_id, role=role)  # type: ignore[arg-type]
    )


class TestRequireTeamRole:
    def test_non_member_gets_404(
        self, auth_app: FastAPI, auth_client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(auth_app, uid="stranger-uid")
        resp = auth_client.get("/teams/t1/viewer")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_viewer_passes_viewer_gate(
        self, auth_app: FastAPI, auth_client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await _seed_member(fake_gcs, "t1", "u1", "viewer")
        login_as(auth_app, uid="u1")
        resp = auth_client.get("/teams/t1/viewer")
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"

    @pytest.mark.asyncio
    async def test_viewer_blocked_from_editor_gate(
        self, auth_app: FastAPI, auth_client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await _seed_member(fake_gcs, "t1", "u1", "viewer")
        login_as(auth_app, uid="u1")
        resp = auth_client.get("/teams/t1/editor")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_editor_passes_editor_and_viewer_gates(
        self, auth_app: FastAPI, auth_client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await _seed_member(fake_gcs, "t1", "u1", "editor")
        login_as(auth_app, uid="u1")
        assert auth_client.get("/teams/t1/editor").status_code == 200
        assert auth_client.get("/teams/t1/viewer").status_code == 200

    @pytest.mark.asyncio
    async def test_editor_blocked_from_owner_gate(
        self, auth_app: FastAPI, auth_client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await _seed_member(fake_gcs, "t1", "u1", "editor")
        login_as(auth_app, uid="u1")
        resp = auth_client.get("/teams/t1/owner")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_owner_passes_every_gate(
        self, auth_app: FastAPI, auth_client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await _seed_member(fake_gcs, "t1", "u1", "owner")
        login_as(auth_app, uid="u1")
        assert auth_client.get("/teams/t1/viewer").status_code == 200
        assert auth_client.get("/teams/t1/editor").status_code == 200
        assert auth_client.get("/teams/t1/owner").status_code == 200

    def test_dependency_callable_is_cached_per_role(self) -> None:
        assert require_team_role("viewer") is require_team_role("viewer")
        assert require_team_role("viewer") is not require_team_role("owner")
