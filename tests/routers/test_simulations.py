"""Tests for the simulations router (``app.routers.simulations``).

Worker dispatch is replaced with a recorder; GCS is the in-memory fake. The
focus is on the contract between the API and its clients: status code paths,
which blobs get written, and which dispatches fire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.motor import SOLID_FORMULATIONS
from tests.conftest import ADMIN_UID, MEMBER_UID, FakeGCS, login_as, logout
from tests.routers.conftest import DispatchRecorder

OTHER_UID = "other-user-uid"


def _solid_config_dict() -> dict[str, Any]:
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


def _seed_motor(fake_gcs: FakeGCS, user_id: str, motor_id: str = "motor-1") -> None:
    fake_gcs.blobs[f"users/{user_id}/motors/{motor_id}.json"] = {
        "motor_id": motor_id,
        "name": "Olympus",
        "config": _solid_config_dict(),
    }


def _seed_simulation(
    fake_gcs: FakeGCS,
    user_id: str,
    simulation_id: str,
    *,
    motor_id: str = "motor-1",
    status: str = "pending",
    error: str | None = None,
) -> None:
    fake_gcs.blobs[f"users/{user_id}/simulations/{simulation_id}/config.json"] = {
        "simulation_id": simulation_id,
        "user_id": user_id,
        "motor_id": motor_id,
        "motor_config": _solid_config_dict(),
        "params": {
            "d_t": 0.001,
            "igniter_pressure": 1_000_000.0,
            "external_pressure": 101_325.0,
            "other_losses": 12.0,
        },
    }
    fake_gcs.blobs[f"users/{user_id}/simulations/{simulation_id}/status.json"] = {
        "simulation_id": simulation_id,
        "status": status,
        "error": error,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Auth matrix
# ---------------------------------------------------------------------------


PROTECTED_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/simulations", {"motor_id": "x"}),
    ("GET", "/simulations", None),
    ("GET", "/simulations/some-id/status", None),
    ("GET", "/simulations/some-id/results", None),
    ("DELETE", "/simulations/some-id", None),
    ("POST", "/admin/simulations/rerun-all", None),
]


class TestSimulationsAuthentication:
    @pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ENDPOINTS)
    def test_anonymous_rejected(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        logout(app)
        resp = client.request(method, path, json=body)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /simulations
# ---------------------------------------------------------------------------


class TestCreateSimulation:
    def test_dispatches_and_persists(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)

        resp = client.post(
            "/simulations",
            json={
                "motor_id": "motor-1",
                "params": {
                    "d_t": 0.001,
                    "igniter_pressure": 1_000_000.0,
                    "external_pressure": 101_325.0,
                    "other_losses": 12.0,
                },
            },
        )
        assert resp.status_code == 202
        sim_id = resp.json()["simulation_id"]

        # Config + status both written.
        assert f"users/{MEMBER_UID}/simulations/{sim_id}/config.json" in fake_gcs.blobs
        status = fake_gcs.blobs[f"users/{MEMBER_UID}/simulations/{sim_id}/status.json"]
        assert status["status"] == "pending"

        # Worker dispatched exactly once with the right ids.
        assert dispatch_recorder.calls == [(sim_id, MEMBER_UID)]

    def test_uses_default_params_when_omitted(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)
        resp = client.post("/simulations", json={"motor_id": "motor-1"})
        assert resp.status_code == 202

    def test_unknown_motor_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        resp = client.post("/simulations", json={"motor_id": "missing"})
        assert resp.status_code == 404
        # Nothing was dispatched or persisted.
        assert dispatch_recorder.calls == []
        assert fake_gcs.blobs == {}

    def test_other_users_motor_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        """A user must not be able to start a simulation against another
        user's motor by guessing its id."""
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_motor(fake_gcs, OTHER_UID, motor_id="other-motor")
        resp = client.post("/simulations", json={"motor_id": "other-motor"})
        assert resp.status_code == 404
        assert dispatch_recorder.calls == []


# ---------------------------------------------------------------------------
# GET /simulations
# ---------------------------------------------------------------------------


