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
