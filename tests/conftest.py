"""Shared fixtures for the whole test suite.

Settings env vars must be present before any ``app.*`` import — pydantic-settings
reads them at ``Settings()`` construction time, which happens transitively
when most app modules are imported.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Force test-only settings before anything imports app modules. Done at import
# time of conftest.py so individual test modules don't have to remember.
# ---------------------------------------------------------------------------
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
os.environ.setdefault("GCP_PROJECT_ID", "test-gcp")
os.environ.setdefault("ENV", "prod")

import copy  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth.firebase import get_current_user, get_firebase_app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import gcs as gcs_module  # noqa: E402

ADMIN_UID = "admin-uid"
MEMBER_UID = "member-uid"
TARGET_UID = "target-uid"


# ---------------------------------------------------------------------------
# Shared in-memory GCS — used by every test that touches the storage layer.
# ---------------------------------------------------------------------------


class FakeGCS:
    """Dict-backed implementation of the four ``app.storage.gcs`` helpers.

    Each operation is async to match the real signatures, and reads return
    deep copies so test mutations don't bleed back into the store.
    """

    def __init__(self) -> None:
        self.blobs: dict[str, dict[str, Any]] = {}

    async def read_json(self, blob_name: str) -> dict[str, Any] | None:
        data = self.blobs.get(blob_name)
        return copy.deepcopy(data) if data is not None else None

    async def write_json(self, blob_name: str, data: dict[str, Any]) -> None:
        self.blobs[blob_name] = copy.deepcopy(data)

    async def delete_prefix(self, prefix: str) -> None:
        for name in list(self.blobs):
            if name.startswith(prefix):
                del self.blobs[name]

    async def list_blobs(self, prefix: str) -> list[str]:
        return sorted(name for name in self.blobs if name.startswith(prefix))


@pytest.fixture()
def fake_gcs(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGCS]:
    fake = FakeGCS()
    monkeypatch.setattr(gcs_module, "read_json", fake.read_json)
    monkeypatch.setattr(gcs_module, "write_json", fake.write_json)
    monkeypatch.setattr(gcs_module, "delete_prefix", fake.delete_prefix)
    monkeypatch.setattr(gcs_module, "list_blobs", fake.list_blobs)
    yield fake


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """``get_settings`` is ``lru_cache``d — clear it so per-test env-var
    monkeypatches are honoured rather than masked by an earlier resolution."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def app() -> Iterator[FastAPI]:
    """A fresh FastAPI app with a dummy Firebase app injected.

    ``get_firebase_app`` would otherwise call ``firebase_admin.initialize_app``
    against a real project; the override returns a placeholder object that
    routers happily forward to ``app=`` parameters on stubbed Firebase calls.
    """
    application = create_app()
    application.dependency_overrides[get_firebase_app] = lambda: object()
    yield application
    application.dependency_overrides.clear()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def login_as(
    app: FastAPI,
    *,
    role: str | None = None,
    uid: str = MEMBER_UID,
    email: str | None = None,
) -> dict[str, Any]:
    """Override ``get_current_user`` to simulate a signed-in user.

    ``role=None`` simulates a token without the ``role`` custom claim — the
    default which ``app.auth.rbac.get_user_role`` resolves to ``member``.
    """
    user: dict[str, Any] = {"uid": uid, "email": email or f"{uid}@example.com"}
    if role is not None:
        user["role"] = role
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def logout(app: FastAPI) -> None:
    app.dependency_overrides.pop(get_current_user, None)
