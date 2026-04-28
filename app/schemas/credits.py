"""Schemas for the credit / usage system.

Tokens are the internal unit of accounting (one ``machwave token`` per integration
step worth of compute). User-facing pricing maps tokens to USD (e.g. 10,000
tokens = $0.10).

Credit state lives nested under ``UserAccount.credits`` (a :class:`CreditAccount`)
so token-related fields are isolated from storage caps (``motor_limit``,
``simulation_limit``). The same ``CreditAccount`` shape is reused as the
response wire format — ``tokens_remaining`` is a computed field so the JSON
exposes the derived value without requiring it to be persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

_PERIOD_PATTERN = r"^\d{4}-\d{2}$"


def current_period_utc() -> str:
    """Current monthly period as ``YYYY-MM``."""
    return datetime.now(UTC).strftime("%Y-%m")


class CreditAccount(BaseModel):
    """Credit / token state for a single user.

    Stored at ``users/{uid}/account.json`` under the ``credits`` key. The
    same shape is also returned in account snapshots — ``tokens_remaining``
    is computed and added to JSON output but excluded from storage writes
    so the on-disk record stays minimal.
    """

    model_config = ConfigDict(validate_assignment=True)

    monthly_token_limit: int | None = Field(default=None, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    usage_period: str = Field(
        pattern=_PERIOD_PATTERN,
        description="ISO month YYYY-MM (UTC) the usage counter pertains to",
    )

    # ``tokens_remaining`` is exposed in JSON output so the frontend gets it
    # without recomputing. Excluded on save (see AccountRepository.save) so
    # storage carries the raw fields only and can never drift from them.
    @computed_field
    @property
    def tokens_remaining(self) -> int | None:
        if self.monthly_token_limit is None:
            return None
        return max(0, self.monthly_token_limit - self.tokens_used)

    @property
    def is_unlimited(self) -> bool:
        return self.monthly_token_limit is None

    def can_afford(self, tokens: int) -> bool:
        """Return True if ``tokens`` can be debited without exceeding the limit."""
        if self.is_unlimited:
            return True
        return (self.tokens_remaining or 0) >= tokens

    def is_period_stale(self, current_period: str | None = None) -> bool:
        """True when ``usage_period`` is older than the current calendar month."""
        return self.usage_period < (current_period or current_period_utc())

    @classmethod
    def fresh(cls, *, role: str, default_limit: int) -> CreditAccount:
        """Build a starter credit account: admins get unlimited, members get
        the config-default monthly token limit. Usage starts at 0 and the
        period is set to the current month."""
        return cls(
            monthly_token_limit=None if role == "admin" else default_limit,
            tokens_used=0,
            usage_period=current_period_utc(),
        )


class UserAccount(BaseModel):
    """Per-user billing & quota record at ``users/{uid}/account.json``.

    Storage caps (``motor_limit``, ``simulation_limit``) live at the top level;
    token state is nested under ``credits``. ``None`` on any cap means
    unlimited — admins are seeded with ``None`` everywhere; members get the
    config defaults. Role transitions sync these via ``admin_set_role``.
    """

    user_id: str

    motor_limit: int | None
    simulation_limit: int | None
    credits: CreditAccount

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SimulationCostRecord(BaseModel):
    """Per-simulation cost ledger.

    Stored at ``users/{uid}/simulations/{sid}/cost.json``. ``estimated_tokens``
    is set at submit; the worker fills in ``actual_tokens``, ``iterations``,
    and the final ``tokens_charged`` once the run completes.
    """

    simulation_id: str

    estimated_tokens: int = Field(ge=0)
    actual_tokens: int | None = Field(default=None, ge=0)
    iterations: int | None = Field(default=None, ge=0)
    tokens_charged: int = Field(default=0, ge=0, description="Net tokens added to usage")

    period: str = Field(description="Monthly period (YYYY-MM) the charge counts toward")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    refunded: bool = False


class AccountSnapshot(BaseModel):
    """``GET /me/account`` and ``GET /admin/users/{uid}/account`` response.

    ``credits`` mirrors the storage shape; ``motor_count`` and
    ``simulation_count`` are populated only on the admin endpoints so the
    admin UI can render usage vs caps without a second round trip. The
    user-self endpoint leaves them ``None`` and ``/me/usage`` is the
    composite view that fills them in for the calling user.
    """

    user_id: str
    motor_limit: int | None
    simulation_limit: int | None
    credits: CreditAccount
    is_admin: bool
    motor_count: int | None = None
    simulation_count: int | None = None


class UsageSnapshot(BaseModel):
    """``GET /me/usage`` response — counts vs caps + nested credit summary.

    Frontend renders this as the user's quota panel.
    """

    motor_count: int
    motor_limit: int | None
    motors_remaining: int | None

    simulation_count: int
    simulation_limit: int | None
    simulations_remaining: int | None

    credits: CreditAccount

    is_admin: bool
