"""Team metadata, memberships, invites, and account repositories.

Each repo is dumb (no auth, no business rules) and just owns a path scheme,
matching the pattern in :mod:`app.repositories.motor` and
:mod:`app.repositories.account`. Team-scoped motors / simulations / costs
live in :mod:`app.repositories.team_resources`.

Membership is stored in two places — the canonical record under the team and
a reverse pointer under the user. The team-side blob is the source of truth;
the user-side pointer exists so listing a user's teams is a single prefix
scan rather than a whole-bucket walk. Saves and deletes write/clear both.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.config import get_settings
from app.repositories.base import GCSRepository
from app.schemas.credits import CreditAccount, current_period_utc
from app.schemas.team import (
    Team,
    TeamAccount,
    TeamInvite,
    TeamInviteIndex,
    TeamMembership,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _team_blob(team_id: str) -> str:
    return f"teams/{team_id}/team.json"


def _team_prefix(team_id: str) -> str:
    return f"teams/{team_id}/"


def _members_prefix(team_id: str) -> str:
    return f"teams/{team_id}/members/"


def _member_blob(team_id: str, user_id: str) -> str:
    return f"teams/{team_id}/members/{user_id}.json"


def _user_teams_prefix(user_id: str) -> str:
    return f"users/{user_id}/teams/"


def _user_team_pointer_blob(user_id: str, team_id: str) -> str:
    return f"users/{user_id}/teams/{team_id}.json"


def _invites_prefix(team_id: str) -> str:
    return f"teams/{team_id}/invites/"


def _invite_blob(team_id: str, token: str) -> str:
    return f"teams/{team_id}/invites/{token}.json"


def _invite_index_blob(token: str) -> str:
    return f"invites/{token}.json"


def _team_account_blob(team_id: str) -> str:
    return f"teams/{team_id}/account.json"


# Fields the admin update-limits endpoint may set on a TeamAccount.
_LIMIT_FIELDS = frozenset({"motor_limit", "simulation_limit", "monthly_token_limit"})


# ---------------------------------------------------------------------------
# Team metadata
# ---------------------------------------------------------------------------


class TeamRepository(GCSRepository):
    async def get(self, team_id: str) -> Team | None:
        data = await self._read(_team_blob(team_id))
        if data is None:
            return None
        return Team.model_validate(data)

    async def save(self, team: Team) -> None:
        await self._write(_team_blob(team.team_id), team.model_dump(mode="json"))

    async def delete(self, team_id: str) -> None:
        """Cascade-delete the entire team prefix.

        Reverse pointers under ``users/*/teams/{team_id}.json`` are NOT touched
        here — the caller (router) clears those after listing members, so this
        repo stays focused on the team prefix.
        """
        await self._delete(_team_prefix(team_id))

    async def list_all(self) -> list[Team]:
        """Admin-only — full bucket scan for the team root."""
        all_blobs = await self._list("teams/")
        teams: list[Team] = []
        seen: set[str] = set()
        for blob_name in all_blobs:
            # teams/{team_id}/team.json
            parts = blob_name.split("/")
            if len(parts) != 3 or parts[2] != "team.json":
                continue
            team_id = parts[1]
            if team_id in seen:
                continue
            seen.add(team_id)
            data = await self._read(blob_name)
            if data is None:
                continue
            try:
                teams.append(Team.model_validate(data))
            except Exception:
                logger.warning("Skipping malformed team record: %s", blob_name, exc_info=True)
        return teams


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


class TeamMembershipRepository(GCSRepository):
    async def get(self, team_id: str, user_id: str) -> TeamMembership | None:
        data = await self._read(_member_blob(team_id, user_id))
        if data is None:
            return None
        return TeamMembership.model_validate(data)

    async def list_for_team(self, team_id: str) -> list[TeamMembership]:
        return await self._read_memberships(_members_prefix(team_id))

    async def list_for_user(self, user_id: str) -> list[TeamMembership]:
        return await self._read_memberships(_user_teams_prefix(user_id))

    async def _read_memberships(self, prefix: str) -> list[TeamMembership]:
        blob_names = await self._list(prefix)
        records: list[TeamMembership] = []
        for blob_name in blob_names:
            if not blob_name.endswith(".json"):
                continue
            data = await self._read(blob_name)
            if data is None:
                continue
            try:
                records.append(TeamMembership.model_validate(data))
            except Exception:
                logger.warning("Skipping malformed membership: %s", blob_name, exc_info=True)
        return records

    async def save(self, membership: TeamMembership) -> None:
        """Write both the canonical team-scoped record and the user-side pointer."""
        payload = membership.model_dump(mode="json")
        await self._write(_member_blob(membership.team_id, membership.user_id), payload)
        await self._write(_user_team_pointer_blob(membership.user_id, membership.team_id), payload)

    async def delete(self, team_id: str, user_id: str) -> None:
        await self._delete(_member_blob(team_id, user_id))
        await self._delete(_user_team_pointer_blob(user_id, team_id))

    async def count_owners(self, team_id: str) -> int:
        members = await self.list_for_team(team_id)
        return sum(1 for m in members if m.role == "owner")

    async def count_for_user(self, user_id: str) -> int:
        return len(await self.list_for_user(user_id))


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class TeamInviteRepository(GCSRepository):
    async def get(self, team_id: str, token: str) -> TeamInvite | None:
        data = await self._read(_invite_blob(team_id, token))
        if data is None:
            return None
        return TeamInvite.model_validate(data)

    async def get_by_token(self, token: str) -> TeamInvite | None:
        """O(1) lookup via the ``invites/{token}.json`` index."""
        index_data = await self._read(_invite_index_blob(token))
        if index_data is None:
            return None
        try:
            index = TeamInviteIndex.model_validate(index_data)
        except Exception:
            logger.warning("Malformed invite index for token=%s", token, exc_info=True)
            return None
        return await self.get(index.team_id, token)

    async def list_for_team(self, team_id: str) -> list[TeamInvite]:
        blob_names = await self._list(_invites_prefix(team_id))
        invites: list[TeamInvite] = []
        for blob_name in blob_names:
            if not blob_name.endswith(".json"):
                continue
            data = await self._read(blob_name)
            if data is None:
                continue
            try:
                invites.append(TeamInvite.model_validate(data))
            except Exception:
                logger.warning("Skipping malformed invite: %s", blob_name, exc_info=True)
        return invites

    async def save(self, invite: TeamInvite) -> None:
        """Write the team-scoped record and the token-keyed index."""
        await self._write(
            _invite_blob(invite.team_id, invite.token),
            invite.model_dump(mode="json"),
        )
        await self._write(
            _invite_index_blob(invite.token),
            TeamInviteIndex(team_id=invite.team_id).model_dump(mode="json"),
        )

    async def delete(self, team_id: str, token: str) -> None:
        await self._delete(_invite_blob(team_id, token))
        await self._delete(_invite_index_blob(token))


# ---------------------------------------------------------------------------
# Team account / credits
# ---------------------------------------------------------------------------


class TeamInsufficientBalanceError(Exception):
    """Raised when a team debit would push usage past the monthly limit."""

    def __init__(self, team_id: str, requested: int, remaining: int) -> None:
        super().__init__(
            f"Team {team_id} requested {requested} tokens but only {remaining} remaining."
        )
        self.team_id = team_id
        self.requested = requested
        self.remaining = remaining


class TeamAccountRepository(GCSRepository):
    """Mirrors :class:`app.repositories.account.AccountRepository`.

    Same non-transactional read-modify-write tradeoff applies — acceptable
    while the credit system is internal accounting; revisit if real money
    flows through this layer.
    """

    def _defaults(self, team_id: str) -> TeamAccount:
        settings = get_settings()
        return TeamAccount(
            team_id=team_id,
            motor_limit=settings.default_team_motor_limit,
            simulation_limit=settings.default_team_simulation_limit,
            credits=CreditAccount(
                monthly_token_limit=settings.default_team_monthly_token_limit,
                tokens_used=0,
                usage_period=current_period_utc(),
            ),
        )

    async def get_or_create(self, team_id: str) -> TeamAccount:
        data = await self._read(_team_account_blob(team_id))
        if data is None:
            account = self._defaults(team_id)
            await self.save(account)
            return account

        account = TeamAccount.model_validate(data)
        if account.credits.is_period_stale():
            current = current_period_utc()
            logger.info(
                "Resetting usage for team %s: %s -> %s",
                team_id,
                account.credits.usage_period,
                current,
            )
            account.credits.tokens_used = 0
            account.credits.usage_period = current
            account.updated_at = datetime.now(UTC)
            await self.save(account)
        return account

    async def save(self, account: TeamAccount) -> None:
        account.updated_at = datetime.now(UTC)
        data = account.model_dump(
            mode="json",
            exclude={"credits": {"tokens_remaining"}},
        )
        await self._write(_team_account_blob(account.team_id), data)

    async def debit(self, team_id: str, tokens: int) -> int:
        if tokens < 0:
            raise ValueError("Cannot debit a negative amount; use credit() to refund.")
        account = await self.get_or_create(team_id)
        if not account.credits.can_afford(tokens):
            remaining = account.credits.tokens_remaining or 0
            raise TeamInsufficientBalanceError(team_id, tokens, remaining)

        account.credits.tokens_used += tokens
        await self.save(account)
        return tokens

    async def credit(self, team_id: str, tokens: int) -> TeamAccount:
        if tokens < 0:
            raise ValueError("Credit amount must be non-negative.")
        account = await self.get_or_create(team_id)
        if tokens:
            account.credits.tokens_used = max(0, account.credits.tokens_used - tokens)
        await self.save(account)
        return account

    async def update_limits(self, team_id: str, updates: dict[str, int | None]) -> TeamAccount:
        bad = set(updates) - _LIMIT_FIELDS
        if bad:
            raise ValueError(f"Cannot update fields {sorted(bad)!r}")
        account = await self.get_or_create(team_id)
        for key, value in updates.items():
            if key == "monthly_token_limit":
                account.credits.monthly_token_limit = value
            else:
                setattr(account, key, value)
        await self.save(account)
        return account
