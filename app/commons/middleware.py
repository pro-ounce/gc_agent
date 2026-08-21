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

from ..commons import metrics as M
from ..commons.config import cfg
from ..commons.flags import flags
from ..commons.logger import get_logger, set_request_context

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
        # propagate the gc platform trace id for cross-service correlation
        trace_id = request.headers.get("X-TRACE-ID") or request.headers.get("X-Trace-Id", "")
        user_id = getattr(getattr(request, "state", None), "user_id", "")

        set_request_context(request_id=request_id, session_id=session_id,
                            user_id=user_id, trace_id=trace_id)

        M.http_in_flight.inc()
        start = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
        finally:
            elapsed = time.perf_counter() - start
            M.http_in_flight.dec()
            # low-cardinality 'uri' label: matched route template (micrometer convention)
            route = request.scope.get("route")
            uri = getattr(route, "path", None) or request.url.path
            M.http_server_requests.labels(
                request.method, uri, str(status), M.http_outcome(status)
            ).observe(elapsed)

        duration_ms = round(elapsed * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        if trace_id:
            response.headers["X-TRACE-ID"] = trace_id

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
