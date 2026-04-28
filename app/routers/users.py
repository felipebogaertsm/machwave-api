"""Users router."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import firebase_admin
from fastapi import APIRouter, Depends, HTTPException, Query, status
from firebase_admin import auth as firebase_auth
from pydantic import BaseModel

from app.auth.firebase import get_current_user, get_firebase_app
from app.auth.rbac import Role, get_user_role, require_role
from app.repositories.account import AccountRepository
from app.storage import gcs

router = APIRouter()
admin_router = APIRouter()


class UserSummary(BaseModel):
    uid: str
    email: str | None
    email_verified: bool
    display_name: str | None
    photo_url: str | None
    disabled: bool
    role: Role
    created_at: datetime | None
    last_sign_in_at: datetime | None


class ListUsersResponse(BaseModel):
    users: list[UserSummary]
    next_page_token: str | None
    has_more: bool


class SetRoleRequest(BaseModel):
    role: Role


class SetDisabledRequest(BaseModel):
    disabled: bool


def _ms_to_datetime(value: int | float | None) -> datetime | None:
    """Firebase timestamps come back as ms since epoch. None when unset."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _summarize(record: firebase_auth.UserRecord) -> UserSummary:
    claims = record.custom_claims or {}
    metadata = getattr(record, "user_metadata", None)
    creation_ts = getattr(metadata, "creation_timestamp", None) if metadata else None
    last_sign_in_ts = getattr(metadata, "last_sign_in_timestamp", None) if metadata else None
    return UserSummary(
        uid=str(record.uid),
        email=record.email,
        email_verified=bool(getattr(record, "email_verified", False)),
        display_name=record.display_name,
        photo_url=getattr(record, "photo_url", None),
        disabled=bool(record.disabled),
        role=get_user_role(claims),
        created_at=_ms_to_datetime(creation_ts),
        last_sign_in_at=_ms_to_datetime(last_sign_in_ts),
    )


def _user_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")


@admin_router.get("", response_model=ListUsersResponse)
async def admin_list_users(
    page_token: str | None = Query(default=None),
    max_results: int = Query(default=100, ge=1, le=1000),
    _: dict[str, Any] = Depends(require_role("admin")),
    app: firebase_admin.App = Depends(get_firebase_app),
) -> ListUsersResponse:
    """List Firebase users. Admin-only.

    Token-based pagination — pass ``page_token`` from the previous response to
    fetch the next page. ``has_more`` is true while another page exists.
    """
    page = await asyncio.to_thread(
        firebase_auth.list_users,
        page_token=page_token,
        max_results=max_results,
        app=app,
    )
    next_token = page.next_page_token or None
    return ListUsersResponse(
        users=[_summarize(u) for u in page.users],
        next_page_token=next_token,
        has_more=next_token is not None,
    )


@admin_router.put("/{user_id}/role", response_model=UserSummary)
async def admin_set_role(
    user_id: str,
    body: SetRoleRequest,
    actor: dict[str, Any] = Depends(require_role("admin")),
    app: firebase_admin.App = Depends(get_firebase_app),
    account_repo: AccountRepository = Depends(AccountRepository),
) -> UserSummary:
    """Set a user's role custom claim. Admin-only.

    Also syncs the user's account limits to the new role's defaults — admins
    get ``None`` (unlimited), members get config defaults — so storage stays
    consistent with the claim. The user must sign out and back in (or call
    ``getIdToken(true)``) before the new claim takes effect on their client.
    """
    if actor["uid"] == user_id and body.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admins cannot demote themselves. Have another admin do it.",
        )

    try:
        record = await asyncio.to_thread(firebase_auth.get_user, user_id, app=app)
    except firebase_auth.UserNotFoundError as err:
        raise _user_not_found() from err

    claims = dict(record.custom_claims or {})
    if body.role == "member":
        claims.pop("role", None)
    else:
        claims["role"] = body.role

    await asyncio.to_thread(
        firebase_auth.set_custom_user_claims,
        user_id,
        claims or None,
        app=app,
    )
    await account_repo.reset_to_role_defaults(user_id, role=body.role)
    record = await asyncio.to_thread(firebase_auth.get_user, user_id, app=app)
    return _summarize(record)


@admin_router.put("/{user_id}/disabled", response_model=UserSummary)
async def admin_set_disabled(
    user_id: str,
    body: SetDisabledRequest,
    actor: dict[str, Any] = Depends(require_role("admin")),
    app: firebase_admin.App = Depends(get_firebase_app),
) -> UserSummary:
    """Enable or disable a user account. Disabled users cannot sign in or
    refresh tokens. Existing ID tokens remain valid until they expire (≤1 hour).
    """
    if actor["uid"] == user_id and body.disabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admins cannot disable themselves.",
        )

    try:
        record = await asyncio.to_thread(
            firebase_auth.update_user,
            user_id,
            disabled=body.disabled,
            app=app,
        )
    except firebase_auth.UserNotFoundError as err:
        raise _user_not_found() from err

    return _summarize(record)


@admin_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_user(
    user_id: str,
    actor: dict[str, Any] = Depends(require_role("admin")),
    app: firebase_admin.App = Depends(get_firebase_app),
) -> None:
    """Permanently delete a user's Firebase account and all of their GCS data.

    Admins cannot delete themselves through this endpoint — use the self-delete
    flow at ``DELETE /users/{user_id}`` instead.
    """
    if actor["uid"] == user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Admins cannot delete themselves through the admin endpoint.",
        )

    try:
        await asyncio.to_thread(firebase_auth.get_user, user_id, app=app)
    except firebase_auth.UserNotFoundError as err:
        raise _user_not_found() from err

    await gcs.delete_prefix(f"users/{user_id}/")
    await asyncio.to_thread(firebase_auth.delete_user, user_id, app=app)


@router.delete("/{user_id}/clear", status_code=status.HTTP_204_NO_CONTENT)
async def clear_account(
    user_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Delete all GCS data belonging to the authenticated user."""
    if user["uid"] != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    await gcs.delete_prefix(f"users/{user_id}/")


class DeleteAccountRequest(BaseModel):
    email: str


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    user_id: str,
    body: DeleteAccountRequest,
    user: dict[str, Any] = Depends(get_current_user),
    app: firebase_admin.App = Depends(get_firebase_app),
) -> None:
    """Permanently delete the authenticated user's GCS data and Firebase account.

    Requires the user to confirm their email address in the request body.
    """
    if user["uid"] != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    token_email: str | None = user.get("email")
    if not token_email or token_email.lower() != body.email.lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Email confirmation does not match the account email.",
        )

    await gcs.delete_prefix(f"users/{user_id}/")
    await asyncio.to_thread(firebase_auth.delete_user, user_id, app=app)
