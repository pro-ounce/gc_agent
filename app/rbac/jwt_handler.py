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


def decode_internal_token(token: str) -> dict[str, Any]:
    """
    Verify a gc gateway internal token (X-INT-TKN).

    HS512, signed with the shared gc key (GC_JWT_SECRET, sealed from the DB setting),
    with issuer/audience checked. This is the cryptographic proof that a request came
    from the gc gateway — used by PLATFORM_AUTH_MODE=gateway.
    """
    secret = cfg.GC_JWT_SECRET
    if not secret:
        raise JWTError("GC_JWT_SECRET is not configured (required for PLATFORM_AUTH_MODE=gateway)")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[cfg.GC_JWT_ALGORITHM],
            issuer=cfg.GC_INTERNAL_ISSUER,
            audience=cfg.GC_INTERNAL_AUDIENCE,
            leeway=cfg.GC_JWT_LEEWAY,
        )
    except jwt.ExpiredSignatureError:
        raise JWTError("Internal token has expired")
    except jwt.InvalidTokenError as exc:
        raise JWTError(f"Invalid internal token: {exc}")
