"""
rbac/jwt_handler.py
────────────────────
JWT encode / decode helpers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.commons.config import cfg
from app.commons.logger import get_logger

log = get_logger(__name__)

_ALGORITHM = cfg.JWT_ALGORITHM


class JWTError(Exception):
    pass


def create_access_token(payload: dict[str, Any]) -> str:
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(minutes=cfg.JWT_EXPIRE_MINUTES)
    data["iat"] = datetime.now(timezone.utc)
    data["type"] = "access"
    return jwt.encode(data, cfg.JWT_SECRET, algorithm=_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    data = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=cfg.JWT_REFRESH_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    }
    return jwt.encode(data, cfg.JWT_SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, cfg.JWT_SECRET, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise JWTError("Token has expired")
    except jwt.InvalidTokenError as exc:
        raise JWTError(f"Invalid token: {exc}")
