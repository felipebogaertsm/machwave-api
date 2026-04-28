"""Security tests for admin-only endpoints.

Every endpoint guarded by ``require_role("admin")`` is exercised against three
identities — anonymous, member-role, and admin-role — to confirm that only an
admin Firebase token grants access. Functional behavior, self-action guards,
and not-found / validation paths are also covered.

All Firebase Admin SDK calls are monkeypatched so the suite never reaches a
real Firebase project; GCS deletes are stubbed too.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

# Settings env vars are required before importing the app — pydantic-settings
# reads them at app construction time.
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
os.environ.setdefault("GCP_PROJECT_ID", "test-gcp")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from firebase_admin import auth as firebase_auth  # noqa: E402

import app.routers.users as users_module  # noqa: E402
from app.auth.firebase import get_current_user, get_firebase_app  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN_UID = "admin-uid"
MEMBER_UID = "member-uid"
TARGET_UID = "target-uid"


def _make_record(
    uid: str,
    email: str | None = None,
    display_name: str | None = None,
    disabled: bool = False,
    custom_claims: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid,
        email=email or f"{uid}@example.com",
        display_name=display_name,
        disabled=disabled,
        custom_claims=custom_claims,
    )


class FakeFirebase:
    """In-memory stand-in for ``firebase_admin.auth`` calls used by the router."""

    def __init__(self, records: list[SimpleNamespace] | None = None) -> None:
        self.records: dict[str, SimpleNamespace] = {r.uid: r for r in (records or [])}
        self.deleted: list[str] = []
        self.list_calls: list[dict[str, Any]] = []

    def list_users(
        self,
        page_token: str | None = None,
        max_results: int = 1000,
        app: Any = None,
    ) -> SimpleNamespace:
        self.list_calls.append({"page_token": page_token, "max_results": max_results, "app": app})
        return SimpleNamespace(users=list(self.records.values()), next_page_token=None)

    def get_user(self, uid: str, app: Any = None) -> SimpleNamespace:
        if uid not in self.records:
            raise firebase_auth.UserNotFoundError(f"No user {uid}")
        return self.records[uid]

    def update_user(self, uid: str, *, disabled: bool, app: Any = None) -> SimpleNamespace:
        if uid not in self.records:
            raise firebase_auth.UserNotFoundError(f"No user {uid}")
        record = self.records[uid]
        record.disabled = disabled
        return record

    def set_custom_user_claims(
        self, uid: str, claims: dict[str, Any] | None, app: Any = None
    ) -> None:
        if uid not in self.records:
            raise firebase_auth.UserNotFoundError(f"No user {uid}")
        self.records[uid].custom_claims = claims

    def delete_user(self, uid: str, app: Any = None) -> None:
        if uid not in self.records:
            raise firebase_auth.UserNotFoundError(f"No user {uid}")
        self.deleted.append(uid)
        del self.records[uid]


@pytest.fixture()
def fake_fb() -> FakeFirebase:
    return FakeFirebase(
        records=[
            _make_record(ADMIN_UID, custom_claims={"role": "admin"}),
            _make_record(MEMBER_UID),
            _make_record(TARGET_UID, display_name="Target User"),
        ]
    )


@pytest.fixture()
def gcs_deletes() -> list[str]:
    return []


@pytest.fixture()
def app(
    monkeypatch: pytest.MonkeyPatch,
    fake_fb: FakeFirebase,
    gcs_deletes: list[str],
) -> Iterator[FastAPI]:
    """FastAPI app with Firebase + GCS stubbed and a dummy firebase_admin.App injected."""
    monkeypatch.setattr(users_module.firebase_auth, "list_users", fake_fb.list_users)
    monkeypatch.setattr(users_module.firebase_auth, "get_user", fake_fb.get_user)
    monkeypatch.setattr(users_module.firebase_auth, "update_user", fake_fb.update_user)
    monkeypatch.setattr(
        users_module.firebase_auth, "set_custom_user_claims", fake_fb.set_custom_user_claims
    )
    monkeypatch.setattr(users_module.firebase_auth, "delete_user", fake_fb.delete_user)

    async def fake_delete_prefix(prefix: str) -> None:
        gcs_deletes.append(prefix)

    monkeypatch.setattr(users_module.gcs, "delete_prefix", fake_delete_prefix)

    application = create_app()
    application.dependency_overrides[get_firebase_app] = lambda: object()
    yield application
    application.dependency_overrides.clear()


def _login_as(app: FastAPI, role: str | None, uid: str = ADMIN_UID) -> None:
    """Override ``get_current_user`` so the next request is authenticated as the given role.

    ``role=None`` simulates a signed-in user with no ``role`` claim — i.e. the
    default ``member`` per ``app.auth.rbac.get_user_role``.
    """
    user: dict[str, Any] = {"uid": uid, "email": f"{uid}@example.com"}
    if role is not None:
        user["role"] = role
    app.dependency_overrides[get_current_user] = lambda: user


def _logout(app: FastAPI) -> None:
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Authorization matrix — every admin endpoint must reject anon + member.
# ---------------------------------------------------------------------------


ADMIN_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/admin/users", None),
    ("PUT", f"/admin/users/{TARGET_UID}/role", {"role": "admin"}),
    ("PUT", f"/admin/users/{TARGET_UID}/disabled", {"disabled": True}),
    ("DELETE", f"/admin/users/{TARGET_UID}", None),
    ("POST", "/admin/simulations/rerun-all", None),
]


class TestAdminAuthorizationMatrix:
    """Without an admin Firebase token, every admin endpoint must refuse access."""

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_anonymous_request_is_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        _logout(app)
        resp = client.request(method, path, json=body)
        assert resp.status_code in (401, 403), (
            f"{method} {path} must reject anonymous, got {resp.status_code}"
        )

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_member_role_is_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        _login_as(app, role="member", uid=MEMBER_UID)
        resp = client.request(method, path, json=body)
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_missing_role_claim_is_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        """A signed-in user without a ``role`` claim defaults to ``member``."""
        _login_as(app, role=None, uid=MEMBER_UID)
        resp = client.request(method, path, json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
    def test_unknown_role_value_is_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        """An unrecognised role value (e.g. ``superuser``) must not be treated as admin."""
        _login_as(app, role="superuser", uid=MEMBER_UID)
        resp = client.request(method, path, json=body)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Functional happy paths — admin token grants access and the call works.
# ---------------------------------------------------------------------------


class TestAdminListUsers:
    def test_admin_can_list_users(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        body = resp.json()
        uids = {u["uid"] for u in body["users"]}
        assert uids == {ADMIN_UID, MEMBER_UID, TARGET_UID}
        admin_entry = next(u for u in body["users"] if u["uid"] == ADMIN_UID)
        assert admin_entry["role"] == "admin"
        member_entry = next(u for u in body["users"] if u["uid"] == MEMBER_UID)
        assert member_entry["role"] == "member"

    def test_max_results_validation(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        assert client.get("/admin/users?max_results=0").status_code == 422
        assert client.get("/admin/users?max_results=1001").status_code == 422


class TestAdminSetRole:
    def test_promotes_member_to_admin(
        self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase
    ) -> None:
        _login_as(app, role="admin")
        resp = client.put(f"/admin/users/{TARGET_UID}/role", json={"role": "admin"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        assert fake_fb.records[TARGET_UID].custom_claims == {"role": "admin"}

    def test_demotes_admin_to_member_clears_claim(
        self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase
    ) -> None:
        _login_as(app, role="admin")
        fake_fb.records[TARGET_UID].custom_claims = {"role": "admin", "other": "keep"}
        resp = client.put(f"/admin/users/{TARGET_UID}/role", json={"role": "member"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "member"
        # Other claims preserved; only ``role`` is cleared.
        assert fake_fb.records[TARGET_UID].custom_claims == {"other": "keep"}

    def test_self_demotion_blocked(
        self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase
    ) -> None:
        _login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{ADMIN_UID}/role", json={"role": "member"})
        assert resp.status_code == 409
        # The claim must not have been mutated.
        assert fake_fb.records[ADMIN_UID].custom_claims == {"role": "admin"}

    def test_self_promotion_allowed_noop(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{ADMIN_UID}/role", json={"role": "admin"})
        assert resp.status_code == 200

    def test_unknown_user_returns_404(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        resp = client.put("/admin/users/does-not-exist/role", json={"role": "admin"})
        assert resp.status_code == 404

    def test_invalid_role_value_rejected(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        resp = client.put(f"/admin/users/{TARGET_UID}/role", json={"role": "root"})
        assert resp.status_code == 422

    def test_missing_body_rejected(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        resp = client.put(f"/admin/users/{TARGET_UID}/role")
        assert resp.status_code == 422


class TestAdminSetDisabled:
    def test_disable_user(self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase) -> None:
        _login_as(app, role="admin")
        resp = client.put(f"/admin/users/{TARGET_UID}/disabled", json={"disabled": True})
        assert resp.status_code == 200
        assert resp.json()["disabled"] is True
        assert fake_fb.records[TARGET_UID].disabled is True

    def test_enable_user(self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase) -> None:
        _login_as(app, role="admin")
        fake_fb.records[TARGET_UID].disabled = True
        resp = client.put(f"/admin/users/{TARGET_UID}/disabled", json={"disabled": False})
        assert resp.status_code == 200
        assert resp.json()["disabled"] is False
        assert fake_fb.records[TARGET_UID].disabled is False

    def test_self_disable_blocked(
        self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase
    ) -> None:
        _login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.put(f"/admin/users/{ADMIN_UID}/disabled", json={"disabled": True})
        assert resp.status_code == 409
        assert fake_fb.records[ADMIN_UID].disabled is False

    def test_self_enable_allowed(
        self, app: FastAPI, client: TestClient, fake_fb: FakeFirebase
    ) -> None:
        _login_as(app, role="admin", uid=ADMIN_UID)
        fake_fb.records[ADMIN_UID].disabled = True
        resp = client.put(f"/admin/users/{ADMIN_UID}/disabled", json={"disabled": False})
        assert resp.status_code == 200

    def test_unknown_user_returns_404(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        resp = client.put("/admin/users/does-not-exist/disabled", json={"disabled": True})
        assert resp.status_code == 404

    def test_missing_body_rejected(self, app: FastAPI, client: TestClient) -> None:
        _login_as(app, role="admin")
        resp = client.put(f"/admin/users/{TARGET_UID}/disabled")
        assert resp.status_code == 422


class TestAdminDeleteUser:
    def test_deletes_firebase_account_and_gcs_data(
        self,
        app: FastAPI,
        client: TestClient,
        fake_fb: FakeFirebase,
        gcs_deletes: list[str],
    ) -> None:
        _login_as(app, role="admin")
        resp = client.delete(f"/admin/users/{TARGET_UID}")
        assert resp.status_code == 204
        assert TARGET_UID in fake_fb.deleted
        assert TARGET_UID not in fake_fb.records
        assert gcs_deletes == [f"users/{TARGET_UID}/"]

    def test_self_delete_blocked(
        self,
        app: FastAPI,
        client: TestClient,
        fake_fb: FakeFirebase,
        gcs_deletes: list[str],
    ) -> None:
        _login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.delete(f"/admin/users/{ADMIN_UID}")
        assert resp.status_code == 409
        # Neither Firebase nor GCS state must change.
        assert ADMIN_UID in fake_fb.records
        assert fake_fb.deleted == []
        assert gcs_deletes == []

    def test_unknown_user_returns_404_without_touching_gcs(
        self,
        app: FastAPI,
        client: TestClient,
        fake_fb: FakeFirebase,
        gcs_deletes: list[str],
    ) -> None:
        _login_as(app, role="admin")
        resp = client.delete("/admin/users/does-not-exist")
        assert resp.status_code == 404
        assert fake_fb.deleted == []
        assert gcs_deletes == [], "must not delete GCS data when the user doesn't exist"


# ---------------------------------------------------------------------------
# Self-service endpoints stay reachable and stay scoped to the caller — admin
# powers must not leak through them.
# ---------------------------------------------------------------------------


class TestSelfServiceIsolation:
    def test_self_service_clear_works_for_member(
        self, app: FastAPI, client: TestClient, gcs_deletes: list[str]
    ) -> None:
        _login_as(app, role="member", uid=MEMBER_UID)
        resp = client.delete(f"/users/{MEMBER_UID}/clear")
        assert resp.status_code == 204
        assert gcs_deletes == [f"users/{MEMBER_UID}/"]

    def test_self_service_clear_rejects_other_users(
        self, app: FastAPI, client: TestClient, gcs_deletes: list[str]
    ) -> None:
        """Even an admin must use ``/admin/users/{uid}``, not impersonate via self-service."""
        _login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.delete(f"/users/{TARGET_UID}/clear")
        assert resp.status_code == 403
        assert gcs_deletes == []
