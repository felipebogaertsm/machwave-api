"""Tests for ``app.routers.users`` — admin user management + self-service.

Every admin endpoint is exercised against three identities (anonymous,
member, admin). Functional happy paths, self-action guards (cannot demote /
disable / delete yourself), and not-found paths are covered explicitly.
Self-service endpoints stay scoped to the caller — admins must use the
``/admin/users/{uid}`` routes, not impersonate via self-service.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_UID, MEMBER_UID, TARGET_UID, FakeGCS, login_as, logout
from tests.routers.conftest import FakeFirebase

ADMIN_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/admin/users", None),
    ("PUT", f"/admin/users/{TARGET_UID}/role", {"role": "admin"}),
    ("PUT", f"/admin/users/{TARGET_UID}/disabled", {"disabled": True}),
    ("DELETE", f"/admin/users/{TARGET_UID}", None),
]


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------


class TestAdminAuthorizationMatrix:
    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_anonymous_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        logout(app)
        resp = client.request(method, path, json=body)
        assert resp.status_code in (401, 403)

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_member_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        resp = client.request(method, path, json=body)
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_missing_role_claim_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        login_as(app, role=None, uid=MEMBER_UID)
        assert client.request(method, path, json=body).status_code == 403

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_unknown_role_value_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        """Tokens with a role like ``superuser`` must not back into admin."""
        login_as(app, role="superuser", uid=MEMBER_UID)
        assert client.request(method, path, json=body).status_code == 403


# ---------------------------------------------------------------------------
# Admin: list users
# ---------------------------------------------------------------------------


class TestAdminListUsers:
    def test_admin_can_list(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        body = client.get("/admin/users").json()
        uids = {u["uid"] for u in body["users"]}
        assert uids == {ADMIN_UID, MEMBER_UID, TARGET_UID}

        admin_entry = next(u for u in body["users"] if u["uid"] == ADMIN_UID)
        member_entry = next(u for u in body["users"] if u["uid"] == MEMBER_UID)
        assert admin_entry["role"] == "admin"
        assert member_entry["role"] == "member"

    @pytest.mark.parametrize("max_results", [0, 1001])
    def test_max_results_bounds(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
        max_results: int,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        assert client.get(f"/admin/users?max_results={max_results}").status_code == 422


# ---------------------------------------------------------------------------
# Admin: set role
# ---------------------------------------------------------------------------


class TestAdminSetRole:
    def test_promote_member_to_admin(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{TARGET_UID}/role", json={"role": "admin"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        assert fake_firebase.records[TARGET_UID].custom_claims == {"role": "admin"}

    def test_demote_admin_to_member_clears_role_claim(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        """``member`` is the absence of the ``role`` claim — only that key is
        cleared, sibling claims must survive."""
        login_as(app, role="admin", uid=ADMIN_UID)
        fake_firebase.records[TARGET_UID].custom_claims = {
            "role": "admin",
            "other": "keep",
        }
        resp = client.put(f"/admin/users/{TARGET_UID}/role", json={"role": "member"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "member"
        assert fake_firebase.records[TARGET_UID].custom_claims == {"other": "keep"}

    def test_self_demotion_blocked(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        """Lockout safety: an admin must not be able to remove their own
        privileges, otherwise a typo can leave the project with zero admins."""
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{ADMIN_UID}/role", json={"role": "member"})
        assert resp.status_code == 409
        assert fake_firebase.records[ADMIN_UID].custom_claims == {"role": "admin"}

    def test_self_promotion_noop_allowed(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{ADMIN_UID}/role", json={"role": "admin"})
        assert resp.status_code == 200

    def test_unknown_user_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put("/admin/users/does-not-exist/role", json={"role": "admin"})
        assert resp.status_code == 404

    def test_invalid_role_value_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{TARGET_UID}/role", json={"role": "root"})
        assert resp.status_code == 422

    def test_missing_body_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        assert client.put(f"/admin/users/{TARGET_UID}/role").status_code == 422


# ---------------------------------------------------------------------------
# Admin: set disabled
# ---------------------------------------------------------------------------


class TestAdminSetDisabled:
    def test_disable_user(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{TARGET_UID}/disabled", json={"disabled": True})
        assert resp.status_code == 200
        assert resp.json()["disabled"] is True
        assert fake_firebase.records[TARGET_UID].disabled is True

    def test_enable_user(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        fake_firebase.records[TARGET_UID].disabled = True
        resp = client.put(f"/admin/users/{TARGET_UID}/disabled", json={"disabled": False})
        assert resp.status_code == 200
        assert fake_firebase.records[TARGET_UID].disabled is False

    def test_self_disable_blocked(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{ADMIN_UID}/disabled", json={"disabled": True})
        assert resp.status_code == 409
        assert fake_firebase.records[ADMIN_UID].disabled is False

    def test_self_enable_allowed(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        fake_firebase.records[ADMIN_UID].disabled = True
        resp = client.put(f"/admin/users/{ADMIN_UID}/disabled", json={"disabled": False})
        assert resp.status_code == 200

    def test_unknown_user_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put("/admin/users/does-not-exist/disabled", json={"disabled": True})
        assert resp.status_code == 404

    def test_missing_body_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        assert client.put(f"/admin/users/{TARGET_UID}/disabled").status_code == 422


# ---------------------------------------------------------------------------
# Admin: delete user
# ---------------------------------------------------------------------------


class TestAdminDeleteUser:
    def test_deletes_firebase_account_and_gcs_data(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        # A motor + simulation owned by the target — both must be wiped.
        fake_gcs.blobs[f"users/{TARGET_UID}/motors/m1.json"] = {"v": 1}
        fake_gcs.blobs[f"users/{TARGET_UID}/simulations/sim-1/status.json"] = {"v": 1}

        resp = client.delete(f"/admin/users/{TARGET_UID}")
        assert resp.status_code == 204
        assert TARGET_UID in fake_firebase.deleted
        assert TARGET_UID not in fake_firebase.records
        # All GCS data under the user prefix gone.
        assert not [k for k in fake_gcs.blobs if k.startswith(f"users/{TARGET_UID}/")]

    def test_self_delete_blocked(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        """Same lockout reasoning as ``test_self_demotion_blocked``: deleting
        yourself through the admin endpoint is not allowed. Use the
        self-service flow (``DELETE /users/{uid}``) instead, which requires
        an email confirmation."""
        login_as(app, role="admin", uid=ADMIN_UID)
        fake_gcs.blobs[f"users/{ADMIN_UID}/motors/m.json"] = {"v": 1}

        resp = client.delete(f"/admin/users/{ADMIN_UID}")
        assert resp.status_code == 409
        assert ADMIN_UID in fake_firebase.records
        assert fake_firebase.deleted == []
        assert f"users/{ADMIN_UID}/motors/m.json" in fake_gcs.blobs

    def test_unknown_user_404_without_touching_gcs(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.delete("/admin/users/does-not-exist")
        assert resp.status_code == 404
        assert fake_gcs.blobs == {}


# ---------------------------------------------------------------------------
# Self-service: clear (data only)
# ---------------------------------------------------------------------------


class TestSelfServiceClear:
    def test_clears_callers_data(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        fake_gcs.blobs[f"users/{MEMBER_UID}/motors/m.json"] = {"v": 1}

        assert client.delete(f"/users/{MEMBER_UID}/clear").status_code == 204
        assert not [k for k in fake_gcs.blobs if k.startswith(f"users/{MEMBER_UID}/")]
        # Firebase account is *not* deleted by /clear — only GCS data.
        assert MEMBER_UID not in fake_firebase.deleted

    def test_other_user_path_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        """Even an admin must not impersonate another user via this endpoint."""
        login_as(app, role="admin", uid=ADMIN_UID)
        fake_gcs.blobs[f"users/{TARGET_UID}/motors/m.json"] = {"v": 1}

        resp = client.delete(f"/users/{TARGET_UID}/clear")
        assert resp.status_code == 403
        assert f"users/{TARGET_UID}/motors/m.json" in fake_gcs.blobs


# ---------------------------------------------------------------------------
# Self-service: delete account (with email confirmation)
# ---------------------------------------------------------------------------


class TestSelfServiceDeleteAccount:
    def test_email_confirmation_required(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID, email=f"{MEMBER_UID}@example.com")
        resp = client.request("DELETE", f"/users/{MEMBER_UID}", json={"email": "wrong@example.com"})
        assert resp.status_code == 422
        assert MEMBER_UID not in fake_firebase.deleted

    def test_email_match_is_case_insensitive(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        """The router lowercases both sides — clients shouldn't have to worry
        about case when echoing the user's own email."""
        login_as(app, role="member", uid=MEMBER_UID, email=f"{MEMBER_UID}@example.com")
        # Need a real Firebase record so delete_user doesn't 404 the fake.
        fake_firebase.records[MEMBER_UID] = (
            fake_firebase.records.get(MEMBER_UID) or fake_firebase.records[TARGET_UID]
        )
        from types import SimpleNamespace

        fake_firebase.records[MEMBER_UID] = SimpleNamespace(
            uid=MEMBER_UID,
            email=f"{MEMBER_UID}@example.com",
            display_name=None,
            disabled=False,
            custom_claims=None,
        )

        resp = client.request(
            "DELETE",
            f"/users/{MEMBER_UID}",
            json={"email": f"{MEMBER_UID.upper()}@EXAMPLE.COM"},
        )
        assert resp.status_code == 204
        assert MEMBER_UID in fake_firebase.deleted

    def test_other_user_path_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID, email=f"{ADMIN_UID}@example.com")
        resp = client.request(
            "DELETE",
            f"/users/{TARGET_UID}",
            json={"email": f"{TARGET_UID}@example.com"},
        )
        assert resp.status_code == 403
        assert TARGET_UID not in fake_firebase.deleted

    def test_token_without_email_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        """Some Firebase identity providers issue tokens without an email
        claim — the endpoint must refuse those rather than silently delete."""
        from app.auth.firebase import get_current_user

        app.dependency_overrides[get_current_user] = lambda: {"uid": MEMBER_UID}
        resp = client.request(
            "DELETE", f"/users/{MEMBER_UID}", json={"email": "anything@example.com"}
        )
        assert resp.status_code == 422
        assert MEMBER_UID not in fake_firebase.deleted

    def test_full_delete_clears_gcs_and_firebase(
        self,
        app: FastAPI,
        client: TestClient,
        fake_firebase: FakeFirebase,
        fake_gcs: FakeGCS,
    ) -> None:
        from types import SimpleNamespace

        login_as(app, role="member", uid=MEMBER_UID, email=f"{MEMBER_UID}@example.com")
        fake_firebase.records[MEMBER_UID] = SimpleNamespace(
            uid=MEMBER_UID,
            email=f"{MEMBER_UID}@example.com",
            display_name=None,
            disabled=False,
            custom_claims=None,
        )
        fake_gcs.blobs[f"users/{MEMBER_UID}/motors/m1.json"] = {"v": 1}

        resp = client.request(
            "DELETE",
            f"/users/{MEMBER_UID}",
            json={"email": f"{MEMBER_UID}@example.com"},
        )
        assert resp.status_code == 204
        assert MEMBER_UID in fake_firebase.deleted
        assert not [k for k in fake_gcs.blobs if k.startswith(f"users/{MEMBER_UID}/")]
