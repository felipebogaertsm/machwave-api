"""Tests for the team repos in ``app.repositories.team``.

The bidirectional membership index is the most important invariant — saves
must write both blobs and deletes must remove both. Beyond that we cover the
invite-by-token index, owner counting, the team account debit/refund cycle,
and the membership-count-for-user query that backs the per-user team cap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.repositories.team import (
    TeamAccountRepository,
    TeamInsufficientBalanceError,
    TeamInviteRepository,
    TeamMembershipRepository,
    TeamRepository,
)
from app.schemas.team import Team, TeamInvite, TeamMembership
from tests.conftest import FakeGCS  # noqa: F401  (used in fixtures)


class TestTeamRepository:
    @pytest.mark.asyncio
    async def test_save_get_roundtrip(self, fake_gcs: FakeGCS) -> None:
        repo = TeamRepository()
        team = Team(team_id="t1", name="Foo", created_by="u1")
        await repo.save(team)
        loaded = await repo.get("t1")
        assert loaded is not None
        assert loaded.team_id == "t1"
        assert loaded.name == "Foo"

    @pytest.mark.asyncio
    async def test_delete_cascades_team_prefix(self, fake_gcs: FakeGCS) -> None:
        repo = TeamRepository()
        await repo.save(Team(team_id="t1", name="Foo", created_by="u1"))
        # Plant arbitrary sub-blobs to confirm the prefix cascade.
        await fake_gcs.write_json("teams/t1/motors/m1.json", {"x": 1})
        await fake_gcs.write_json("teams/t1/simulations/s1/config.json", {"y": 2})

        await repo.delete("t1")

        assert "teams/t1/team.json" not in fake_gcs.blobs
        assert "teams/t1/motors/m1.json" not in fake_gcs.blobs
        assert "teams/t1/simulations/s1/config.json" not in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_list_all_returns_every_team(self, fake_gcs: FakeGCS) -> None:
        repo = TeamRepository()
        await repo.save(Team(team_id="t1", name="A", created_by="u1"))
        await repo.save(Team(team_id="t2", name="B", created_by="u2"))
        teams = await repo.list_all()
        assert {t.team_id for t in teams} == {"t1", "t2"}


class TestTeamMembershipRepository:
    @pytest.mark.asyncio
    async def test_save_writes_both_blobs(self, fake_gcs: FakeGCS) -> None:
        repo = TeamMembershipRepository()
        await repo.save(TeamMembership(team_id="t1", user_id="u1", role="editor"))

        assert "teams/t1/members/u1.json" in fake_gcs.blobs
        assert "users/u1/teams/t1.json" in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_delete_removes_both_blobs(self, fake_gcs: FakeGCS) -> None:
        repo = TeamMembershipRepository()
        await repo.save(TeamMembership(team_id="t1", user_id="u1", role="editor"))
        await repo.delete("t1", "u1")

        assert "teams/t1/members/u1.json" not in fake_gcs.blobs
        assert "users/u1/teams/t1.json" not in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_list_for_team_returns_all_members(self, fake_gcs: FakeGCS) -> None:
        repo = TeamMembershipRepository()
        await repo.save(TeamMembership(team_id="t1", user_id="u1", role="owner"))
        await repo.save(TeamMembership(team_id="t1", user_id="u2", role="editor"))
        await repo.save(TeamMembership(team_id="t2", user_id="u1", role="viewer"))

        members = await repo.list_for_team("t1")
        assert {m.user_id for m in members} == {"u1", "u2"}

    @pytest.mark.asyncio
    async def test_list_for_user_uses_reverse_index(self, fake_gcs: FakeGCS) -> None:
        repo = TeamMembershipRepository()
        await repo.save(TeamMembership(team_id="t1", user_id="u1", role="owner"))
        await repo.save(TeamMembership(team_id="t2", user_id="u1", role="viewer"))
        await repo.save(TeamMembership(team_id="t1", user_id="u2", role="editor"))

        teams = await repo.list_for_user("u1")
        assert {m.team_id for m in teams} == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_count_owners(self, fake_gcs: FakeGCS) -> None:
        repo = TeamMembershipRepository()
        await repo.save(TeamMembership(team_id="t1", user_id="u1", role="owner"))
        await repo.save(TeamMembership(team_id="t1", user_id="u2", role="owner"))
        await repo.save(TeamMembership(team_id="t1", user_id="u3", role="editor"))

        assert await repo.count_owners("t1") == 2

    @pytest.mark.asyncio
    async def test_count_for_user(self, fake_gcs: FakeGCS) -> None:
        repo = TeamMembershipRepository()
        await repo.save(TeamMembership(team_id="t1", user_id="u1", role="owner"))
        await repo.save(TeamMembership(team_id="t2", user_id="u1", role="editor"))

        assert await repo.count_for_user("u1") == 2
        assert await repo.count_for_user("u2") == 0


class TestTeamInviteRepository:
    @pytest.mark.asyncio
    async def test_save_writes_both_blob_and_index(self, fake_gcs: FakeGCS) -> None:
        repo = TeamInviteRepository()
        await repo.save(TeamInvite(token="tok123", team_id="t1", role="editor", created_by="u1"))  # noqa: S106

        assert "teams/t1/invites/tok123.json" in fake_gcs.blobs
        assert "invites/tok123.json" in fake_gcs.blobs
        assert fake_gcs.blobs["invites/tok123.json"] == {"team_id": "t1"}

    @pytest.mark.asyncio
    async def test_get_by_token_resolves_through_index(self, fake_gcs: FakeGCS) -> None:
        repo = TeamInviteRepository()
        await repo.save(TeamInvite(token="tok123", team_id="t1", role="editor", created_by="u1"))  # noqa: S106

        invite = await repo.get_by_token("tok123")
        assert invite is not None
        assert invite.team_id == "t1"

    @pytest.mark.asyncio
    async def test_delete_removes_both(self, fake_gcs: FakeGCS) -> None:
        repo = TeamInviteRepository()
        await repo.save(TeamInvite(token="tok123", team_id="t1", role="editor", created_by="u1"))  # noqa: S106
        await repo.delete("t1", "tok123")

        assert "teams/t1/invites/tok123.json" not in fake_gcs.blobs
        assert "invites/tok123.json" not in fake_gcs.blobs

    @pytest.mark.asyncio
    async def test_get_by_token_returns_none_for_unknown(self, fake_gcs: FakeGCS) -> None:
        repo = TeamInviteRepository()
        assert await repo.get_by_token("missing") is None


class TestTeamAccountRepository:
    @pytest.mark.asyncio
    async def test_get_or_create_seeds_with_defaults(self, fake_gcs: FakeGCS) -> None:
        repo = TeamAccountRepository()
        account = await repo.get_or_create("t1")
        # Defaults match the values in app.config.Settings.
        assert account.motor_limit == 50
        assert account.simulation_limit == 50
        assert account.credits.monthly_token_limit == 100_000
        assert account.credits.tokens_used == 0

    @pytest.mark.asyncio
    async def test_debit_increments_usage(self, fake_gcs: FakeGCS) -> None:
        repo = TeamAccountRepository()
        await repo.get_or_create("t1")
        charged = await repo.debit("t1", 100)
        assert charged == 100

        account = await repo.get_or_create("t1")
        assert account.credits.tokens_used == 100

    @pytest.mark.asyncio
    async def test_debit_rejects_overdraft(self, fake_gcs: FakeGCS) -> None:
        repo = TeamAccountRepository()
        await repo.get_or_create("t1")
        await repo.update_limits("t1", {"monthly_token_limit": 50})

        with pytest.raises(TeamInsufficientBalanceError) as exc:
            await repo.debit("t1", 100)
        assert exc.value.remaining == 50

    @pytest.mark.asyncio
    async def test_credit_refunds_floored_at_zero(self, fake_gcs: FakeGCS) -> None:
        repo = TeamAccountRepository()
        await repo.get_or_create("t1")
        await repo.debit("t1", 200)

        await repo.credit("t1", 1_000)  # over-refund must not go negative
        account = await repo.get_or_create("t1")
        assert account.credits.tokens_used == 0

    @pytest.mark.asyncio
    async def test_period_resets_usage(self, fake_gcs: FakeGCS) -> None:
        repo = TeamAccountRepository()
        await repo.get_or_create("t1")
        await repo.debit("t1", 200)

        # Hand-edit the stored record to simulate a stale period.
        stale_period = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m")
        fake_gcs.blobs["teams/t1/account.json"]["credits"]["usage_period"] = stale_period

        # First read after the rollover must zero the counter.
        account = await repo.get_or_create("t1")
        assert account.credits.tokens_used == 0
