"""Tests for ``app.worker.dispatch.trigger_simulation`` — publishes to Pub/Sub.

Locally (under docker-compose) the publisher hits the Pub/Sub emulator;
deployed it hits real Pub/Sub. The branch is inside the Google client
library (``PUBSUB_EMULATOR_HOST`` env var), so our code is identical for
both — and so is this test, which stubs the publisher entirely.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.worker import dispatch as dispatch_module


@pytest.fixture(autouse=True)
def _settings_cache_per_test() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestTriggerSimulation:
    @pytest.mark.asyncio
    async def test_publishes_to_pubsub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCP_PROJECT_ID", "machwave-dev")
        monkeypatch.setenv("PUBSUB_TOPIC", "machwave-simulations")

        fake_pubsub_v1 = MagicMock()
        fake_publisher = MagicMock()
        fake_pubsub_v1.PublisherClient.return_value = fake_publisher
        fake_publisher.topic_path.side_effect = lambda project, topic: (
            f"projects/{project}/topics/{topic}"
        )
        fake_future = MagicMock()
        fake_future.result.return_value = "msg-1"
        fake_publisher.publish.return_value = fake_future

        # Inject as ``google.cloud.pubsub_v1`` so the ``from google.cloud
        # import pubsub_v1`` inside ``_publish`` resolves to our fake.
        monkeypatch.setitem(sys.modules, "google.cloud.pubsub_v1", fake_pubsub_v1)

        await dispatch_module.trigger_simulation("sim-1", "user-1")

        fake_pubsub_v1.PublisherClient.assert_called_once()
        fake_publisher.publish.assert_called_once()

        call = fake_publisher.publish.call_args
        assert call.args[0] == "projects/machwave-dev/topics/machwave-simulations"
        assert call.kwargs["simulation_id"] == "sim-1"
        assert call.kwargs["owner_id"] == "user-1"
        assert call.kwargs["owner_kind"] == "user"
        # Ensure publish was awaited by checking ``future.result`` was called.
        fake_future.result.assert_called_once()

    @pytest.mark.asyncio
    async def test_publishes_team_owner_kind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCP_PROJECT_ID", "machwave-dev")
        monkeypatch.setenv("PUBSUB_TOPIC", "machwave-simulations")

        fake_pubsub_v1 = MagicMock()
        fake_publisher = MagicMock()
        fake_pubsub_v1.PublisherClient.return_value = fake_publisher
        fake_publisher.topic_path.side_effect = lambda project, topic: (
            f"projects/{project}/topics/{topic}"
        )
        fake_future = MagicMock()
        fake_publisher.publish.return_value = fake_future

        monkeypatch.setitem(sys.modules, "google.cloud.pubsub_v1", fake_pubsub_v1)

        await dispatch_module.trigger_simulation("sim-1", "team-1", owner_kind="team")

        call = fake_publisher.publish.call_args
        assert call.kwargs["owner_kind"] == "team"
        assert call.kwargs["owner_id"] == "team-1"
