"""Tests for the invite flow in ``app.routers.teams``.

Covers create / list / revoke from the team-scoped routes plus the
token-keyed accept namespace, including the membership-cap check on accept.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.repositories.team import TeamInviteRepository
from app.schemas.team import TeamInvite
from tests.conftest import ADMIN_UID, FakeGCS, login_as, logout, make_team

# ---------------------------------------------------------------------------
# Create / list / revoke
# ---------------------------------------------------------------------------


class TestCreateInvite:
    @pytest.mark.asyncio
    async def test_owner_can_create_invite(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        resp = client.post("/teams/t1/invites", json={"role": "editor"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["team_id"] == "t1"
        assert body["role"] == "editor"
        assert isinstance(body["token"], str) and len(body["token"]) > 0

    @pytest.mark.asyncio
    async def test_owner_invite_role_rejected(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        resp = client.post("/teams/t1/invites", json={"role": "owner"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_editor_cannot_invite(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u2")
        assert client.post("/teams/t1/invites", json={"role": "viewer"}).status_code == 403


class TestListAndRevokeInvites:
    @pytest.mark.asyncio
    async def test_list_then_revoke(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        token = client.post("/teams/t1/invites", json={"role": "editor"}).json()["token"]

        listed = client.get("/teams/t1/invites").json()
        assert len(listed) == 1 and listed[0]["token"] == token

        assert client.delete(f"/teams/t1/invites/{token}").status_code == 204
        assert client.get("/teams/t1/invites").json() == []

        # Index also gone — accept-by-token must now 404.
        logout(app)
        login_as(app, uid="u2")
        assert client.post(f"/invites/{token}/accept").status_code == 404


# ---------------------------------------------------------------------------
# Inspect / accept
# ---------------------------------------------------------------------------


class TestInspectInvite:
    @pytest.mark.asyncio
    async def test_inspect_shows_team_name_and_role(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", name="Foo")
        login_as(app, uid="u1")
        token = client.post("/teams/t1/invites", json={"role": "editor"}).json()["token"]

        logout(app)
        login_as(app, uid="invitee")
        resp = client.get(f"/invites/{token}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["team_name"] == "Foo"
        assert body["role"] == "editor"
        assert body["is_usable"] is True


class TestAcceptInvite:
    @pytest.mark.asyncio
    async def test_accept_adds_member(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        token = client.post("/teams/t1/invites", json={"role": "editor"}).json()["token"]

        logout(app)
        login_as(app, uid="invitee", email="invitee@example.com")
        resp = client.post(f"/invites/{token}/accept")
        assert resp.status_code == 201
        body = resp.json()
        assert body["user_id"] == "invitee"
        assert body["role"] == "editor"
        assert body["email"] == "invitee@example.com"

    @pytest.mark.asyncio
    async def test_accept_is_single_use(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        token = client.post("/teams/t1/invites", json={"role": "editor"}).json()["token"]

        logout(app)
        login_as(app, uid="invitee")
        assert client.post(f"/invites/{token}/accept").status_code == 201

        # A *different* user trying the same token after consumption.
        logout(app)
        login_as(app, uid="invitee2")
        resp = client.post(f"/invites/{token}/accept")
        assert resp.status_code == 409
        assert "used" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_accept_rejects_existing_member(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "viewer")])
        login_as(app, uid="u1")
        token = client.post("/teams/t1/invites", json={"role": "editor"}).json()["token"]

        logout(app)
        login_as(app, uid="u2")
        resp = client.post(f"/invites/{token}/accept")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_accept_rejects_revoked_invite(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        repo = TeamInviteRepository()
        await repo.save(
            TeamInvite(
                token="tok-revoked",  # noqa: S106
                team_id="t1",
                role="editor",
                created_by="u1",
                revoked=True,
            )
        )
        login_as(app, uid="invitee")
        resp = client.post("/invites/tok-revoked/accept")
        assert resp.status_code == 409
        assert "revoked" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_accept_rejects_expired_invite(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        repo = TeamInviteRepository()
        await repo.save(
            TeamInvite(
                token="tok-expired",  # noqa: S106
                team_id="t1",
                role="editor",
                created_by="u1",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        login_as(app, uid="invitee")
        resp = client.post("/invites/tok-expired/accept")
        assert resp.status_code == 409
        assert "expired" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_accept_blocked_at_team_membership_cap(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        # invitee already in 5 teams.
        for i in range(5):
            await make_team(fake_gcs, team_id=f"t{i}", owner_uid="invitee")

        await make_team(fake_gcs, team_id="t-extra", owner_uid="u1")
        login_as(app, uid="u1")
        token = client.post("/teams/t-extra/invites", json={"role": "editor"}).json()["token"]

        logout(app)
        login_as(app, uid="invitee")
        resp = client.post(f"/invites/{token}/accept")
        assert resp.status_code == 409
        assert "limit" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_admin_bypasses_membership_cap_on_accept(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        for i in range(5):
            await make_team(fake_gcs, team_id=f"t{i}", owner_uid=ADMIN_UID)

        await make_team(fake_gcs, team_id="t-extra", owner_uid="u1")
        login_as(app, uid="u1")
        token = client.post("/teams/t-extra/invites", json={"role": "editor"}).json()["token"]

        logout(app)
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.post(f"/invites/{token}/accept")
        assert resp.status_code == 201
