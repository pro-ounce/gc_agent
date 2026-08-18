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
# Accept both HMAC variants on decode: the platform signs user JWTs with HS512, while
# the agent's own standalone tokens use JWT_ALGORITHM (default HS256). Same shared
# secret, so allowing both is safe — no symmetric/asymmetric confusion risk.
_DECODE_ALGORITHMS = sorted({cfg.JWT_ALGORITHM, "HS256", "HS512"})


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
        return jwt.decode(token, cfg.JWT_SECRET, algorithms=_DECODE_ALGORITHMS)
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
    # The HMAC signature (shared gateway secret) is the trust anchor — only the gateway
    # can produce a validly-signed token. issuer/audience are secondary and not every
    # gateway build sets them (notably no `aud`), so verify them only when explicitly
    # enabled; otherwise a correctly-signed token is wrongly rejected.
    kwargs: dict[str, Any] = {}
    options: dict[str, bool] = {}
    if cfg.GC_VERIFY_ISSUER and cfg.GC_INTERNAL_ISSUER:
        kwargs["issuer"] = cfg.GC_INTERNAL_ISSUER
    else:
        options["verify_iss"] = False
    if cfg.GC_VERIFY_AUDIENCE and cfg.GC_INTERNAL_AUDIENCE:
        kwargs["audience"] = cfg.GC_INTERNAL_AUDIENCE
    else:
        options["verify_aud"] = False
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[cfg.GC_JWT_ALGORITHM],
            leeway=cfg.GC_JWT_LEEWAY,
            options=options,
            **kwargs,
        )
    except jwt.ExpiredSignatureError:
        raise JWTError("Internal token has expired")
    except jwt.InvalidTokenError as exc:
        raise JWTError(f"Invalid internal token: {exc}")


def decode_user_token(token: str) -> dict[str, Any]:
    """Verify a platform USER JWT (iss=GC360) that the gateway forwards in Authorization,
    using the separate user signing key (GC_USER_JWT_SECRET). Signature-first."""
    secret = cfg.GC_USER_JWT_SECRET
    if not secret:
        raise JWTError("GC_USER_JWT_SECRET is not configured")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=_DECODE_ALGORITHMS,
            leeway=cfg.GC_JWT_LEEWAY,
            options={"verify_aud": False, "verify_iss": False},
        )
    except jwt.ExpiredSignatureError:
        raise JWTError("User token has expired")
    except jwt.InvalidTokenError as exc:
        raise JWTError(f"Invalid user token: {exc}")