class TestListSimulations:
    def test_scoped_per_user(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_simulation(fake_gcs, MEMBER_UID, "mine")
        _seed_simulation(fake_gcs, OTHER_UID, "theirs")

        listing = client.get("/simulations").json()
        assert {s["simulation_id"] for s in listing} == {"mine"}

    def test_empty_listing(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.get("/simulations").json() == []


# ---------------------------------------------------------------------------
# GET /simulations/{simulation_id}/status
# ---------------------------------------------------------------------------


class TestGetSimulationStatus:
    def test_returns_status_record(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_simulation(fake_gcs, MEMBER_UID, "sim-1", status="running")
        body = client.get("/simulations/sim-1/status").json()
        assert body["status"] == "running"

    def test_unknown_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.get("/simulations/nope/status").status_code == 404


# ---------------------------------------------------------------------------
# GET /simulations/{simulation_id}/results
# ---------------------------------------------------------------------------


class TestGetSimulationResults:
    def _seed_done_with_results(
        self, fake_gcs: FakeGCS, user_id: str, simulation_id: str = "sim-1"
    ) -> None:
        _seed_simulation(fake_gcs, user_id, simulation_id, status="done")
        # Minimal valid SolidSimulationResultsSchema payload.
        fake_gcs.blobs[f"users/{user_id}/simulations/{simulation_id}/results.json"] = {
            "motor_type": "solid",
            "simulation_id": simulation_id,
            "t": [0.0, 0.1],
            "thrust": [0.0, 100.0],
            "P_0": [101_325.0, 1_000_000.0],
            "P_exit": [101_325.0, 200_000.0],
            "m_prop": [1.0, 0.9],
            "burn_area": [0.01, 0.01],
            "propellant_volume": [0.001, 0.0009],
            "free_chamber_volume": [0.002, 0.0021],
            "web": [0.0, 0.001],
            "burn_rate": [0.001, 0.001],
            "C_f": [1.5, 1.5],
            "C_f_ideal": [1.6, 1.6],
            "nozzle_efficiency": [0.95, 0.95],
            "overall_efficiency": [0.9, 0.9],
            "eta_div": [99.0, 99.0],
            "eta_kin": [98.0, 98.0],
            "eta_bl": [97.0, 97.0],
            "eta_2p": [96.0, 96.0],
            "grain_mass_flux": [[1.0], [1.1]],
            "propellant_cog": [[0.1, 0.0, 0.0], [0.1, 0.0, 0.0]],
            "propellant_moi": [[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]] * 2,
            "total_impulse": 10.0,
            "specific_impulse": 100.0,
            "thrust_time": 0.1,
            "burn_time": 0.1,
            "max_thrust": 100.0,
            "avg_thrust": 50.0,
            "max_chamber_pressure": 1_000_000.0,
            "avg_chamber_pressure": 500_000.0,
            "avg_nozzle_efficiency": 0.95,
            "avg_overall_efficiency": 0.9,
            "initial_propellant_mass": 1.0,
            "volumetric_efficiency": 0.5,
            "mean_klemmung": 100.0,
            "max_klemmung": 110.0,
            "initial_to_final_klemmung_ratio": 1.1,
            "max_mass_flux": 1.1,
            "burn_profile": "neutral",
        }

    def test_returns_full_payload_when_done(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        self._seed_done_with_results(fake_gcs, MEMBER_UID)

        body = client.get("/simulations/sim-1/results").json()
        assert body["simulation_id"] == "sim-1"
        assert body["motor_id"] == "motor-1"
        assert body["motor_config"]["motor_type"] == "solid"
        assert body["results"]["motor_type"] == "solid"

    def test_unknown_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.get("/simulations/nope/results").status_code == 404

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_pending_or_running_returns_409(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
        status: str,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_simulation(fake_gcs, MEMBER_UID, "sim-1", status=status)
        resp = client.get("/simulations/sim-1/results")
        assert resp.status_code == 409
        assert status in resp.json()["detail"]

    def test_failed_returns_422_with_error_detail(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_simulation(
            fake_gcs, MEMBER_UID, "sim-1", status="failed", error="numerical instability"
        )
        resp = client.get("/simulations/sim-1/results")
        assert resp.status_code == 422
        assert "numerical instability" in resp.json()["detail"]

    def test_done_without_results_blob_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        """Status says ``done`` but results.json is missing — a partial-write
        race. Caller gets a 404, not a 500."""
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_simulation(fake_gcs, MEMBER_UID, "sim-1", status="done")
        resp = client.get("/simulations/sim-1/results")
        assert resp.status_code == 404
        assert "Results" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /simulations/{simulation_id}
# ---------------------------------------------------------------------------


class TestDeleteSimulation:
    def test_removes_all_simulation_blobs(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        _seed_simulation(fake_gcs, MEMBER_UID, "sim-1")
        # Plus a results blob to confirm it gets removed too.
        fake_gcs.blobs[f"users/{MEMBER_UID}/simulations/sim-1/results.json"] = {
            "motor_type": "solid",
            "simulation_id": "sim-1",
        }

        assert client.delete("/simulations/sim-1").status_code == 204
        # Nothing left under the simulation prefix.
        assert not [
            k for k in fake_gcs.blobs if k.startswith(f"users/{MEMBER_UID}/simulations/sim-1/")
        ]

    def test_unknown_returns_404(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.delete("/simulations/no-such").status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/simulations/rerun-all
# ---------------------------------------------------------------------------


class TestRerunAllSimulations:
    def test_member_blocked(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="member", uid=MEMBER_UID)
        assert client.post("/admin/simulations/rerun-all").status_code == 403
        assert dispatch_recorder.calls == []

    def test_admin_redispatches_every_simulation(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        _seed_simulation(fake_gcs, "u1", "sim-a", status="done")
        _seed_simulation(fake_gcs, "u2", "sim-b", status="failed")

        resp = client.post("/admin/simulations/rerun-all")
        assert resp.status_code == 202
        body = resp.json()
        assert body["triggered"] == 2
        assert set(body["simulation_ids"]) == {"sim-a", "sim-b"}

        # Both simulations dispatched, each with the right user_id.
        assert sorted(dispatch_recorder.calls) == [("sim-a", "u1"), ("sim-b", "u2")]

        # Statuses reset to pending for both.
        for user_id, sim_id in [("u1", "sim-a"), ("u2", "sim-b")]:
            status = fake_gcs.blobs[f"users/{user_id}/simulations/{sim_id}/status.json"]
            assert status["status"] == "pending"

    def test_simulation_without_config_is_skipped(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        _seed_simulation(fake_gcs, "u1", "sim-good")
        # Orphan: only a status blob, no config — must not crash, must not dispatch.
        fake_gcs.blobs["users/u2/simulations/sim-orphan/status.json"] = {
            "simulation_id": "sim-orphan",
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }

        resp = client.post("/admin/simulations/rerun-all")
        assert resp.status_code == 202
        assert resp.json()["simulation_ids"] == ["sim-good"]
        assert dispatch_recorder.calls == [("sim-good", "u1")]

    def test_no_simulations_returns_zero(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, role="admin", uid=ADMIN_UID)
        resp = client.post("/admin/simulations/rerun-all")
        assert resp.status_code == 202
        assert resp.json() == {"triggered": 0, "simulation_ids": []}
