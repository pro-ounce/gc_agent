"""
rbac/middleware.py
──────────────────
Authentication middleware — tries JWT Bearer, then API key header.
Attaches a User (or synthetic service user) to request.state.user.
Public paths listed in _PUBLIC_PATHS bypass auth entirely.
"""
from __future__ import annotations

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ..commons.config import cfg
from ..commons.flags import flags
from ..commons.logger import get_logger, set_request_context
from ..rbac.gateway_auth import authenticate_gateway
from ..rbac.jwt_handler import JWTError, decode_token
from ..rbac.models import APIKey, User
from ..rbac.permissions import get_permissions_for_roles

log = get_logger(__name__)

_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health", "/api/health",
        "/health/live", "/api/health/live",
        "/health/ready", "/api/health/ready",
        "/metrics",
        "/docs", "/openapi.json", "/redoc",
    }
)


class RBACMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not flags.auth_enabled:
            request.state.user = _anonymous()
            request.state.auth_method = "disabled"
            return await call_next(request)

        path = request.url.path
        if (
            path in _PUBLIC_PATHS
            or path.startswith("/assets")
            or path.startswith(cfg.MANAGEMENT_BASE_PATH)   # /actuator/* — actuator, guarded by IP allow-list
            or path.startswith("/admin")                   # /admin/* — config UI, same IP allow-list guard
        ):
            return await call_next(request)

        user, method = await self._authenticate(request)

        if user is None:
            return Response(
                content='{"detail": "Unauthorized"}',
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.user = user
        request.state.auth_method = method
        # Update logger context with user_id
        set_request_context(user_id=user.id if user else "")

        return await call_next(request)

    async def _authenticate(self, request: Request) -> tuple[User | None, str]:
        # 0. Platform gateway-vouched auth — verify the gc gateway's X-INT-TKN.
        #    Primary path when the agent runs behind the gateway (PLATFORM_AUTH_MODE=gateway).
        if cfg.PLATFORM_AUTH_MODE.lower() == "gateway":
            user, method = authenticate_gateway(request)
            if user is not None:
                return user, method

        # 1. JWT Bearer (standalone login)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                payload = decode_token(token)
                user = User.find_by_id(payload.get("sub", ""))
                if user and user.is_active:
                    return user, "jwt"
            except JWTError as exc:
                log.warning(f"JWT auth failed: {exc}")

        # 2. API Key header
        raw_key = (
            request.headers.get("X-API-KEY")
            or request.headers.get("X-Api-Key")
        )
        if raw_key:
            api_key = APIKey.find_by_raw(raw_key)
            if api_key:
                api_key.touch()
                # Build synthetic User from API key
                user = User(
                    id=api_key.user_id or f"apikey:{api_key.id}",
                    username=api_key.name,
                    roles=api_key.roles,
                )
                return user, "api_key"

        return None, ""


def _anonymous() -> User:
    return User(id="anonymous", username="anonymous", roles=["user"])


# ── FastAPI dependency for current user ───────────────────────────────────────

def get_current_user(request: Request) -> User:
    # Auth disabled globally — everyone is an anonymous user
    if not flags.auth_enabled:
        return _anonymous()
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_permission(permission: str):
    """FastAPI dependency that checks a single permission."""
    def _dep(request: Request) -> User:
        # Auth/RBAC disabled globally — pass through
        if not flags.auth_enabled:
            return _anonymous()
        user = get_current_user(request)
        if not flags.rbac_enabled:
            return user
        perms = get_permissions_for_roles(user.roles)
        if permission not in perms:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: '{permission}' required",
            )
        return user
    return _dep
