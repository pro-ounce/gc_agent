"""
rbac/gateway_auth.py
────────────────────
Platform ("gateway-vouched") authentication.

When PLATFORM_AUTH_MODE=gateway, the agent runs behind the gc gateway. The gateway
validates the browser cookie / user JWT (using the gc DB-held HMAC key) and forwards a
short-lived internal token — X-INT-TKN (HS512, iss=GC_INTERNAL, aud=GC_INTERNAL_API,
X_TP=SVC_PER_USER, sub=userId, ~300s TTL).

The agent verifies THAT token. Because it is signed with the gc key and carries the
user, it is cryptographic proof the request came from the gateway — safe whether the
agent is co-located or on a separate server (no reliance on network isolation).

The agent never reads the DB and never handles the browser cookie; it only holds a
sealed copy of the HMAC key (GC_JWT_SECRET) to verify this one token.
"""
from __future__ import annotations

from fastapi import Request

from app.commons.config import cfg
from app.commons.logger import get_logger
from app.rbac.jwt_handler import JWTError, decode_internal_token
from app.rbac.models import User

log = get_logger(__name__)

# gc InternalTokenProvider claim names / values
_TYPE_CLAIM = "X_TP"
_TYPE_USER = "SVC_PER_USER"   # user-scoped internal call (carries userId)
_TYPE_SVC = "INT"             # pure service-to-service call


def _map_roles(authorities: object, is_service: bool) -> list[str]:
    """Map gc authorities (e.g. ['ADMIN'] / ['USER']) to the agent's role vocabulary."""
    vals: list[str] = []
    if isinstance(authorities, list):
        vals = [str(a).upper() for a in authorities]
    elif isinstance(authorities, str):
        vals = [authorities.upper()]
    if any("ADMIN" in v for v in vals):
        return ["admin"]
    if is_service or any(("OPERATOR" in v) or ("SYSTEM" in v) for v in vals):
        return ["operator"]
    return ["user"]


def authenticate_gateway(request: Request) -> tuple[User | None, str]:
    """
    Return (User, "gateway") if a valid gateway internal token is present, else (None, "").
    The returned User is transient (built from token claims) — not persisted to Redis.
    """
    header = cfg.GC_INTERNAL_HEADER
    token = request.headers.get(header) or request.headers.get(header.lower())
    if not token:
        return None, ""

    try:
        claims = decode_internal_token(token)
    except JWTError as exc:
        log.warning(f"gateway auth: internal-token verification failed: {exc}")
        return None, ""

    ttype = claims.get(_TYPE_CLAIM)
    if ttype not in (_TYPE_USER, _TYPE_SVC):
        log.warning(f"gateway auth: unexpected internal token type '{ttype}'")
        return None, ""

    is_service = ttype == _TYPE_SVC
    user_id = str(claims.get("sub", "0"))
    username = str(claims.get("uname", "") or (f"service:{claims.get('X_SVC', 'gc')}" if is_service else user_id))

    user = User(
        id=user_id,
        username=username,
        roles=_map_roles(claims.get("authorities"), is_service),
        is_active=True,
    )
    return user, "gateway"
