"""Tests for ``MotorRepository``."""

from __future__ import annotations

import pytest

from app.repositories.motor import MotorRepository
from app.schemas.motor import (
    SOLID_FORMULATIONS,
    BatesSegmentSchema,
    CombustionChamberSchema,
    GrainSchema,
    MotorRecord,
    NozzleSchema,
    SolidMotorConfigSchema,
    SolidMotorThrustChamberSchema,
)
from tests.conftest import FakeGCS

USER_ID = "user-1"


def _motor_record(motor_id: str, name: str = "Olympus") -> MotorRecord:
    return MotorRecord(
        motor_id=motor_id,
        name=name,
        config=SolidMotorConfigSchema(
            propellant_id=next(iter(SOLID_FORMULATIONS)),
            grain=GrainSchema(
                segments=[
                    BatesSegmentSchema(outer_diameter=0.069, core_diameter=0.025, length=0.12)
                ]
            ),
            thrust_chamber=SolidMotorThrustChamberSchema(
                nozzle=NozzleSchema(
                    inlet_diameter=0.060,
                    throat_diameter=0.015,
                    divergent_angle=12,
                    convergent_angle=45,
                    expansion_ratio=8,
                ),
                combustion_chamber=CombustionChamberSchema(
                    casing_inner_diameter=0.0702,
                    casing_outer_diameter=0.0762,
                    internal_length=0.280,
                ),
                dry_mass=1.5,
                nozzle_exit_to_grain_port_distance=0.010,
            ),
        ),
    )


class TestMotorRepositoryRoundTrip:
    @pytest.mark.asyncio
    async def test_save_then_get(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        record = _motor_record("m1")
        await repo.save(USER_ID, "m1", record)

        # The blob is stored at the documented path.
        assert "users/user-1/motors/m1.json" in fake_gcs.blobs

        loaded = await repo.get(USER_ID, "m1")
        assert loaded is not None
        assert loaded.motor_id == "m1"
        assert loaded.config.motor_type == "solid"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        assert await repo.get(USER_ID, "nope") is None

    @pytest.mark.asyncio
    async def test_list_skips_malformed_records(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        await repo.save(USER_ID, "m1", _motor_record("m1"))
        # Garbage blob that lives under the same prefix — must not crash list().
        fake_gcs.blobs["users/user-1/motors/garbage.json"] = {"not": "a motor"}

        records = await repo.list(USER_ID)
        assert [r.motor_id for r in records] == ["m1"]

    @pytest.mark.asyncio
    async def test_list_scoped_per_user(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        await repo.save("user-1", "m1", _motor_record("m1"))
        await repo.save("user-2", "m2", _motor_record("m2"))

        assert {r.motor_id for r in await repo.list("user-1")} == {"m1"}
        assert {r.motor_id for r in await repo.list("user-2")} == {"m2"}

    @pytest.mark.asyncio
    async def test_delete_removes_blob(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        await repo.save(USER_ID, "m1", _motor_record("m1"))
        await repo.delete(USER_ID, "m1")
        assert await repo.get(USER_ID, "m1") is None

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self, fake_gcs: FakeGCS) -> None:
        """Deleting a motor that doesn't exist must not raise — the router's
        404 logic depends on ``get`` for existence, not on ``delete`` failing."""
        repo = MotorRepository()
        await repo.delete(USER_ID, "never-existed")


class TestListAllUsersWithMotors:
    @pytest.mark.asyncio
    async def test_returns_distinct_user_ids(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        await repo.save("user-1", "m1", _motor_record("m1"))
        await repo.save("user-1", "m2", _motor_record("m2"))
        await repo.save("user-2", "m3", _motor_record("m3"))

        assert await repo.list_all_users_with_motors() == ["user-1", "user-2"]

    @pytest.mark.asyncio
    async def test_empty_bucket_returns_empty(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        assert await repo.list_all_users_with_motors() == []

    @pytest.mark.asyncio
    async def test_ignores_non_motor_paths(self, fake_gcs: FakeGCS) -> None:
        """Sibling subtrees (``simulations/``, ``profile.json``) live under the
        same ``users/{uid}/`` umbrella but must not surface here."""
        repo = MotorRepository()
        await repo.save("user-1", "m1", _motor_record("m1"))
        fake_gcs.blobs["users/user-2/simulations/sim-a/status.json"] = {"v": 1}
        fake_gcs.blobs["users/user-3/profile.json"] = {"v": 1}

        assert await repo.list_all_users_with_motors() == ["user-1"]


class TestDeleteAllForUser:
    @pytest.mark.asyncio
    async def test_removes_every_motor_for_user_and_returns_count(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        await repo.save("user-1", "m1", _motor_record("m1"))
        await repo.save("user-1", "m2", _motor_record("m2"))

        deleted = await repo.delete_all_for_user("user-1")

        assert deleted == 2
        assert await repo.list("user-1") == []

    @pytest.mark.asyncio
    async def test_does_not_touch_other_users(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        await repo.save("user-1", "m1", _motor_record("m1"))
        await repo.save("user-2", "m2", _motor_record("m2"))

        await repo.delete_all_for_user("user-1")

        assert {r.motor_id for r in await repo.list("user-2")} == {"m2"}

    @pytest.mark.asyncio
    async def test_does_not_touch_sibling_subtrees(self, fake_gcs: FakeGCS) -> None:
        """Only the ``motors/`` subtree should be wiped — adjacent simulation
        and profile blobs under the same user prefix must survive."""
        repo = MotorRepository()
        await repo.save("user-1", "m1", _motor_record("m1"))
        fake_gcs.blobs["users/user-1/simulations/sim-a/status.json"] = {"v": 1}
        fake_gcs.blobs["users/user-1/profile.json"] = {"v": 1}

        await repo.delete_all_for_user("user-1")

        assert "users/user-1/simulations/sim-a/status.json" in fake_gcs.blobs
        assert "users/user-1/profile.json" in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_user_with_no_motors_returns_zero(self, fake_gcs: FakeGCS) -> None:
        repo = MotorRepository()
        assert await repo.delete_all_for_user("never-existed") == 0
