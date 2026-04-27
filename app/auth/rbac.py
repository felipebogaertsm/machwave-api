"""Role-based access control on top of Firebase custom claims.

Roles live in the ``role`` custom claim on a user's Firebase ID token. Users
without the claim are treated as ``member`` (the default for any signed-in
account). Promote a user with ``scripts/set_user_role.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Literal

from fastapi import Depends, HTTPException, status

from app.auth.firebase import get_current_user

Role = Literal["admin", "member"]

_VALID_ROLES: frozenset[str] = frozenset({"admin", "member"})
DEFAULT_ROLE: Role = "member"


def get_user_role(user: dict[str, Any]) -> Role:
    """Return the user's role, defaulting to ``member`` if the claim is absent or unknown."""
    claim = user.get("role")
    if claim in _VALID_ROLES:
        return claim  # type: ignore[return-value]
    return DEFAULT_ROLE


@lru_cache(maxsize=len(_VALID_ROLES))
def require_role(required: Role) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build a FastAPI dependency that allows only users holding ``required``.

    Cached per role so each route gets the same dependency callable (lets
    FastAPI reuse the resolved value within a request).
    """

    async def _dependency(
        user: dict[str, Any] = Depends(get_current_user),
    ) -> dict[str, Any]:
        if get_user_role(user) != required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires '{required}' role.",
            )
        return user

    return _dependency
