"""Per-user account / billing record.

Loaded with ``get_or_create`` — first access for a user materialises the
account from config defaults. Subsequent reads reset ``credits.tokens_used``
to 0 when the current calendar month rolls past ``credits.usage_period``.

Read-modify-write here is **not transactional**. Two parallel debits can both
read the same ``tokens_used``, both pass the affordability check, and both
write the incremented value — letting through 2 * cost of work for ~1 * cost
recorded. With per-user keys and a small free tier this is bounded and
acceptable; if real money is ever charged through this layer it must be
replaced (Firestore transactions, Postgres row locks, or an authoritative
usage service).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.config import get_settings
from app.repositories.base import GCSRepository
from app.schemas.credits import CreditAccount, UserAccount, current_period_utc

logger = logging.getLogger(__name__)


def _account_blob(user_id: str) -> str:
    return f"users/{user_id}/account.json"


# Fields the admin UpdateLimitsRequest may set. ``monthly_token_limit`` is
# routed into the nested ``credits`` object; the rest live at the top level.
_LIMIT_FIELDS = frozenset({"motor_limit", "simulation_limit", "monthly_token_limit"})


class InsufficientBalanceError(Exception):
    """Raised when a debit would push usage past the monthly limit."""

    def __init__(self, user_id: str, requested: int, remaining: int) -> None:
        super().__init__(
            f"User {user_id} requested {requested} tokens but only {remaining} remaining."
        )
        self.user_id = user_id
        self.requested = requested
        self.remaining = remaining


class AccountRepository(GCSRepository):
    def _defaults(self, user_id: str, *, role: str = "member") -> UserAccount:
        """Build the seed account. Admins get ``None`` for every cap (unlimited);
        members get the config defaults."""
        settings = get_settings()
        is_admin = role == "admin"
        return UserAccount(
            user_id=user_id,
            motor_limit=None if is_admin else settings.default_motor_limit,
            simulation_limit=None if is_admin else settings.default_simulation_limit,
            credits=CreditAccount.fresh(
                role=role,
                default_limit=settings.default_monthly_token_limit,
            ),
        )

    async def get_or_create(self, user_id: str, *, role: str = "member") -> UserAccount:
        data = await self._read(_account_blob(user_id))
        if data is None:
            account = self._defaults(user_id, role=role)
            await self.save(account)
            return account

        account = UserAccount.model_validate(data)

        if account.credits.is_period_stale():
            current = current_period_utc()
            logger.info(
                "Resetting usage for user %s: %s -> %s",
                user_id,
                account.credits.usage_period,
                current,
            )
            account.credits.tokens_used = 0
            account.credits.usage_period = current
            account.updated_at = datetime.now(UTC)
            await self.save(account)
        return account

    async def save(self, account: UserAccount) -> None:
        account.updated_at = datetime.now(UTC)
        # ``tokens_remaining`` is a computed field; exclude it from storage so
        # the on-disk record only carries the raw fields and can never drift
        # from them on read.
        data = account.model_dump(
            mode="json",
            exclude={"credits": {"tokens_remaining"}},
        )
        await self._write(_account_blob(account.user_id), data)

    async def debit(self, user_id: str, tokens: int) -> int:
        """Add ``tokens`` to the usage counter. Returns tokens added.

        Unlimited accounts always succeed and still increment ``tokens_used``
        so admin telemetry stays accurate. A finite limit raises
        :class:`InsufficientBalanceError` when it would be breached.
        """
        if tokens < 0:
            raise ValueError("Cannot debit a negative amount; use credit() to refund.")
        account = await self.get_or_create(user_id)
        if not account.credits.can_afford(tokens):
            remaining = account.credits.tokens_remaining or 0
            raise InsufficientBalanceError(user_id, tokens, remaining)

        account.credits.tokens_used += tokens
        await self.save(account)
        return tokens

    async def credit(self, user_id: str, tokens: int) -> UserAccount:
        """Refund — subtract ``tokens`` from the usage counter, floored at 0."""
        if tokens < 0:
            raise ValueError("Credit amount must be non-negative.")
        account = await self.get_or_create(user_id)
        if tokens:
            account.credits.tokens_used = max(0, account.credits.tokens_used - tokens)
        await self.save(account)
        return account

    async def update_limits(
        self, user_id: str, updates: dict[str, int | None]
    ) -> UserAccount:
        """Apply the given fields. Pass ``{field: None}`` to mark unlimited;
        omit a field to leave it unchanged.

        ``monthly_token_limit`` is routed into the nested ``credits`` object.
        """
        bad = set(updates) - _LIMIT_FIELDS
        if bad:
            raise ValueError(f"Cannot update fields {sorted(bad)!r}")
        account = await self.get_or_create(user_id)
        for key, value in updates.items():
            if key == "monthly_token_limit":
                account.credits.monthly_token_limit = value
            else:
                setattr(account, key, value)
        await self.save(account)
        return account

    async def reset_to_role_defaults(
        self, user_id: str, *, role: str = "member"
    ) -> UserAccount:
        """Reset the limits (only) to the config defaults for ``role``. Used
        by the role-set flow so promotion/demotion keeps storage in sync.
        Preserves ``tokens_used`` and ``usage_period``."""
        account = await self.get_or_create(user_id, role=role)
        seed = self._defaults(user_id, role=role)
        account.motor_limit = seed.motor_limit
        account.simulation_limit = seed.simulation_limit
        account.credits.monthly_token_limit = seed.credits.monthly_token_limit
        await self.save(account)
        return account
