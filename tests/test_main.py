"""Tests for ``app.main`` — application factory wiring."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    def test_health_returns_ok_without_auth(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestRouterMounting:
    @pytest.mark.parametrize(
        "expected_path",
        [
            "/propellants",
            "/motors",
            "/motors/{motor_id}",
            "/simulations",
            "/simulations/{simulation_id}/status",
            "/simulations/{simulation_id}/results",
            "/users/{user_id}/clear",
            "/users/{user_id}",
            "/admin/users",
            "/admin/users/{user_id}/role",
            "/admin/users/{user_id}/disabled",
            "/admin/simulations/rerun-all",
            "/health",
        ],
    )
    def test_route_is_registered(self, app: FastAPI, expected_path: str) -> None:
        """Every advertised route must be mounted by ``create_app``. Renaming
        a prefix or forgetting an ``include_router`` call breaks this test."""
        registered = {route.path for route in app.routes}  # type: ignore[attr-defined]
        assert expected_path in registered, f"No route registered for {expected_path!r}"

    def test_openapi_documents_admin_namespace(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        # Admin tag exists and admin endpoints are documented under it.
        admin_paths = [path for path, item in spec["paths"].items() if path.startswith("/admin/")]
        assert "/admin/users" in admin_paths
        assert "/admin/simulations/rerun-all" in admin_paths


class TestCorsMiddleware:
    def test_cors_headers_set_for_allowed_origin(self, client: TestClient) -> None:
        """The dev default is ``http://localhost:3000`` — that origin must be
        echoed back so the frontend can read responses cross-origin."""
        resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_preflight_request(self, client: TestClient) -> None:
        resp = client.options(
            "/motors",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
        # ``allow_methods=["*"]`` — POST must be permitted.
        assert "POST" in resp.headers.get("access-control-allow-methods", "")
