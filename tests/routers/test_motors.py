"""Tests for the motor CRUD router (``app.routers.motors``)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.motor import BILIQUID_FORMULATIONS, SOLID_FORMULATIONS
from tests.conftest import MEMBER_UID, FakeGCS, login_as, logout

OTHER_UID = "other-user-uid"


def _solid_config() -> dict[str, Any]:
    return {
        "motor_type": "solid",
        "propellant_id": next(iter(SOLID_FORMULATIONS)),
        "grain": {
            "segments": [
                {"type": "bates", "outer_diameter": 0.069, "core_diameter": 0.025, "length": 0.12}
            ],
            "spacing": 0.005,
        },
        "thrust_chamber": {
            "nozzle": {
                "inlet_diameter": 0.060,
                "throat_diameter": 0.015,
                "divergent_angle": 12,
                "convergent_angle": 45,
                "expansion_ratio": 8,
            },
            "combustion_chamber": {
                "casing_inner_diameter": 0.0702,
                "casing_outer_diameter": 0.0762,
                "internal_length": 0.280,
            },
            "dry_mass": 1.5,
            "nozzle_exit_to_grain_port_distance": 0.010,
        },
    }


def _liquid_config() -> dict[str, Any]:
    return {
        "motor_type": "liquid",
        "propellant_id": next(iter(BILIQUID_FORMULATIONS)),
        "thrust_chamber": {
            "nozzle": {
                "inlet_diameter": 0.060,
                "throat_diameter": 0.015,
                "divergent_angle": 12,
                "convergent_angle": 45,
                "expansion_ratio": 8,
            },
            "injector": {
                "discharge_coefficient_fuel": 0.7,
                "discharge_coefficient_oxidizer": 0.7,
                "area_fuel": 2e-5,
                "area_ox": 4e-5,
            },
            "combustion_chamber": {
                "casing_inner_diameter": 0.0702,
                "casing_outer_diameter": 0.0762,
                "internal_length": 0.280,
            },
            "dry_mass": 8.0,
        },
        "feed_system": {
            "type": "stacked_tank_pressure_fed",
            "oxidizer_line_diameter": 0.012,
            "oxidizer_line_length": 0.5,
            "fuel_line_diameter": 0.012,
            "fuel_line_length": 0.5,
            "fuel_tank": {
                "fluid_name": "Hydrogen",
                "volume": 0.07,
                "temperature": 22.0,
                "initial_fluid_mass": 5.0,
            },
            "oxidizer_tank": {
                "fluid_name": "Oxygen",
                "volume": 0.05,
                "temperature": 90.0,
                "initial_fluid_mass": 30.0,
            },
        },
    }


# ---------------------------------------------------------------------------
# Authentication — every motor endpoint requires a signed-in user.
# ---------------------------------------------------------------------------


PROTECTED_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/motors", {"name": "x", "config": _solid_config()}),
    ("GET", "/motors", None),
    ("GET", "/motors/some-id", None),
    ("PUT", "/motors/some-id", {"name": "x"}),
    ("DELETE", "/motors/some-id", None),
]


class TestMotorAuthentication:
    @pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ENDPOINTS)
    def test_anonymous_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        logout(app)
        resp = client.request(method, path, json=body)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /motors
# ---------------------------------------------------------------------------


class TestCreateMotor:
    def test_creates_solid_motor(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        resp = client.post("/motors", json={"name": "Olympus", "config": _solid_config()})
        assert resp.status_code == 201
        motor_id = resp.json()["motor_id"]

        # Blob written under the caller's path.
        assert f"users/{MEMBER_UID}/motors/{motor_id}.json" in fake_gcs.blobs

    def test_creates_liquid_motor(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        resp = client.post("/motors", json={"name": "LRE-1", "config": _liquid_config()})
        assert resp.status_code == 201
        # The persisted record carries the discriminator.
        motor_id = resp.json()["motor_id"]
        stored = fake_gcs.blobs[f"users/{MEMBER_UID}/motors/{motor_id}.json"]
        assert stored["config"]["motor_type"] == "liquid"

    def test_invalid_config_rejected(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member")
        bad_config = _solid_config()
        bad_config["propellant_id"] = "DEFINITELY_NOT_REAL"
        resp = client.post("/motors", json={"name": "x", "config": bad_config})
        assert resp.status_code == 422

    def test_returns_unique_motor_ids(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        """``motor_id`` is a server-issued UUID — two creates must yield two
        distinct ids so a client racing two requests can't collide."""
        login_as(app, role="member", uid=MEMBER_UID)
        a = client.post("/motors", json={"name": "A", "config": _solid_config()}).json()
        b = client.post("/motors", json={"name": "B", "config": _solid_config()}).json()
        assert a["motor_id"] != b["motor_id"]


