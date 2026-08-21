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

import jwt
from fastapi import Request

from ..commons.config import cfg
from ..commons.logger import get_logger
from ..rbac.jwt_handler import JWTError, decode_internal_token, decode_user_token
from ..rbac.models import User

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
    # Fallback: some gateway routes carry the vouching token in Authorization: Bearer
    # rather than X-INT-TKN. Try that before giving up.
    if not token:
        authz = request.headers.get("authorization") or request.headers.get("Authorization")
        if authz and authz.lower().startswith("bearer "):
            token = authz[7:].strip()
    if not token:
        log.info(
            f"gateway auth: no {header!r} / Bearer token on request "
            f"(headers present: {sorted(request.headers.keys())})"
        )
        return None, ""

    # 1. Internal token (X-INT-TKN style: carries X_TP, signed with the internal key).
    try:
        claims = decode_internal_token(token)
        ttype = claims.get(_TYPE_CLAIM)
        if ttype in (_TYPE_USER, _TYPE_SVC):
            is_service = ttype == _TYPE_SVC
            user_id = str(claims.get("sub", "0"))
            username = str(
                claims.get("uname", "")
                or (f"service:{claims.get('X_SVC', 'gc')}" if is_service else user_id)
            )
            return (
                User(id=user_id, username=username,
                     roles=_map_roles(claims.get("authorities"), is_service), is_active=True),
                "gateway",
            )
    except JWTError:
        pass  # not the internal token → try the user JWT below

    # 2. User JWT (iss=GC360) the gateway forwards, verified with the user signing key.
    try:
        claims = decode_user_token(token)
        user_id = str(claims.get("sub", "0"))
        username = str(claims.get("uname", "") or user_id)
        return (
            User(id=user_id, username=username,
                 roles=_map_roles(claims.get("authorities"), False), is_active=True),
            "gateway-user",
        )
    except JWTError as exc:
        # Both keys failed. Log the token's UNVERIFIED alg/issuer (never the signature)
        # so we know which key it actually needs.
        try:
            hdr = jwt.get_unverified_header(token)
            unv = jwt.decode(token, options={"verify_signature": False})
            hint = f"alg={hdr.get('alg')} iss={unv.get('iss')!r} has_uname={'uname' in unv}"
        except Exception:  # noqa: BLE001
            hint = "unparseable token"
        log.warning(f"gateway auth: token verification failed ({exc}); token is [{hint}]")
        return None, ""
