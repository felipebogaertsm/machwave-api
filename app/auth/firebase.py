from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=True)


@lru_cache(maxsize=1)
def _init_firebase_app(project_id: str) -> firebase_admin.App:
    return firebase_admin.initialize_app(
        options={"projectId": project_id},
    )


def _get_firebase_app(settings: Settings = Depends(get_settings)) -> firebase_admin.App:
    return _init_firebase_app(settings.firebase_project_id)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    app: firebase_admin.App = Depends(_get_firebase_app),
) -> dict[str, Any]:
    from firebase_admin import auth as firebase_auth

    try:
        decoded = firebase_auth.verify_id_token(
            credentials.credentials,
            app=app,
            check_revoked=False,
        )
    except firebase_auth.RevokedIdTokenError as err:
        logger.warning("Token revoked: %s", err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please sign in again.",
        ) from err
    except firebase_auth.ExpiredIdTokenError as err:
        logger.warning("Token expired: %s", err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please sign in again.",
        ) from err
    except firebase_auth.InvalidIdTokenError as err:
        logger.warning("Invalid token: %s", err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        ) from err
    except Exception as err:
        logger.exception("Unexpected token validation failure: %s", err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
        ) from err

    return decoded
