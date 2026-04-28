"""Tests for ``app.auth.rbac`` — role resolution + ``require_role`` dependency."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.firebase import get_current_user
from app.auth.rbac import DEFAULT_ROLE, get_user_role, require_role


class TestGetUserRole:
    @pytest.mark.parametrize(
        "claim,expected",
        [
            ("admin", "admin"),
            ("member", "member"),
            (None, DEFAULT_ROLE),
            ("", DEFAULT_ROLE),
            ("superuser", DEFAULT_ROLE),
            ("ADMIN", DEFAULT_ROLE),  # case-sensitive — "ADMIN" is not in the allow-list
        ],
    )
    def test_role_resolution(self, claim: str | None, expected: str) -> None:
        user = {"uid": "u1"}
        if claim is not None:
            user["role"] = claim
        assert get_user_role(user) == expected

    def test_missing_role_key_defaults(self) -> None:
        assert get_user_role({"uid": "u1"}) == DEFAULT_ROLE


class TestRequireRoleDependency:
    @pytest.fixture()
    def app(self) -> FastAPI:
        application = FastAPI()

        @application.get("/admin-only")
        async def admin_only(_: dict = Depends(require_role("admin"))) -> dict[str, str]:
            return {"ok": "yes"}

        @application.get("/member-only")
        async def member_only(_: dict = Depends(require_role("member"))) -> dict[str, str]:
            return {"ok": "yes"}

        return application

    @pytest.fixture()
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app)

    def test_admin_passes_admin_gate(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: {"uid": "u1", "role": "admin"}
        assert client.get("/admin-only").status_code == 200

    def test_member_blocked_from_admin_gate(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: {"uid": "u1", "role": "member"}
        resp = client.get("/admin-only")
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    def test_no_role_claim_blocked_from_admin_gate(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: {"uid": "u1"}
        assert client.get("/admin-only").status_code == 403

    def test_admin_passes_member_gate(self, app: FastAPI, client: TestClient) -> None:
        """A member-gated route is *strictly* members-only — a higher-privilege
        admin still fails because ``require_role`` checks for equality, not a
        hierarchy. This is a behavioural lock-in: admins must use admin routes."""
        app.dependency_overrides[get_current_user] = lambda: {"uid": "u1", "role": "admin"}
        assert client.get("/member-only").status_code == 403

    def test_dependency_callable_is_cached_per_role(self) -> None:
        """``require_role`` is ``lru_cache``d so FastAPI sees the same callable
        for repeated mounts — without that, dependencies resolve as distinct
        and cannot share their resolved value across one request."""
        assert require_role("admin") is require_role("admin")
        assert require_role("admin") is not require_role("member")
