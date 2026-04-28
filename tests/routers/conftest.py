"""Router-test fixtures: fake Firebase Admin SDK + no-op worker dispatch.

The in-memory GCS fixture (``fake_gcs``) lives in the top-level conftest so
worker / repository tests can share it.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from firebase_admin import auth as firebase_auth_module

import app.routers.users as users_router_module
from app.worker import dispatch as dispatch_module
from tests.conftest import FakeGCS  # noqa: F401

# ---------------------------------------------------------------------------
# Worker dispatch — record calls, never spawn anything.
# ---------------------------------------------------------------------------


class DispatchRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def trigger(self, simulation_id: str, user_id: str) -> None:
        self.calls.append((simulation_id, user_id))


@pytest.fixture()
def dispatch_recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[DispatchRecorder]:
    recorder = DispatchRecorder()
    monkeypatch.setattr(dispatch_module, "trigger_simulation", recorder.trigger)

    # The simulations router imported the symbol at module load time, so we
    # also patch the router's reference to it.
    import app.routers.simulations as simulations_router_module

    monkeypatch.setattr(simulations_router_module, "trigger_simulation", recorder.trigger)
    yield recorder


# ---------------------------------------------------------------------------
# Fake firebase_admin.auth — backs the users router.
# ---------------------------------------------------------------------------


def _make_record(
    uid: str,
    email: str | None = None,
    display_name: str | None = None,
    disabled: bool = False,
    custom_claims: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid,
        email=email or f"{uid}@example.com",
        display_name=display_name,
        disabled=disabled,
        custom_claims=custom_claims,
    )


class FakeFirebase:
    def __init__(self, records: list[SimpleNamespace] | None = None) -> None:
        self.records: dict[str, SimpleNamespace] = {r.uid: r for r in (records or [])}
        self.deleted: list[str] = []

    def list_users(
        self,
        page_token: str | None = None,
        max_results: int = 1000,
        app: Any = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(users=list(self.records.values()), next_page_token=None)

    def get_user(self, uid: str, app: Any = None) -> SimpleNamespace:
        if uid not in self.records:
            raise firebase_auth_module.UserNotFoundError(f"No user {uid}")
        return self.records[uid]

    def update_user(self, uid: str, *, disabled: bool, app: Any = None) -> SimpleNamespace:
        if uid not in self.records:
            raise firebase_auth_module.UserNotFoundError(f"No user {uid}")
        self.records[uid].disabled = disabled
        return self.records[uid]

    def set_custom_user_claims(
        self, uid: str, claims: dict[str, Any] | None, app: Any = None
    ) -> None:
        if uid not in self.records:
            raise firebase_auth_module.UserNotFoundError(f"No user {uid}")
        self.records[uid].custom_claims = claims

    def delete_user(self, uid: str, app: Any = None) -> None:
        if uid not in self.records:
            raise firebase_auth_module.UserNotFoundError(f"No user {uid}")
        self.deleted.append(uid)
        del self.records[uid]


@pytest.fixture()
def fake_firebase(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFirebase]:
    """Pre-populated with admin / member / target accounts so admin-endpoint
    tests can refer to them without re-creating fixtures everywhere."""
    fb = FakeFirebase(
        records=[
            _make_record("admin-uid", custom_claims={"role": "admin"}),
            _make_record("member-uid"),
            _make_record("target-uid", display_name="Target User"),
        ]
    )
    monkeypatch.setattr(users_router_module.firebase_auth, "list_users", fb.list_users)
    monkeypatch.setattr(users_router_module.firebase_auth, "get_user", fb.get_user)
    monkeypatch.setattr(users_router_module.firebase_auth, "update_user", fb.update_user)
    monkeypatch.setattr(
        users_router_module.firebase_auth, "set_custom_user_claims", fb.set_custom_user_claims
    )
    monkeypatch.setattr(users_router_module.firebase_auth, "delete_user", fb.delete_user)
    yield fb
