"""HTTP server for Pub/Sub push delivery.

A Cloud Run service running this app sits warm (min-instances=1) and accepts
push deliveries from a Pub/Sub subscription on ``POST /pubsub/push``. The
endpoint runs the simulation synchronously and returns 200 to ack the message.

Push auth (OIDC token) is validated by Cloud Run via IAM — only the configured
push service account is granted ``run.invoker`` on this service, so the handler
itself does not need to verify tokens.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from app.worker.run import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("machwave-worker")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    yield


app = FastAPI(title="Machwave Worker", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pubsub/push")
async def pubsub_push(request: Request) -> dict[str, str]:
    """Handle a Pub/Sub push delivery.

    Returning 2xx acks the message; any 4xx/5xx triggers redelivery. We
    intentionally return 204-equivalent (200 OK) on validation errors that
    are *not* the worker's fault (malformed envelope), so Pub/Sub stops
    retrying poison messages — they go to the dead-letter topic if configured.
    """
    envelope: dict[str, Any] = await request.json()
    message = envelope.get("message")
    if not isinstance(message, dict):
        logger.error("Push envelope missing 'message': %r", envelope)
        # Ack — not retryable.
        return {"status": "dropped"}

    attrs = message.get("attributes") or {}
    simulation_id = attrs.get("simulation_id")
    owner_id = attrs.get("owner_id")
    owner_kind = attrs.get("owner_kind", "user")

    if not simulation_id or not owner_id:
        logger.error("Push message missing required attributes: %r", attrs)
        return {"status": "dropped"}

    if owner_kind not in ("user", "team"):
        logger.error("Invalid owner_kind=%r", owner_kind)
        return {"status": "dropped"}

    logger.info(
        "Received push  message_id=%s  simulation_id=%s  owner_kind=%s",
        message.get("messageId"),
        simulation_id,
        owner_kind,
    )

    try:
        await run(simulation_id, owner_id, owner_kind)
    except Exception:
        logger.exception("Worker run failed; nacking for redelivery")
        # 5xx => Pub/Sub redelivers.
        raise HTTPException(status_code=500, detail="worker failed") from None

    return {"status": "ok"}
