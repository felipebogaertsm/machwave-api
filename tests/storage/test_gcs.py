"""Tests for ``app.storage.gcs`` — the thin async wrappers around
``google.cloud.storage``.

These verify the *behaviour* of the helpers (correct sync helpers called,
correct ``None`` semantics for missing blobs, correct serialisation choices)
while stubbing out the real ``storage.Client`` so nothing reaches GCP.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.storage import gcs as gcs_module


@pytest.fixture()
def fake_bucket(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``_get_client`` and ``_get_bucket`` with a single bucket mock.

    We bypass the ``lru_cache``d helpers by clearing them, then patch the
    underlying functions to return our mocks.
    """
    bucket = MagicMock(name="bucket")
    client = MagicMock(name="client")
    client.bucket.return_value = bucket

    gcs_module._get_client.cache_clear()
    gcs_module._get_bucket.cache_clear()
    monkeypatch.setattr(gcs_module, "_get_client", lambda: client)
    monkeypatch.setattr(gcs_module, "_get_bucket", lambda: bucket)
    return bucket


class TestReadJson:
    @pytest.mark.asyncio
    async def test_returns_none_when_blob_missing(self, fake_bucket: MagicMock) -> None:
        blob = MagicMock()
        blob.exists.return_value = False
        fake_bucket.blob.return_value = blob

        assert await gcs_module.read_json("missing.json") is None
        # No download attempted when the blob doesn't exist.
        blob.download_as_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_parses_existing_json(self, fake_bucket: MagicMock) -> None:
        blob = MagicMock()
        blob.exists.return_value = True
        blob.download_as_text.return_value = '{"a": 1, "b": [2, 3]}'
        fake_bucket.blob.return_value = blob

        assert await gcs_module.read_json("present.json") == {"a": 1, "b": [2, 3]}


class TestWriteJson:
    @pytest.mark.asyncio
    async def test_uploads_serialised_payload_with_json_content_type(
        self, fake_bucket: MagicMock
    ) -> None:
        blob = MagicMock()
        fake_bucket.blob.return_value = blob

        await gcs_module.write_json("path.json", {"a": 1})

        fake_bucket.blob.assert_called_once_with("path.json")
        blob.upload_from_string.assert_called_once()
        payload, kwargs = (
            blob.upload_from_string.call_args.args,
            blob.upload_from_string.call_args.kwargs,
        )
        assert json.loads(payload[0]) == {"a": 1}
        assert kwargs == {"content_type": "application/json"}

    @pytest.mark.asyncio
    async def test_serialises_non_json_native_types_via_default_str(
        self, fake_bucket: MagicMock
    ) -> None:
        """``json.dumps(..., default=str)`` is used so datetimes survive the
        write path. The repository layer already passes ``model_dump(mode="json")``,
        but this fallback prevents 500s on raw dicts."""
        from datetime import UTC, datetime

        blob = MagicMock()
        fake_bucket.blob.return_value = blob

        await gcs_module.write_json("path.json", {"when": datetime(2026, 1, 1, tzinfo=UTC)})
        sent: str = blob.upload_from_string.call_args.args[0]
        # Datetime stringified rather than crashing the encoder.
        assert "2026-01-01" in sent


class TestDeletePrefix:
    @pytest.mark.asyncio
    async def test_deletes_all_listed_blobs(self, fake_bucket: MagicMock) -> None:
        # ``_get_client().list_blobs`` returns the iterable; route both calls
        # through the same client mock that ``fake_bucket`` set up.
        client = gcs_module._get_client()  # the patched version
        b1, b2 = MagicMock(), MagicMock()
        client.list_blobs.return_value = [b1, b2]
        # ``_sync_delete_prefix`` looks the bucket up again via
        # ``_get_client().bucket(name)`` — it must return a bucket exposing
        # ``.delete_blobs``.
        delete_target = MagicMock()
        client.bucket.return_value = delete_target

        await gcs_module.delete_prefix("users/u1/")
        delete_target.delete_blobs.assert_called_once_with([b1, b2])

    @pytest.mark.asyncio
    async def test_noop_when_prefix_empty(self, fake_bucket: MagicMock) -> None:
        client = gcs_module._get_client()
        client.list_blobs.return_value = []
        delete_target = MagicMock()
        client.bucket.return_value = delete_target

        await gcs_module.delete_prefix("users/empty/")
        delete_target.delete_blobs.assert_not_called()


class TestListBlobs:
    @pytest.mark.asyncio
    async def test_returns_only_names(self, fake_bucket: MagicMock) -> None:
        client = gcs_module._get_client()
        b1, b2 = MagicMock(name="a"), MagicMock(name="b")
        b1.name = "a/1.json"
        b2.name = "a/2.json"
        client.list_blobs.return_value = [b1, b2]

        names = await gcs_module.list_blobs("a/")
        assert names == ["a/1.json", "a/2.json"]


class TestRunsViaToThread:
    """The async wrappers exist to keep the FastAPI event loop unblocked.
    Verify each delegates to ``asyncio.to_thread`` so the underlying sync
    storage call never runs on the loop."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "func_name,sync_name,kwargs",
        [
            ("read_json", "_sync_read_json", {"blob_name": "x"}),
            ("write_json", "_sync_write_json", {"blob_name": "x", "data": {"k": 1}}),
            ("delete_prefix", "_sync_delete_prefix", {"prefix": "p/"}),
            ("list_blobs", "_sync_list_blobs", {"prefix": "p/"}),
        ],
    )
    async def test_to_thread_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        func_name: str,
        sync_name: str,
        kwargs: dict[str, Any],
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_to_thread(fn: object, *args: Any, **kw: Any) -> str:
            captured["fn"] = fn
            captured["args"] = args
            return "sentinel"

        monkeypatch.setattr(gcs_module.asyncio, "to_thread", fake_to_thread)
        await getattr(gcs_module, func_name)(**kwargs)
        # Each wrapper hands the matching sync helper to ``to_thread``.
        assert captured["fn"] is getattr(gcs_module, sync_name)