# ---------------------------------------------------------------------------
# GET /motors
# ---------------------------------------------------------------------------


class TestListMotors:
    def test_returns_only_callers_motors(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        resp = client.post("/motors", json={"name": "Mine", "config": _solid_config()})
        assert resp.status_code == 201

        # A motor under a different user — current request must not see it.
        fake_gcs.blobs[f"users/{OTHER_UID}/motors/leaked.json"] = {
            "motor_id": "leaked",
            "name": "Other",
            "config": _solid_config(),
        }

        listing = client.get("/motors").json()
        assert {m["name"] for m in listing} == {"Mine"}

    def test_sorted_by_updated_at_descending(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        from datetime import UTC, datetime, timedelta

        login_as(app, role="member", uid=MEMBER_UID)
        now = datetime.now(UTC)
        for i, ts in enumerate([now - timedelta(hours=2), now, now - timedelta(hours=1)]):
            fake_gcs.blobs[f"users/{MEMBER_UID}/motors/m{i}.json"] = {
                "motor_id": f"m{i}",
                "name": f"motor-{i}",
                "created_at": ts.isoformat(),
                "updated_at": ts.isoformat(),
                "config": _solid_config(),
            }

        listing = client.get("/motors").json()
        assert [m["motor_id"] for m in listing] == ["m1", "m2", "m0"]

    def test_empty_listing(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.get("/motors").json() == []


# ---------------------------------------------------------------------------
# GET /motors/{motor_id}
# ---------------------------------------------------------------------------


class TestGetMotor:
    def test_returns_full_record(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        motor_id = client.post(
            "/motors", json={"name": "Olympus", "config": _solid_config()}
        ).json()["motor_id"]

        body = client.get(f"/motors/{motor_id}").json()
        assert body["motor_id"] == motor_id
        assert body["config"]["motor_type"] == "solid"

    def test_missing_returns_404(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.get("/motors/does-not-exist").status_code == 404

    def test_other_users_motor_returns_404(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        """Cross-user reads are not authorised — they look like a 404, never a
        200. Anything else would leak which motor IDs exist for other users."""
        login_as(app, role="member", uid=MEMBER_UID)
        fake_gcs.blobs[f"users/{OTHER_UID}/motors/sneaky.json"] = {
            "motor_id": "sneaky",
            "name": "x",
            "config": _solid_config(),
        }
        assert client.get("/motors/sneaky").status_code == 404


# ---------------------------------------------------------------------------
# PUT /motors/{motor_id}
# ---------------------------------------------------------------------------


class TestUpdateMotor:
    def test_partial_update_keeps_unspecified_fields(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        motor_id = client.post(
            "/motors", json={"name": "Olympus", "config": _solid_config()}
        ).json()["motor_id"]

        resp = client.put(f"/motors/{motor_id}", json={"name": "Olympus V2"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Olympus V2"
        assert body["config"]["motor_type"] == "solid"  # config preserved

    def test_update_bumps_updated_at(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        motor_id = client.post(
            "/motors", json={"name": "Olympus", "config": _solid_config()}
        ).json()["motor_id"]
        before = client.get(f"/motors/{motor_id}").json()

        after = client.put(f"/motors/{motor_id}", json={"name": "Olympus V2"}).json()

        assert after["created_at"] == before["created_at"]
        assert after["updated_at"] >= before["updated_at"]

    def test_missing_returns_404(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.put("/motors/no-such-id", json={"name": "x"}).status_code == 404

    def test_other_users_motor_returns_404(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        fake_gcs.blobs[f"users/{OTHER_UID}/motors/sneaky.json"] = {
            "motor_id": "sneaky",
            "name": "x",
            "config": _solid_config(),
        }
        assert client.put("/motors/sneaky", json={"name": "y"}).status_code == 404


# ---------------------------------------------------------------------------
# DELETE /motors/{motor_id}
# ---------------------------------------------------------------------------


class TestDeleteMotor:
    def test_removes_blob(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        motor_id = client.post(
            "/motors", json={"name": "Olympus", "config": _solid_config()}
        ).json()["motor_id"]

        resp = client.delete(f"/motors/{motor_id}")
        assert resp.status_code == 204
        assert f"users/{MEMBER_UID}/motors/{motor_id}.json" not in fake_gcs.blobs

    def test_missing_returns_404(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.delete("/motors/no-such-id").status_code == 404

    def test_other_users_motor_returns_404_and_doesnt_delete(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        fake_gcs.blobs[f"users/{OTHER_UID}/motors/sneaky.json"] = {
            "motor_id": "sneaky",
            "name": "x",
            "config": _solid_config(),
        }
        assert client.delete("/motors/sneaky").status_code == 404
        assert f"users/{OTHER_UID}/motors/sneaky.json" in fake_gcs.blobs
