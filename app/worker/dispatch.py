"""Worker dispatch helpers — local subprocess vs Cloud Run Job submission."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

_worker_tasks: set[asyncio.Task[None]] = set()

OwnerKind = Literal["user", "team"]


async def trigger_simulation(
    simulation_id: str,
    owner_id: str,
    owner_kind: OwnerKind = "user",
) -> None:
    """Dispatch a simulation. Local env uses a subprocess; prod uses Cloud Run Jobs.

    ``owner_kind`` selects the path scheme (user-scoped vs team-scoped) the
    worker reads from. Default ``"user"`` keeps legacy callers unchanged.
    """
    if get_settings().env == "local":
        _spawn_local_worker(simulation_id, owner_id, owner_kind)
    else:
        await _trigger_cloud_run_job(simulation_id, owner_id, owner_kind)


def _spawn_local_worker(simulation_id: str, owner_id: str, owner_kind: OwnerKind) -> None:
    async def _run() -> None:
        env = {
            **os.environ,
            "SIM_ID": simulation_id,
            "OWNER_ID": owner_id,
            "OWNER_KIND": owner_kind,
            # Back-compat: keep USER_ID populated for ``owner_kind == "user"``
            # so the worker entrypoint can read either env var.
            "USER_ID": owner_id if owner_kind == "user" else "",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.worker.run", env=env
        )
        rc = await process.wait()
        if rc != 0:
            logger.error("Local worker exited %d for simulation_id=%s", rc, simulation_id)

    task = asyncio.create_task(_run())
    _worker_tasks.add(task)
    task.add_done_callback(_worker_tasks.discard)


async def _trigger_cloud_run_job(simulation_id: str, owner_id: str, owner_kind: OwnerKind) -> None:
    settings = get_settings()

    def _submit() -> None:
        from google.cloud import run_v2

        client = run_v2.JobsClient()
        job_name = (
            f"projects/{settings.gcp_project_id}"
            f"/locations/{settings.cloud_run_job_region}"
            f"/jobs/{settings.cloud_run_job_name}"
        )
        env_vars = [
            run_v2.EnvVar(name="SIM_ID", value=simulation_id),
            run_v2.EnvVar(name="OWNER_ID", value=owner_id),
            run_v2.EnvVar(name="OWNER_KIND", value=owner_kind),
        ]
        if owner_kind == "user":
            env_vars.append(run_v2.EnvVar(name="USER_ID", value=owner_id))
        request = run_v2.RunJobRequest(
            name=job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(env=env_vars)]
            ),
        )
        client.run_job(request=request)

    await asyncio.to_thread(_submit)
