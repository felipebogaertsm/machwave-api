"""Tests for ``app.worker.dispatch.trigger_simulation``.

Two paths to cover: ``ENV=local`` spawns a local subprocess; anything else
submits a Cloud Run Job execution. Both paths are stubbed — no subprocesses,
no GCP calls.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.worker import dispatch as dispatch_module


@pytest.fixture(autouse=True)
def _settings_cache_per_test() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Local subprocess path
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, returncode: int = 0) -> None:
        self._rc = returncode

    async def wait(self) -> int:
        return self._rc


class TestTriggerSimulationLocal:
    @pytest.mark.asyncio
    async def test_spawns_worker_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "local")

        captured: dict[str, Any] = {}

        async def fake_create_subprocess_exec(
            *args: str, env: dict[str, str], **_: Any
        ) -> _FakeProcess:
            captured["args"] = args
            captured["env"] = env
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        await dispatch_module.trigger_simulation("sim-1", "user-1")

        # Wait for the background task — ``_spawn_local_worker`` schedules it
        # but ``trigger_simulation`` returns immediately.
        for task in list(dispatch_module._worker_tasks):
            await task

        assert captured["args"][:3] == (sys.executable, "-m", "app.worker.run")
        assert captured["env"]["SIM_ID"] == "sim-1"
        assert captured["env"]["USER_ID"] == "user-1"

    @pytest.mark.asyncio
    async def test_failure_logged_but_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-zero exit from the worker must not propagate — the API
        already returned 202 and the caller is polling /status."""
        import logging

        monkeypatch.setenv("ENV", "local")

        async def fake_create_subprocess_exec(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(returncode=1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        with caplog.at_level(logging.ERROR, logger="app.worker.dispatch"):
            await dispatch_module.trigger_simulation("sim-1", "user-1")
            for task in list(dispatch_module._worker_tasks):
                await task

        assert any(
            "exited 1" in r.getMessage() for r in caplog.records if r.name == "app.worker.dispatch"
        )


# ---------------------------------------------------------------------------
# Cloud Run Jobs path
# ---------------------------------------------------------------------------


class TestTriggerSimulationProd:
    @pytest.mark.asyncio
    async def test_submits_cloud_run_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "prod")
        monkeypatch.setenv("GCP_PROJECT_ID", "machwave-prod")
        monkeypatch.setenv("CLOUD_RUN_JOB_NAME", "machwave-worker")
        monkeypatch.setenv("CLOUD_RUN_JOB_REGION", "us-central1")

        # Build a fake ``run_v2`` module surface big enough for ``_submit``.
        fake_run_v2 = MagicMock()
        fake_jobs_client = MagicMock()
        fake_run_v2.JobsClient.return_value = fake_jobs_client
        fake_run_v2.RunJobRequest = MagicMock()
        fake_run_v2.RunJobRequest.Overrides = MagicMock()
        fake_run_v2.RunJobRequest.Overrides.ContainerOverride = MagicMock()
        fake_run_v2.EnvVar = MagicMock(
            side_effect=lambda name, value: {"name": name, "value": value}
        )

        # Inject as ``google.cloud.run_v2`` so the ``from google.cloud import
        # run_v2`` inside ``_submit`` resolves to our fake.
        monkeypatch.setitem(sys.modules, "google.cloud.run_v2", fake_run_v2)

        await dispatch_module.trigger_simulation("sim-1", "user-1")

        # JobsClient instantiated and run_job called once.
        fake_run_v2.JobsClient.assert_called_once()
        fake_jobs_client.run_job.assert_called_once()

        # The job name interpolates settings correctly.
        request = fake_run_v2.RunJobRequest.call_args.kwargs
        assert request["name"] == (
            "projects/machwave-prod/locations/us-central1/jobs/machwave-worker"
        )

        # SIM_ID / USER_ID are passed as env vars on the override.
        env_pairs = [
            (
                call.kwargs.get("name") or call.args[0],
                call.kwargs.get("value") or call.args[1],
            )
            for call in fake_run_v2.EnvVar.call_args_list
        ]
        assert ("SIM_ID", "sim-1") in env_pairs
        assert ("USER_ID", "user-1") in env_pairs
