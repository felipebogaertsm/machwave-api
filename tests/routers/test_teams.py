"""Tests for ``app.routers.teams`` — CRUD, membership, account/usage, admin."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_UID, MEMBER_UID, FakeGCS, login_as, make_team


def _team_id(resp_json: dict) -> str:
    return resp_json["team_id"]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCreateTeam:
    def test_creates_team_with_caller_as_owner(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid="u1")
        resp = client.post("/teams", json={"name": "My Team"})
        assert resp.status_code == 201
        team_id = _team_id(resp.json())

        # Caller appears as owner.
        members_resp = client.get(f"/teams/{team_id}/members")
        assert members_resp.status_code == 200
        members = members_resp.json()
        assert len(members) == 1
        assert members[0]["user_id"] == "u1"
        assert members[0]["role"] == "owner"

    def test_seeds_team_account(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, uid="u1")
        resp = client.post("/teams", json={"name": "My Team"})
        team_id = _team_id(resp.json())

        # Account was materialised with default caps.
        acct = client.get(f"/teams/{team_id}/account")
        assert acct.status_code == 200
        assert acct.json()["credits"]["monthly_token_limit"] == 100_000

    @pytest.mark.asyncio
    async def test_member_blocked_at_team_membership_cap(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        # Seed five team memberships up-front; the next create should 409.
        for i in range(5):
            await make_team(fake_gcs, team_id=f"t{i}", owner_uid="u1", name=f"Team {i}")
        login_as(app, uid="u1")
        resp = client.post("/teams", json={"name": "Sixth Team"})
        assert resp.status_code == 409
        assert "limit" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_admin_bypasses_team_membership_cap(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        for i in range(5):
            await make_team(fake_gcs, team_id=f"t{i}", owner_uid=ADMIN_UID, name=f"Team {i}")
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.post("/teams", json={"name": "Sixth Team"})
        assert resp.status_code == 201


class TestListMyTeams:
    @pytest.mark.asyncio
    async def test_lists_teams_caller_belongs_to(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", name="Alpha")
        await make_team(fake_gcs, team_id="t2", owner_uid="u2", members=[("u1", "viewer")])
        await make_team(fake_gcs, team_id="t3", owner_uid="u3", name="Other")

        login_as(app, uid="u1")
        resp = client.get("/teams")
        assert resp.status_code == 200
        teams = {t["team_id"]: t["role"] for t in resp.json()}
        assert teams == {"t1": "owner", "t2": "viewer"}

    def test_returns_empty_for_user_in_no_teams(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid="lonely")
        assert client.get("/teams").json() == []


class TestUpdateTeam:
    @pytest.mark.asyncio
    async def test_owner_can_update_name(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        resp = client.patch("/teams/t1", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_editor_blocked(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u2")
        resp = client.patch("/teams/t1", json={"name": "Renamed"})
        assert resp.status_code == 403


class TestDeleteTeam:
    @pytest.mark.asyncio
    async def test_owner_can_delete_and_cascade_clears_pointers(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(
            fake_gcs,
            team_id="t1",
            owner_uid="u1",
            members=[("u2", "editor"), ("u3", "viewer")],
        )
        # Plant a team motor blob to confirm cascade.
        await fake_gcs.write_json("teams/t1/motors/m1.json", {"x": 1})

        login_as(app, uid="u1")
        resp = client.delete("/teams/t1")
        assert resp.status_code == 204

        # Team prefix wiped + every member's reverse-index pointer gone.
        for blob in list(fake_gcs.blobs):
            assert not blob.startswith("teams/t1/")
        assert "users/u1/teams/t1.json" not in fake_gcs.blobs
        assert "users/u2/teams/t1.json" not in fake_gcs.blobs
        assert "users/u3/teams/t1.json" not in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_editor_blocked(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u2")
        assert client.delete("/teams/t1").status_code == 403


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class TestChangeMemberRole:
    @pytest.mark.asyncio
    async def test_owner_can_promote_member(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "viewer")])
        login_as(app, uid="u1")
        resp = client.patch("/teams/t1/members/u2", json={"role": "editor"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "editor"

    @pytest.mark.asyncio
    async def test_last_owner_demotion_blocked(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u1")
        resp = client.patch("/teams/t1/members/u1", json={"role": "editor"})
        assert resp.status_code == 409
        assert "last owner" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_demotion_succeeds_when_other_owner_exists(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "owner")])
        login_as(app, uid="u1")
        resp = client.patch("/teams/t1/members/u1", json={"role": "editor"})
        assert resp.status_code == 200


class TestRemoveMember:
    @pytest.mark.asyncio
    async def test_owner_can_remove_member(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u1")
        resp = client.delete("/teams/t1/members/u2")
        assert resp.status_code == 204
        assert "teams/t1/members/u2.json" not in fake_gcs.blobs
        assert "users/u2/teams/t1.json" not in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_member_can_self_leave(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u2")
        resp = client.delete("/teams/t1/members/u2")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_member_cannot_remove_other_member(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(
            fake_gcs,
            team_id="t1",
            owner_uid="u1",
            members=[("u2", "editor"), ("u3", "viewer")],
        )
        login_as(app, uid="u2")
        resp = client.delete("/teams/t1/members/u3")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_last_owner_cannot_self_leave(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, uid="u1")
        resp = client.delete("/teams/t1/members/u1")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Account / usage
# ---------------------------------------------------------------------------


class TestTeamUsage:
    @pytest.mark.asyncio
    async def test_usage_reflects_account_caps(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="u1")
        resp = client.get("/teams/t1/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["motor_count"] == 0
        assert body["motor_limit"] == 50

    @pytest.mark.asyncio
    async def test_non_member_gets_404(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, uid="stranger")
        assert client.get("/teams/t1/usage").status_code == 404


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class TestAdminEndpoints:
    @pytest.mark.asyncio
    async def test_admin_can_list_all_teams(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", name="A")
        await make_team(fake_gcs, team_id="t2", owner_uid="u2", name="B")
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.get("/admin/teams")
        assert resp.status_code == 200
        assert {t["team_id"] for t in resp.json()} == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_member_cannot_list_all_teams(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        assert client.get("/admin/teams").status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_update_team_limits(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1")
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.patch(
            "/admin/teams/t1/account/limits",
            json={"monthly_token_limit": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["credits"]["monthly_token_limit"] == 5

    @pytest.mark.asyncio
    async def test_admin_can_force_delete_team(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        await make_team(fake_gcs, team_id="t1", owner_uid="u1", members=[("u2", "editor")])
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.delete("/admin/teams/t1")
        assert resp.status_code == 204
        for blob in list(fake_gcs.blobs):
            assert not blob.startswith("teams/t1/")
        assert "users/u1/teams/t1.json" not in fake_gcs.blobs
        assert "users/u2/teams/t1.json" not in fake_gcs.blobs
