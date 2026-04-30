"""Tests for ``app.schemas.team`` — invite validators, expiry semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.team import (
    CreateInviteRequest,
    TeamInvite,
    TeamRole,
    role_rank,
)


class TestRoleRank:
    @pytest.mark.parametrize(
        "lower,higher",
        [("viewer", "editor"), ("editor", "owner"), ("viewer", "owner")],
    )
    def test_rank_is_strict_total_order(self, lower: TeamRole, higher: TeamRole) -> None:
        assert role_rank(lower) < role_rank(higher)


class TestInviteRoleValidator:
    def test_invite_rejects_owner_role(self) -> None:
        with pytest.raises(ValidationError):
            TeamInvite(
                token="tok",  # noqa: S106
                team_id="t1",
                role="owner",  # type: ignore[arg-type]
                created_by="u1",
            )

    def test_create_invite_request_rejects_owner_role(self) -> None:
        with pytest.raises(ValidationError):
            CreateInviteRequest(role="owner")  # type: ignore[arg-type]

    @pytest.mark.parametrize("role", ["editor", "viewer"])
    def test_invite_accepts_non_owner_roles(self, role: str) -> None:
        invite = TeamInvite(
            token="tok",  # noqa: S106
            team_id="t1",
            role=role,  # type: ignore[arg-type]
            created_by="u1",
        )
        assert invite.role == role


class TestInviteState:
    def _make(self, **kwargs: object) -> TeamInvite:
        return TeamInvite(
            token="tok",  # noqa: S106
            team_id="t1",
            role="editor",
            created_by="u1",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_fresh_invite_is_usable(self) -> None:
        invite = self._make()
        assert invite.is_usable

    def test_expired_invite_not_usable(self) -> None:
        invite = self._make(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        assert invite.is_expired
        assert not invite.is_usable

    def test_revoked_invite_not_usable(self) -> None:
        invite = self._make(revoked=True)
        assert not invite.is_usable

    def test_consumed_invite_not_usable(self) -> None:
        invite = self._make(accepted_by="u2", accepted_at=datetime.now(UTC))
        assert invite.is_consumed
        assert not invite.is_usable
