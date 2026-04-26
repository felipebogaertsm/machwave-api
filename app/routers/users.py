"""Users router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.firebase import get_current_user
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
