"""
commons/middleware.py
─────────────────────
Cross-cutting HTTP middleware:
  - Request ID injection & timing
  - Security headers (CSP, HSTS, etc.)
  - Structured access logging
"""
from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.commons.config import cfg
from app.commons.flags import flags
from app.commons.logger import get_logger, set_request_context

log = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Assigns a unique request_id to every request, sets timing, and
    injects it into structured logs and response headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        session_id = request.headers.get("X-Session-ID", "")
        user_id = getattr(getattr(request, "state", None), "user_id", "")

        set_request_context(request_id=request_id, session_id=session_id, user_id=user_id)

        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        response.headers["X-Request-ID"] = request_id

        if flags.request_logging_enabled:
            log.bind(
                func="request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                request_id=request_id,
            ).info(f"{request.method} {request.url.path} → {response.status_code} ({duration_ms}ms)")

        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security-related response headers.
    CSP can be toggled via feature flag.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        if not cfg.is_production():
            response.headers["X-Environment"] = cfg.ENV

        if flags.csp_enabled:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self';"
            )

        return response
