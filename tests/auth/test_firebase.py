"""Tests for ``app.auth.firebase`` — token verification and error mapping.

Each Firebase ``*IdTokenError`` must surface as a 401 with the right detail.
A generic exception must also be reduced to 401 — we never want to leak
internal SDK errors to the client. The happy path returns the decoded token's
claims dict.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth

from app.auth import firebase as firebase_module
from app.auth.firebase import get_current_user, get_firebase_app


def _make_app(auth_dep) -> FastAPI:
    """Tiny app that just echoes the token claims back through ``get_current_user``."""
    application = FastAPI()
    application.dependency_overrides[get_firebase_app] = lambda: object()

    @application.get("/me")
    async def me(user: dict[str, Any] = auth_dep) -> dict[str, Any]:
        return user

    return application


@pytest.fixture()
def app() -> FastAPI:
    from fastapi import Depends

    return _make_app(Depends(get_current_user))


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestGetCurrentUser:
    def test_valid_token_returns_decoded_claims(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        decoded = {"uid": "u1", "email": "u1@example.com", "role": "admin"}
        monkeypatch.setattr(
            firebase_auth, "verify_id_token", lambda token, app, check_revoked: decoded
        )
        resp = client.get("/me", headers={"Authorization": "Bearer good-token"})
        assert resp.status_code == 200
        assert resp.json() == decoded

    def test_missing_authorization_header_rejected(self, client: TestClient) -> None:
        resp = client.get("/me")
        # HTTPBearer with auto_error=True returns 403 for a missing header.
        assert resp.status_code in (401, 403)

    @pytest.mark.parametrize(
        "exception,expected_substring",
        [
            (firebase_auth.RevokedIdTokenError("revoked"), "revoked"),
            (firebase_auth.ExpiredIdTokenError("expired", cause=None), "expired"),
            (firebase_auth.InvalidIdTokenError("invalid"), "invalid"),
        ],
    )
    def test_known_token_errors_map_to_401(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: TestClient,
        exception: Exception,
        expected_substring: str,
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise exception

        monkeypatch.setattr(firebase_auth, "verify_id_token", _raise)
        resp = client.get("/me", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 401
        assert expected_substring in resp.json()["detail"].lower()

    def test_generic_exception_is_swallowed_into_401(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        """Internal SDK errors must not leak — they reduce to a generic 401."""

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("internal SDK explosion with stack trace")

        monkeypatch.setattr(firebase_auth, "verify_id_token", _raise)
        resp = client.get("/me", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 401
        assert "internal SDK explosion" not in resp.json()["detail"]


class TestGetFirebaseApp:
    def test_caches_init_per_project_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``_init_firebase_app`` is ``lru_cache``d so we don't re-initialise the
        SDK on every request. Verify the underlying ``initialize_app`` is only
        called once per project id."""
        firebase_module._init_firebase_app.cache_clear()

        calls: list[dict[str, Any]] = []

        def fake_initialize(options: dict[str, Any]) -> object:
            calls.append(options)
            return object()

        monkeypatch.setattr(firebase_module.firebase_admin, "initialize_app", fake_initialize)

        a = firebase_module._init_firebase_app("proj-1")
        b = firebase_module._init_firebase_app("proj-1")
        c = firebase_module._init_firebase_app("proj-2")

        assert a is b
        assert a is not c
        assert [c["projectId"] for c in calls] == ["proj-1", "proj-2"]

        firebase_module._init_firebase_app.cache_clear()
