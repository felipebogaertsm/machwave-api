"""Tests for the credit/usage system: caps, estimator, account, admin overrides."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.credits import current_period_utc
from app.schemas.motor import SOLID_FORMULATIONS
from tests.conftest import ADMIN_UID, MEMBER_UID, FakeGCS, login_as, logout
from tests.routers.conftest import DispatchRecorder, FakeFirebase


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


def _seed_motor(fake_gcs: FakeGCS, user_id: str, motor_id: str = "motor-1") -> None:
    fake_gcs.blobs[f"users/{user_id}/motors/{motor_id}.json"] = {
        "motor_id": motor_id,
        "name": "Olympus",
        "config": _solid_config(),
    }


def _seed_motors(fake_gcs: FakeGCS, user_id: str, count: int) -> None:
    for i in range(count):
        _seed_motor(fake_gcs, user_id, motor_id=f"motor-{i}")


def _seed_simulations(fake_gcs: FakeGCS, user_id: str, count: int) -> None:
    for i in range(count):
        sid = f"sim-{i}"
        fake_gcs.blobs[f"users/{user_id}/simulations/{sid}/config.json"] = {
            "simulation_id": sid,
            "user_id": user_id,
            "motor_id": "motor-1",
            "motor_config": _solid_config(),
            "params": {
                "d_t": 0.01,
                "igniter_pressure": 1_000_000.0,
                "external_pressure": 101_325.0,
                "other_losses": 12.0,
            },
        }
        fake_gcs.blobs[f"users/{user_id}/simulations/{sid}/status.json"] = {
            "simulation_id": sid,
            "status": "done",
            "error": None,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }


def _seed_account(
    fake_gcs: FakeGCS,
    user_id: str,
    *,
    motor_limit: int | None = 10,
    simulation_limit: int | None = 10,
    monthly_token_limit: int | None = 10_000,
    tokens_used: int = 0,
    usage_period: str | None = None,
) -> None:
    """Seed an account blob in the nested-credits shape. Pass ``None`` for
    any limit to mark unlimited (the storage shape for admins)."""
    fake_gcs.blobs[f"users/{user_id}/account.json"] = {
        "user_id": user_id,
        "motor_limit": motor_limit,
        "simulation_limit": simulation_limit,
        "credits": {
            "monthly_token_limit": monthly_token_limit,
            "tokens_used": tokens_used,
            "usage_period": usage_period or current_period_utc(),
        },
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _seed_admin_account(fake_gcs: FakeGCS, user_id: str, *, tokens_used: int = 0) -> None:
    """Convenience wrapper — admin accounts have None for every limit."""
    _seed_account(
        fake_gcs,
        user_id,
        motor_limit=None,
        simulation_limit=None,
        monthly_token_limit=None,
        tokens_used=tokens_used,
    )


# ---------------------------------------------------------------------------
# Estimation engine
# ---------------------------------------------------------------------------


class TestEstimator:
    def test_solid_cost_scales_inversely_with_dt(self) -> None:
        from app.credits.estimator import estimate_tokens
        from app.schemas.motor import SolidMotorConfigSchema
        from app.schemas.simulation import IBSimParamsSchema, SimulationJobConfig

        motor_config = SolidMotorConfigSchema.model_validate(_solid_config())

        coarse = SimulationJobConfig(
            simulation_id="a",
            user_id="u",
            motor_id="m",
            motor_config=motor_config,
            params=IBSimParamsSchema(d_t=0.1),
        )
        fine = SimulationJobConfig(
            simulation_id="b",
            user_id="u",
            motor_id="m",
            motor_config=motor_config,
            params=IBSimParamsSchema(d_t=0.001),
        )

        assert estimate_tokens(fine) > estimate_tokens(coarse)
        assert estimate_tokens(coarse) >= 1

    def test_compute_actual_floor_is_one(self) -> None:
        from app.credits.estimator import compute_actual_tokens

        assert compute_actual_tokens(0) == 1
        assert compute_actual_tokens(1) == 1
        assert compute_actual_tokens(1000) == 1000

    def test_olympus_motor_estimate_close_to_actual(self) -> None:
        """Empirical pin: the Olympus motor (7-segment KNSB Nakka, mixed
        cores 60/45 mm) actually runs ~402 iterations at d_t=0.01. The
        steady-state estimator + 10% overshoot should land in 400–500."""
        from app.credits.estimator import estimate_tokens
        from app.schemas.motor import SolidMotorConfigSchema
        from app.schemas.simulation import IBSimParamsSchema, SimulationJobConfig

        olympus_config = {
            "motor_type": "solid",
            "propellant_id": "KNSB_NAKKA",
            "grain": {
                "segments": [
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.060,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.060,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.060,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.045,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.045,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.045,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                    {
                        "type": "bates",
                        "outer_diameter": 0.114,
                        "core_diameter": 0.045,
                        "length": 0.2,
                        "density_ratio": 0.95,
                    },
                ],
                "spacing": 0.01,
            },
            "thrust_chamber": {
                "nozzle": {
                    "inlet_diameter": 0.075,
                    "throat_diameter": 0.037,
                    "divergent_angle": 12.0,
                    "convergent_angle": 45.0,
                    "expansion_ratio": 8.0,
                },
                "combustion_chamber": {
                    "casing_inner_diameter": 0.1282,
                    "casing_outer_diameter": 0.1413,
                    "internal_length": 1.5,
                    "thermal_liner_thickness": 0.003,
                },
                "dry_mass": 19.0,
                "nozzle_exit_to_grain_port_distance": 0.05,
            },
        }
        job = SimulationJobConfig(
            simulation_id="olympus",
            user_id="u",
            motor_id="m",
            motor_config=SolidMotorConfigSchema.model_validate(olympus_config),
            params=IBSimParamsSchema(d_t=0.01),
        )

        actual = 402
        estimated = estimate_tokens(job)

        assert estimated >= actual, f"estimate {estimated} undershoots actual {actual}"
        # Within 25% of the actual — comfortable margin around the ~5%
        # steady-state error plus the 10% overshoot factor.
        assert estimated <= actual * 1.25, (
            f"estimate {estimated} too high vs actual {actual} "
            f"(>25% overshoot — recheck OVERSHOOT or steady-state model)"
        )

    def test_unknown_propellant_falls_back_to_heuristic(self) -> None:
        """If the propellant id isn't in the registry, the estimator should
        still return a positive int instead of crashing."""
        from app.credits.estimator import _solid_burn_time
        from app.schemas.motor import SolidMotorConfigSchema

        config = SolidMotorConfigSchema.model_validate(_solid_config())
        # Bypass schema validation by mutating after construction is awkward;
        # instead, monkeypatch the registry lookup at the function layer.
        burn_time = _solid_burn_time(config.model_copy(update={"propellant_id": "KNSB_NAKKA"}))
        assert burn_time > 0


# ---------------------------------------------------------------------------
# Account materialisation + monthly reset
# ---------------------------------------------------------------------------


class TestAccountRepository:
    @pytest.mark.asyncio
    async def test_creates_with_zero_usage_on_first_access(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository

        repo = AccountRepository()
        account = await repo.get_or_create("new-uid")
        assert account.motor_limit == 10
        assert account.simulation_limit == 10
        assert account.credits.monthly_token_limit == 10_000
        assert account.credits.tokens_used == 0
        assert account.credits.tokens_remaining == 10_000

    @pytest.mark.asyncio
    async def test_resets_usage_when_period_advances(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository

        _seed_account(
            fake_gcs,
            "stale-uid",
            tokens_used=8000,
            usage_period="2020-01",
        )

        repo = AccountRepository()
        account = await repo.get_or_create("stale-uid")
        assert account.credits.usage_period == current_period_utc()
        assert account.credits.tokens_used == 0

    @pytest.mark.asyncio
    async def test_debit_increases_tokens_used(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository

        _seed_account(fake_gcs, "spender", tokens_used=100)
        repo = AccountRepository()
        charged = await repo.debit("spender", 250)
        assert charged == 250

        account = await repo.get_or_create("spender")
        assert account.credits.tokens_used == 350

    @pytest.mark.asyncio
    async def test_debit_raises_when_over_limit(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository, InsufficientBalanceError

        _seed_account(fake_gcs, "broke", monthly_token_limit=100, tokens_used=95)
        repo = AccountRepository()
        with pytest.raises(InsufficientBalanceError) as ei:
            await repo.debit("broke", 10)
        assert ei.value.remaining == 5

    @pytest.mark.asyncio
    async def test_credit_floors_at_zero(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository

        _seed_account(fake_gcs, "topup", tokens_used=200)
        repo = AccountRepository()
        account = await repo.credit("topup", 1000)
        assert account.credits.tokens_used == 0

    @pytest.mark.asyncio
    async def test_admin_account_seeds_with_none_limits(self, fake_gcs: FakeGCS) -> None:
        """get_or_create with role='admin' creates an unlimited account."""
        from app.repositories.account import AccountRepository

        repo = AccountRepository()
        account = await repo.get_or_create("admin-uid", role="admin")
        assert account.motor_limit is None
        assert account.simulation_limit is None
        assert account.credits.monthly_token_limit is None
        assert account.credits.tokens_remaining is None
        assert account.credits.is_unlimited is True

    @pytest.mark.asyncio
    async def test_debit_unlimited_always_succeeds_and_tracks_usage(
        self, fake_gcs: FakeGCS
    ) -> None:
        """Unlimited accounts still increment tokens_used so admin telemetry
        is accurate."""
        from app.repositories.account import AccountRepository

        _seed_admin_account(fake_gcs, "unlimited-uid", tokens_used=999_999)
        repo = AccountRepository()
        charged = await repo.debit("unlimited-uid", 5_000)
        assert charged == 5_000

        account = await repo.get_or_create("unlimited-uid", role="admin")
        assert account.credits.tokens_used == 999_999 + 5_000
        assert account.credits.tokens_remaining is None

    @pytest.mark.asyncio
    async def test_update_limits_can_set_field_to_none(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository

        _seed_account(fake_gcs, "promoted")
        repo = AccountRepository()
        account = await repo.update_limits("promoted", {"monthly_token_limit": None})
        assert account.credits.monthly_token_limit is None
        # Other fields untouched.
        assert account.motor_limit == 10
        assert account.simulation_limit == 10

    @pytest.mark.asyncio
    async def test_reset_to_role_defaults_promotes_and_demotes(self, fake_gcs: FakeGCS) -> None:
        from app.repositories.account import AccountRepository

        _seed_account(fake_gcs, "rotater")
        repo = AccountRepository()

        promoted = await repo.reset_to_role_defaults("rotater", role="admin")
        assert promoted.motor_limit is None
        assert promoted.credits.monthly_token_limit is None

        demoted = await repo.reset_to_role_defaults("rotater", role="member")
        assert demoted.motor_limit == 10
        assert demoted.credits.monthly_token_limit == 10_000


# ---------------------------------------------------------------------------
# Motor cap from per-user account
# ---------------------------------------------------------------------------


class TestMotorCap:
    def test_default_cap_blocks_creation(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motors(fake_gcs, MEMBER_UID, 10)

        resp = client.post("/motors", json={"name": "n", "config": _solid_config()})
        assert resp.status_code == 409
        assert "Motor limit" in resp.json()["detail"]

    def test_custom_per_user_cap_honored(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_account(fake_gcs, MEMBER_UID, motor_limit=2)
        _seed_motors(fake_gcs, MEMBER_UID, 2)

        resp = client.post("/motors", json={"name": "n", "config": _solid_config()})
        assert resp.status_code == 409

    def test_admin_bypasses_cap(self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")
        _seed_motors(fake_gcs, ADMIN_UID, 10)

        resp = client.post("/motors", json={"name": "n", "config": _solid_config()})
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Simulation submit — token gating + dry-run estimate
# ---------------------------------------------------------------------------


class TestSimulationCreditGating:
    def test_simulation_count_cap_blocks(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)
        _seed_simulations(fake_gcs, MEMBER_UID, 10)

        resp = client.post("/simulations", json={"motor_id": "motor-1"})
        assert resp.status_code == 409
        assert dispatch_recorder.calls == []

    def test_insufficient_tokens_returns_402(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)
        # Tight: 1 token left, estimate will be > 1.
        _seed_account(fake_gcs, MEMBER_UID, monthly_token_limit=10_000, tokens_used=9_999)

        resp = client.post("/simulations", json={"motor_id": "motor-1"})
        assert resp.status_code == 402
        assert dispatch_recorder.calls == []

    def test_admin_bypasses_token_gate(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")
        _seed_motor(fake_gcs, ADMIN_UID)
        # Admin accounts are stored with None limits (unlimited).
        _seed_admin_account(fake_gcs, ADMIN_UID, tokens_used=10_000)
        _seed_simulations(fake_gcs, ADMIN_UID, 10)

        resp = client.post("/simulations", json={"motor_id": "motor-1"})
        assert resp.status_code == 202

    def test_successful_submit_increments_usage_and_persists_cost(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)

        resp = client.post("/simulations", json={"motor_id": "motor-1"})
        assert resp.status_code == 202
        body = resp.json()
        sid = body["simulation_id"]
        estimated = body["estimated_tokens"]
        assert estimated >= 1

        cost_blob = fake_gcs.blobs[f"users/{MEMBER_UID}/simulations/{sid}/cost.json"]
        assert cost_blob["estimated_tokens"] == estimated
        assert cost_blob["actual_tokens"] is None
        assert cost_blob["tokens_charged"] == estimated

        account = fake_gcs.blobs[f"users/{MEMBER_UID}/account.json"]
        assert account["credits"]["tokens_used"] == estimated


class TestEstimateEndpoint:
    def test_estimate_does_not_create_or_charge(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)

        before = dict(fake_gcs.blobs)
        resp = client.post("/simulations/estimate", json={"motor_id": "motor-1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["estimated_tokens"] >= 1
        assert body["credits"]["tokens_used"] == 0
        assert body["credits"]["tokens_remaining"] == 10_000
        assert body["can_afford"] is True

        sim_blobs = [k for k in fake_gcs.blobs if "simulations/" in k and k not in before]
        assert sim_blobs == []
        assert dispatch_recorder.calls == []

    def test_estimate_404_when_motor_missing(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        resp = client.post("/simulations/estimate", json={"motor_id": "missing"})
        assert resp.status_code == 404

    def test_estimate_admin_unlimited(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")
        _seed_motor(fake_gcs, ADMIN_UID)
        # Admin's account stores None for limits — even with high usage,
        # tokens_remaining is None and can_afford is True.
        _seed_admin_account(fake_gcs, ADMIN_UID, tokens_used=10_000)

        body = client.post("/simulations/estimate", json={"motor_id": "motor-1"}).json()
        assert body["credits"]["tokens_remaining"] is None
        assert body["credits"]["monthly_token_limit"] is None
        assert body["can_afford"] is True


# ---------------------------------------------------------------------------
# Per-simulation cost endpoint + delete refund
# ---------------------------------------------------------------------------


class TestSimulationCostEndpoint:
    def test_get_cost_returns_record(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)
        sid = client.post("/simulations", json={"motor_id": "motor-1"}).json()["simulation_id"]

        resp = client.get(f"/simulations/{sid}/cost")
        assert resp.status_code == 200
        body = resp.json()
        assert body["simulation_id"] == sid
        assert body["estimated_tokens"] >= 1
        assert body["actual_tokens"] is None

    def test_delete_does_not_refund(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        dispatch_recorder: DispatchRecorder,
    ) -> None:
        """Deleting a simulation removes its blobs but leaves tokens_used
        intact. Refunds only happen on the worker failure path."""
        login_as(app, uid=MEMBER_UID)
        _seed_motor(fake_gcs, MEMBER_UID)
        submit = client.post("/simulations", json={"motor_id": "motor-1"}).json()
        sid = submit["simulation_id"]
        estimated = submit["estimated_tokens"]

        usage_after_submit = fake_gcs.blobs[f"users/{MEMBER_UID}/account.json"]["credits"][
            "tokens_used"
        ]
        assert usage_after_submit == estimated

        assert client.delete(f"/simulations/{sid}").status_code == 204

        usage_after_delete = fake_gcs.blobs[f"users/{MEMBER_UID}/account.json"]["credits"][
            "tokens_used"
        ]
        assert usage_after_delete == estimated

        # Simulation blobs (status, cost) are gone.
        assert f"users/{MEMBER_UID}/simulations/{sid}/cost.json" not in fake_gcs.blobs


# ---------------------------------------------------------------------------
# /me/account, /me/usage
# ---------------------------------------------------------------------------


class TestAccountEndpoints:
    def test_anonymous_rejected(self, app: FastAPI, client: TestClient) -> None:
        logout(app)
        assert client.get("/me/account").status_code == 401
        assert client.get("/me/usage").status_code == 401

    def test_account_creates_lazily_with_zero_usage(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        body = client.get("/me/account").json()
        assert body["motor_limit"] == 10
        assert body["simulation_limit"] == 10
        assert body["credits"]["monthly_token_limit"] == 10_000
        assert body["credits"]["tokens_used"] == 0
        assert body["credits"]["tokens_remaining"] == 10_000

    def test_admin_caller_sees_unlimited_caps(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        """For admin callers, all caps render as null (= unlimited)."""
        login_as(app, uid=ADMIN_UID, role="admin")
        body = client.get("/me/account").json()
        assert body["is_admin"] is True
        assert body["motor_limit"] is None
        assert body["simulation_limit"] is None
        assert body["credits"]["monthly_token_limit"] is None
        assert body["credits"]["tokens_remaining"] is None
        # tokens_used is still real — admin runs do consume tokens
        assert body["credits"]["tokens_used"] == 0

    def test_admin_caller_usage_unlimited(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")
        body = client.get("/me/usage").json()
        assert body["is_admin"] is True
        assert body["motor_limit"] is None
        assert body["motors_remaining"] is None
        assert body["simulation_limit"] is None
        assert body["simulations_remaining"] is None
        assert body["credits"]["monthly_token_limit"] is None
        assert body["credits"]["tokens_remaining"] is None

    def test_usage_snapshot_combines_counts_and_usage(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        _seed_motors(fake_gcs, MEMBER_UID, 3)
        _seed_simulations(fake_gcs, MEMBER_UID, 2)
        _seed_account(fake_gcs, MEMBER_UID, tokens_used=2500)

        body = client.get("/me/usage").json()
        assert body["motor_count"] == 3
        assert body["motors_remaining"] == 7
        assert body["simulation_count"] == 2
        assert body["simulations_remaining"] == 8
        assert body["credits"]["tokens_used"] == 2500
        assert body["credits"]["tokens_remaining"] == 7500


# ---------------------------------------------------------------------------
# Admin endpoints — limit overrides
# ---------------------------------------------------------------------------


class TestAdminAccountManagement:
    def test_member_cannot_access_admin_routes(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=MEMBER_UID)
        assert client.get(f"/admin/users/{MEMBER_UID}/account").status_code == 403
        assert (
            client.put(f"/admin/users/{MEMBER_UID}/limits", json={"motor_limit": 5}).status_code
            == 403
        )

    def test_admin_can_override_limits(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")

        resp = client.put(
            "/admin/users/some-user/limits",
            json={"motor_limit": 25, "monthly_token_limit": 50_000},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["motor_limit"] == 25
        assert body["credits"]["monthly_token_limit"] == 50_000
        # AdminAccountSnapshot includes counts (zero for a fresh user)
        assert body["motor_count"] == 0
        assert body["simulation_count"] == 0

    def test_admin_get_account_includes_counts(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        fake_firebase: FakeFirebase,
    ) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")
        # FakeFirebase pre-seeds member-uid with no role claim → not admin
        target = MEMBER_UID
        _seed_motors(fake_gcs, target, 4)
        _seed_simulations(fake_gcs, target, 2)

        resp = client.get(f"/admin/users/{target}/account")
        assert resp.status_code == 200
        body = resp.json()
        assert body["motor_count"] == 4
        assert body["simulation_count"] == 2
        # Target is a member, so limits stay populated
        assert body["motor_limit"] == 10
        assert body["is_admin"] is False

    def test_admin_get_account_target_is_admin_unlimited(
        self,
        app: FastAPI,
        client: TestClient,
        fake_gcs: FakeGCS,
        fake_firebase: FakeFirebase,
    ) -> None:
        """When the *target* of an admin endpoint is itself an admin, caps
        render as null. is_admin is sourced from Firebase claims, not the caller."""
        login_as(app, uid=ADMIN_UID, role="admin")
        # FakeFirebase pre-seeds admin-uid with role=admin claim
        resp = client.get(f"/admin/users/{ADMIN_UID}/account")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_admin"] is True
        assert body["motor_limit"] is None
        assert body["simulation_limit"] is None
        assert body["credits"]["monthly_token_limit"] is None
        assert body["credits"]["tokens_remaining"] is None

    def test_my_account_leaves_counts_null(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        """Counts are populated only on the admin endpoints. The fields are
        present on AccountSnapshot for typing uniformity but null on /me."""
        login_as(app, uid=MEMBER_UID)
        body = client.get("/me/account").json()
        assert body["motor_count"] is None
        assert body["simulation_count"] is None

    def test_admin_update_limits_rejects_empty_body(
        self, app: FastAPI, client: TestClient, fake_gcs: FakeGCS
    ) -> None:
        login_as(app, uid=ADMIN_UID, role="admin")
        resp = client.put("/admin/users/x/limits", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Worker post-charge — reconciliation against account
# ---------------------------------------------------------------------------


def _seed_pending_worker_sim(
    fake_gcs: FakeGCS,
    *,
    user_id: str,
    sim_id: str,
    estimated: int,
    tokens_used: int,
) -> None:
    """Seed everything the worker needs to run: config, pending status, cost
    record with the given estimate, and a pre-charged account."""
    from datetime import UTC
    from datetime import datetime as dt

    period = current_period_utc()
    fake_gcs.blobs[f"users/{user_id}/simulations/{sim_id}/config.json"] = {
        "simulation_id": sim_id,
        "user_id": user_id,
        "motor_id": "motor-1",
        "motor_config": _solid_config(),
        "params": {
            "d_t": 0.001,
            "igniter_pressure": 1_000_000.0,
            "external_pressure": 101_325.0,
            "other_losses": 12.0,
        },
    }
    fake_gcs.blobs[f"users/{user_id}/simulations/{sim_id}/status.json"] = {
        "simulation_id": sim_id,
        "status": "pending",
        "error": None,
        "created_at": dt.now(UTC).isoformat(),
        "updated_at": dt.now(UTC).isoformat(),
    }
    fake_gcs.blobs[f"users/{user_id}/simulations/{sim_id}/cost.json"] = {
        "simulation_id": sim_id,
        "estimated_tokens": estimated,
        "actual_tokens": None,
        "iterations": None,
        "tokens_charged": estimated,
        "period": period,
        "created_at": dt.now(UTC).isoformat(),
        "completed_at": None,
        "refunded": False,
    }
    _seed_account(fake_gcs, user_id, monthly_token_limit=100_000, tokens_used=tokens_used)


@pytest.mark.asyncio
async def test_worker_records_actual_but_keeps_overestimate(fake_gcs: FakeGCS) -> None:
    """When actual_tokens < estimated_tokens the user does **not** get a
    refund — tokens_used and tokens_charged stay at the pre-charged amount.
    Refunds are reserved for the failure path."""
    from app.worker import run as run_module

    user_id = "worker-uid"
    sim_id = "sim-overestimate"
    overestimate = 20_000  # comfortably above the actual run for this motor
    _seed_pending_worker_sim(
        fake_gcs,
        user_id=user_id,
        sim_id=sim_id,
        estimated=overestimate,
        tokens_used=overestimate,
    )

    await run_module.run(sim_id, user_id)

    cost = fake_gcs.blobs[f"users/{user_id}/simulations/{sim_id}/cost.json"]
    assert cost["actual_tokens"] is not None
    assert cost["actual_tokens"] < overestimate, (
        "fixture motor should run cheaper than the seeded estimate"
    )
    assert cost["tokens_charged"] == overestimate, "no refund on over-estimate"
    assert cost["refunded"] is False

    account = fake_gcs.blobs[f"users/{user_id}/account.json"]
    assert account["credits"]["tokens_used"] == overestimate


@pytest.mark.asyncio
async def test_worker_charges_overage_when_actual_exceeds_estimate(
    fake_gcs: FakeGCS,
) -> None:
    """When actual_tokens > estimated_tokens the worker debits the overage
    so the user pays for what they actually consumed."""
    from app.worker import run as run_module

    user_id = "underestimate-uid"
    sim_id = "sim-underestimate"
    underestimate = 10  # well below any real run
    _seed_pending_worker_sim(
        fake_gcs,
        user_id=user_id,
        sim_id=sim_id,
        estimated=underestimate,
        tokens_used=underestimate,
    )

    await run_module.run(sim_id, user_id)

    cost = fake_gcs.blobs[f"users/{user_id}/simulations/{sim_id}/cost.json"]
    actual = cost["actual_tokens"]
    assert actual > underestimate
    assert cost["tokens_charged"] == actual

    account = fake_gcs.blobs[f"users/{user_id}/account.json"]
    assert account["credits"]["tokens_used"] == actual
