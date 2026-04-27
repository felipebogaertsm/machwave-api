"""Worker dispatch helpers — local subprocess vs Cloud Run Job submission."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from app.config import get_settings

logger = logging.getLogger(__name__)

_worker_tasks: set[asyncio.Task[None]] = set()


async def trigger_simulation(simulation_id: str, user_id: str) -> None:
    """Dispatch a simulation. Local env uses a subprocess; prod uses Cloud Run Jobs."""
    if get_settings().env == "local":
        _spawn_local_worker(simulation_id, user_id)
    else:
        await _trigger_cloud_run_job(simulation_id, user_id)


def _spawn_local_worker(simulation_id: str, user_id: str) -> None:
    async def _run() -> None:
        env = {**os.environ, "SIM_ID": simulation_id, "USER_ID": user_id}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.worker.run", env=env
        )
        rc = await process.wait()
        if rc != 0:
            logger.error("Local worker exited %d for simulation_id=%s", rc, simulation_id)

    task = asyncio.create_task(_run())
    _worker_tasks.add(task)
    task.add_done_callback(_worker_tasks.discard)


async def _trigger_cloud_run_job(simulation_id: str, user_id: str) -> None:
    settings = get_settings()

    def _submit() -> None:
        from google.cloud import run_v2

        client = run_v2.JobsClient()
        job_name = (
            f"projects/{settings.gcp_project_id}"
            f"/locations/{settings.cloud_run_job_region}"
            f"/jobs/{settings.cloud_run_job_name}"
        )
        request = run_v2.RunJobRequest(
            name=job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="SIM_ID", value=simulation_id),
                            run_v2.EnvVar(name="USER_ID", value=user_id),
                        ]
                    )
                ]
            ),
        )
        client.run_job(request=request)

    await asyncio.to_thread(_submit)
