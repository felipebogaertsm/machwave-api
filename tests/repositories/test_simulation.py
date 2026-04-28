"""Tests for ``SimulationRepository`` — the GCS layout / blob naming logic
plus the path parsers behind ``list_summaries`` and ``list_all_simulation_pairs``.
"""

from __future__ import annotations

import pytest

from app.repositories.simulation import SimulationRepository
from app.schemas.motor import (
    SOLID_FORMULATIONS,
    BatesSegmentSchema,
    CombustionChamberSchema,
    GrainSchema,
    NozzleSchema,
    SolidMotorConfigSchema,
    SolidMotorThrustChamberSchema,
)
from app.schemas.simulation import (
    IBSimParamsSchema,
    SimulationJobConfig,
    SimulationStatusEvent,
    SimulationStatusRecord,
)
from tests.conftest import FakeGCS


def _job_config(user_id: str, simulation_id: str, motor_id: str = "m1") -> SimulationJobConfig:
    return SimulationJobConfig(
        simulation_id=simulation_id,
        user_id=user_id,
        motor_id=motor_id,
        motor_config=SolidMotorConfigSchema(
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
        params=IBSimParamsSchema(),
    )


class TestSimulationCRUD:
    @pytest.mark.asyncio
    async def test_save_get_status_round_trip(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        record = SimulationStatusRecord(
            simulation_id="sim-1",
            events=[SimulationStatusEvent(status="running")],
        )
        await repo.save_status("u1", "sim-1", record)
        assert "users/u1/simulations/sim-1/status.json" in fake_gcs.blobs

        loaded = await repo.get_status("u1", "sim-1")
        assert loaded is not None
        assert loaded.status == "running"

    @pytest.mark.asyncio
    async def test_save_get_config_round_trip(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("u1", "sim-1"))
        loaded = await repo.get_config("u1", "sim-1")
        assert loaded is not None
        assert loaded.motor_config.motor_type == "solid"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        assert await repo.get_status("u1", "missing") is None
        assert await repo.get_config("u1", "missing") is None
        assert await repo.get_results("u1", "missing") is None

    @pytest.mark.asyncio
    async def test_delete_removes_only_target_simulation(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("u1", "sim-1"))
        await repo.save_status("u1", "sim-1", SimulationStatusRecord(simulation_id="sim-1"))
        await repo.save_config("u1", "sim-2", _job_config("u1", "sim-2"))

        await repo.delete("u1", "sim-1")

        assert await repo.get_status("u1", "sim-1") is None
        assert await repo.get_config("u1", "sim-1") is None
        # sim-2 untouched.
        assert await repo.get_config("u1", "sim-2") is not None


class TestListSummaries:
    @pytest.mark.asyncio
    async def test_only_returns_simulations_with_both_status_and_config(
        self, fake_gcs: FakeGCS
    ) -> None:
        repo = SimulationRepository()

        # Complete simulation
        await repo.save_config("u1", "sim-complete", _job_config("u1", "sim-complete"))
        await repo.save_status(
            "u1", "sim-complete", SimulationStatusRecord(simulation_id="sim-complete")
        )

        # Status without config — must be skipped.
        await repo.save_status(
            "u1", "sim-orphan", SimulationStatusRecord(simulation_id="sim-orphan")
        )

        summaries = await repo.list_summaries("u1")
        assert {s.simulation_id for s in summaries} == {"sim-complete"}

    @pytest.mark.asyncio
    async def test_skips_malformed_status(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("u1", "sim-1"))
        # Junk in the status blob — must not crash list_summaries.
        fake_gcs.blobs["users/u1/simulations/sim-1/status.json"] = {"junk": True}

        summaries = await repo.list_summaries("u1")
        assert summaries == []

    @pytest.mark.asyncio
    async def test_sorted_by_updated_at_descending(self, fake_gcs: FakeGCS) -> None:
        from datetime import UTC, datetime, timedelta

        repo = SimulationRepository()
        now = datetime.now(UTC)

        for i, ts in enumerate([now - timedelta(hours=2), now, now - timedelta(hours=1)]):
            sim_id = f"sim-{i}"
            await repo.save_config("u1", sim_id, _job_config("u1", sim_id))
            await repo.save_status(
                "u1",
                sim_id,
                SimulationStatusRecord(
                    simulation_id=sim_id,
                    events=[SimulationStatusEvent(status="pending", timestamp=ts)],
                ),
            )

        summaries = await repo.list_summaries("u1")
        assert [s.simulation_id for s in summaries] == ["sim-1", "sim-2", "sim-0"]

    @pytest.mark.asyncio
    async def test_scoped_per_user(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-1", _job_config("u1", "sim-1"))
        await repo.save_status("u1", "sim-1", SimulationStatusRecord(simulation_id="sim-1"))
        await repo.save_config("u2", "sim-2", _job_config("u2", "sim-2"))
        await repo.save_status("u2", "sim-2", SimulationStatusRecord(simulation_id="sim-2"))

        assert {s.simulation_id for s in await repo.list_summaries("u1")} == {"sim-1"}
        assert {s.simulation_id for s in await repo.list_summaries("u2")} == {"sim-2"}


class TestListAllSimulationPairs:
    @pytest.mark.asyncio
    async def test_extracts_unique_user_simulation_pairs(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-a", _job_config("u1", "sim-a"))
        await repo.save_status("u1", "sim-a", SimulationStatusRecord(simulation_id="sim-a"))
        await repo.save_config("u2", "sim-b", _job_config("u2", "sim-b"))

        pairs = await repo.list_all_simulation_pairs()
        # Each (user, simulation) pair appears exactly once even though the
        # simulation has multiple files (config + status).
        assert sorted(pairs) == [("u1", "sim-a"), ("u2", "sim-b")]

    @pytest.mark.asyncio
    async def test_ignores_non_simulation_paths(self, fake_gcs: FakeGCS) -> None:
        """Motor blobs sit under the same ``users/`` umbrella but a different
        second path component — they must not surface as simulation pairs."""
        repo = SimulationRepository()
        fake_gcs.blobs["users/u1/motors/m1.json"] = {"motor_id": "m1"}
        fake_gcs.blobs["users/u1/profile.json"] = {"any": "thing"}
        await repo.save_config("u1", "sim-a", _job_config("u1", "sim-a"))

        pairs = await repo.list_all_simulation_pairs()
        assert pairs == [("u1", "sim-a")]

    @pytest.mark.asyncio
    async def test_empty_bucket_returns_empty(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        assert await repo.list_all_simulation_pairs() == []


class TestListAllUsersWithSimulations:
    @pytest.mark.asyncio
    async def test_returns_distinct_user_ids(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-a", _job_config("u1", "sim-a"))
        await repo.save_config("u1", "sim-b", _job_config("u1", "sim-b"))
        await repo.save_config("u2", "sim-c", _job_config("u2", "sim-c"))

        assert await repo.list_all_users_with_simulations() == ["u1", "u2"]

    @pytest.mark.asyncio
    async def test_empty_bucket_returns_empty(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        assert await repo.list_all_users_with_simulations() == []

    @pytest.mark.asyncio
    async def test_ignores_users_with_only_motors(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        fake_gcs.blobs["users/u-motors-only/motors/m1.json"] = {"v": 1}
        await repo.save_config("u-with-sim", "sim-a", _job_config("u-with-sim", "sim-a"))

        assert await repo.list_all_users_with_simulations() == ["u-with-sim"]


class TestDeleteAllForUser:
    @pytest.mark.asyncio
    async def test_removes_every_simulation_for_user_and_returns_count(
        self, fake_gcs: FakeGCS
    ) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-a", _job_config("u1", "sim-a"))
        await repo.save_status("u1", "sim-a", SimulationStatusRecord(simulation_id="sim-a"))
        await repo.save_config("u1", "sim-b", _job_config("u1", "sim-b"))

        deleted = await repo.delete_all_for_user("u1")

        assert deleted == 2
        # Nothing left under the simulations subtree.
        assert not [k for k in fake_gcs.blobs if k.startswith("users/u1/simulations/")]

    @pytest.mark.asyncio
    async def test_does_not_touch_other_users(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-a", _job_config("u1", "sim-a"))
        await repo.save_config("u2", "sim-b", _job_config("u2", "sim-b"))
        await repo.save_status("u2", "sim-b", SimulationStatusRecord(simulation_id="sim-b"))

        await repo.delete_all_for_user("u1")

        assert await repo.get_config("u2", "sim-b") is not None

    @pytest.mark.asyncio
    async def test_does_not_touch_sibling_subtrees(self, fake_gcs: FakeGCS) -> None:
        """Only the ``simulations/`` subtree should be wiped — motors and
        profile blobs under the same user prefix must survive."""
        repo = SimulationRepository()
        await repo.save_config("u1", "sim-a", _job_config("u1", "sim-a"))
        fake_gcs.blobs["users/u1/motors/m1.json"] = {"v": 1}
        fake_gcs.blobs["users/u1/profile.json"] = {"v": 1}

        await repo.delete_all_for_user("u1")

        assert "users/u1/motors/m1.json" in fake_gcs.blobs
        assert "users/u1/profile.json" in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_user_with_no_simulations_returns_zero(self, fake_gcs: FakeGCS) -> None:
        repo = SimulationRepository()
        assert await repo.delete_all_for_user("never-existed") == 0
