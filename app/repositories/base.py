from __future__ import annotations

from typing import Any

from app.storage import gcs


class GCSRepository:
    """Base class that maps repository operations to GCS primitives.

    Subclasses call the protected methods below instead of importing gcs
    directly, which keeps the storage coupling in one place and makes unit
    tests straightforward to write (mock _read/_write/_delete/_list).
    """

    async def _read(self, blob_name: str) -> dict[str, Any] | None:
        return await gcs.read_json(blob_name)

    async def _write(self, blob_name: str, data: dict[str, Any]) -> None:
        await gcs.write_json(blob_name, data)

    async def _delete(self, prefix: str) -> None:
        await gcs.delete_prefix(prefix)

    async def _list(self, prefix: str) -> list[str]:
        return await gcs.list_blobs(prefix)
