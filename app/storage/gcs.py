from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any

from google.cloud import storage

from app.config import get_settings


@lru_cache(maxsize=1)
def _get_client() -> storage.Client:
    return storage.Client()


@lru_cache(maxsize=1)
def _get_bucket() -> storage.Bucket:
    return _get_client().bucket(get_settings().gcs_bucket_name)


def _sync_read_json(blob_name: str) -> dict[str, Any] | None:
    blob = _get_bucket().blob(blob_name)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def _sync_write_json(blob_name: str, data: dict[str, Any]) -> None:
    blob = _get_bucket().blob(blob_name)
    blob.upload_from_string(
        json.dumps(data, default=str),
        content_type="application/json",
    )


def _sync_delete_prefix(prefix: str) -> None:
    blobs = list(_get_client().list_blobs(_get_bucket(), prefix=prefix))
    if blobs:
        _get_client().bucket(get_settings().gcs_bucket_name).delete_blobs(blobs)


def _sync_list_blobs(prefix: str) -> list[str]:
    """Return a list of blob names under *prefix*."""
    return [blob.name for blob in _get_client().list_blobs(_get_bucket(), prefix=prefix)]


async def read_json(blob_name: str) -> dict[str, Any] | None:
    """Read and parse a JSON blob.  Returns ``None`` if the blob doesn't exist."""
    return await asyncio.to_thread(_sync_read_json, blob_name)


async def write_json(blob_name: str, data: dict[str, Any]) -> None:
    """Serialise *data* as JSON and write it to *blob_name*."""
    await asyncio.to_thread(_sync_write_json, blob_name, data)


async def delete_prefix(prefix: str) -> None:
    """Delete all blobs whose name starts with *prefix*."""
    await asyncio.to_thread(_sync_delete_prefix, prefix)


async def list_blobs(prefix: str) -> list[str]:
    """Return blob names under *prefix* (non-recursive)."""
    return await asyncio.to_thread(_sync_list_blobs, prefix)
