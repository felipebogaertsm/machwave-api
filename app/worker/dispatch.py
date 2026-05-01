"""Worker dispatch — publish a Pub/Sub message to trigger a simulation.

Locally, the Google Pub/Sub client picks up ``PUBSUB_EMULATOR_HOST`` and
talks to the emulator running in docker-compose; in deployed environments
it talks to real Pub/Sub.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from app.config import get_settings

logger = logging.getLogger(__name__)

OwnerKind = Literal["user", "team"]


async def trigger_simulation(
    simulation_id: str,
    owner_id: str,
    owner_kind: OwnerKind = "user",
) -> None:
    """Publish a simulation request to the Pub/Sub topic.

    ``owner_kind`` selects the path scheme (user-scoped vs team-scoped) the
    worker reads from. Default ``"user"`` keeps legacy callers unchanged.
    """
    settings = get_settings()

    def _publish() -> None:
        from google.cloud import pubsub_v1

        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(settings.gcp_project_id, settings.pubsub_topic)
        future = publisher.publish(
            topic_path,
            data=b"",
            simulation_id=simulation_id,
            owner_id=owner_id,
            owner_kind=owner_kind,
        )
        future.result(timeout=30)

    await asyncio.to_thread(_publish)
