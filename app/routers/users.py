"""Users router."""

from __future__ import annotations

import asyncio
from typing import Any

import firebase_admin
from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import auth as firebase_auth
from pydantic import BaseModel

from app.auth.firebase import get_current_user, get_firebase_app
from app.storage import gcs

router = APIRouter()


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
