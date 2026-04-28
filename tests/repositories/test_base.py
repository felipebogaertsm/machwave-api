"""Tests for ``app.repositories.base.GCSRepository`` — verify the protected
delegation methods route to the ``app.storage.gcs`` helpers correctly."""

from __future__ import annotations

import pytest

from app.repositories.base import GCSRepository
from tests.conftest import FakeGCS


class TestGCSRepositoryDelegation:
    @pytest.mark.asyncio
    async def test_read_returns_none_for_missing(self, fake_gcs: FakeGCS) -> None:
        repo = GCSRepository()
        assert await repo._read("missing.json") is None

    @pytest.mark.asyncio
    async def test_write_then_read_round_trips(self, fake_gcs: FakeGCS) -> None:
        repo = GCSRepository()
        await repo._write("a/b.json", {"foo": "bar"})
        assert await repo._read("a/b.json") == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_delete_prefix_removes_only_matching_blobs(self, fake_gcs: FakeGCS) -> None:
        repo = GCSRepository()
        await repo._write("a/keep.json", {"v": 1})
        await repo._write("b/c/drop1.json", {"v": 2})
        await repo._write("b/c/drop2.json", {"v": 3})
        await repo._write("b/other.json", {"v": 4})

        await repo._delete("b/c/")

        assert await repo._read("a/keep.json") == {"v": 1}
        assert await repo._read("b/c/drop1.json") is None
        assert await repo._read("b/c/drop2.json") is None
        assert await repo._read("b/other.json") == {"v": 4}

    @pytest.mark.asyncio
    async def test_list_returns_only_prefix_matches(self, fake_gcs: FakeGCS) -> None:
        repo = GCSRepository()
        await repo._write("users/u1/motors/m1.json", {})
        await repo._write("users/u1/motors/m2.json", {})
        await repo._write("users/u2/motors/m3.json", {})

        assert await repo._list("users/u1/motors/") == [
            "users/u1/motors/m1.json",
            "users/u1/motors/m2.json",
        ]
