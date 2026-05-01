"""Tests for ``app.worker.server`` — Pub/Sub push handler."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.worker import server as server_module


@pytest.fixture
def client() -> TestClient:
    return TestClient(server_module.app)


def _envelope(**attrs: str) -> dict[str, Any]:
    return {
        "message": {
            "messageId": "msg-1",
            "attributes": attrs,
            "publishTime": "2026-04-30T00:00:00Z",
        },
        "subscription": "projects/p/subscriptions/s",
    }


class TestHealth:
    def test_returns_ok(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestPubsubPush:
    def test_dispatches_to_run(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_run = AsyncMock(return_value=None)
        monkeypatch.setattr(server_module, "run", fake_run)

        r = client.post(
            "/pubsub/push",
            json=_envelope(simulation_id="sim-1", owner_id="user-1", owner_kind="user"),
        )

        assert r.status_code == 200
        fake_run.assert_awaited_once_with("sim-1", "user-1", "user")

    def test_defaults_owner_kind_to_user(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_run = AsyncMock(return_value=None)
        monkeypatch.setattr(server_module, "run", fake_run)

        r = client.post(
            "/pubsub/push",
            json=_envelope(simulation_id="sim-2", owner_id="user-2"),
        )

        assert r.status_code == 200
        fake_run.assert_awaited_once_with("sim-2", "user-2", "user")

    def test_drops_message_with_missing_attributes(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing required attrs ack the message (200) so Pub/Sub stops
        retrying a poison payload — ``run`` is never invoked."""
        fake_run = AsyncMock()
        monkeypatch.setattr(server_module, "run", fake_run)

        r = client.post("/pubsub/push", json=_envelope(simulation_id="sim-3"))

        assert r.status_code == 200
        assert r.json() == {"status": "dropped"}
        fake_run.assert_not_awaited()

    def test_drops_invalid_owner_kind(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_run = AsyncMock()
        monkeypatch.setattr(server_module, "run", fake_run)

        r = client.post(
            "/pubsub/push",
            json=_envelope(simulation_id="sim-4", owner_id="user-4", owner_kind="bogus"),
        )

        assert r.status_code == 200
        assert r.json() == {"status": "dropped"}
        fake_run.assert_not_awaited()

    def test_returns_500_on_run_failure_so_pubsub_retries(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(server_module, "run", boom)

        r = client.post(
            "/pubsub/push",
            json=_envelope(simulation_id="sim-5", owner_id="user-5"),
        )

        assert r.status_code == 500

    def test_drops_envelope_without_message_field(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_run = AsyncMock()
        monkeypatch.setattr(server_module, "run", fake_run)

        r = client.post("/pubsub/push", json={"subscription": "x"})

        assert r.status_code == 200
        fake_run.assert_not_awaited()
