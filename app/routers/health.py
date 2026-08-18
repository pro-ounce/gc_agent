"""
routers/health.py
─────────────────
Actuator-style observability, matching the gc Spring services (commons-driven):

  GET  {base}/health              aggregate health (Spring shape: status + components)
  GET  {base}/health/liveness     liveness group  (process up)
  GET  {base}/health/readiness    readiness group (store + mcp reachable)
  GET  {base}/info                app / build info
  GET  {base}/prometheus          Prometheus exposition (http_server_requests_seconds, etc.)

  base = MANAGEMENT_BASE_PATH (default /actuator).
Aliases kept for convenience/back-compat: /health, /api/health, /metrics.

Actuator endpoints follow the platform's show-details=always convention and an optional
loopback allow-list (ACTUATOR_ALLOWED_IPS), mirroring app.security.actuator.allowed-ips.
"""
from __future__ import annotations

import os
import sys
import time

from fastapi import APIRouter, HTTPException, Request, Response

from app.commons import metrics as M
from app.commons.config import cfg
from app.commons.flags import flags
from app.connections import store_health
from app.mcp.client import mcp_client
from app.mcp.tool_registry import tool_registry
from app.services.llm_service import llm
from app.services.session_service import count_sessions

# Process start — used for uptime in /info.
_START_TS = time.time()

router = APIRouter(tags=["actuator"])
actuator = APIRouter(prefix=cfg.MANAGEMENT_BASE_PATH, tags=["actuator"])


def _guard(request: Request) -> None:
    """Optional loopback/allow-list guard (mirrors app.security.actuator.allowed-ips)."""
    allowed = cfg.ACTUATOR_ALLOWED_IPS
    if not allowed:
        return
    client = request.client.host if request.client else ""
    if client not in allowed:
        raise HTTPException(status_code=403, detail="actuator access restricted")


def _comp(status: str, **details) -> dict:
    return {"status": status, "details": details} if details else {"status": status}


async def _components() -> dict:
    store = store_health()
    mcp = await mcp_client.health()
    try:
        lm = await llm().health()
    except Exception as exc:  # noqa: BLE001
        lm = {"status": "DOWN", "provider": cfg.LLM_PROVIDER, "error": str(exc)}

    up = lambda d: str(d.get("status", "")).upper() == "UP"
    M.set_dependency("store", up(store))
    M.set_dependency("mcp", up(mcp))
    M.set_dependency("llm", up(lm))

    return {
        "store": _comp("UP" if up(store) else "DOWN", backend=store.get("backend")),
        "mcp": _comp("UP" if up(mcp) else "DOWN", **{k: v for k, v in mcp.items() if k != "status"}),
        "llm": _comp("UP" if up(lm) else "DOWN", **{k: v for k, v in lm.items() if k != "status"}),
    }


def _aggregate(components: dict) -> str:
    statuses = {c["status"] for c in components.values()}
    return "UP" if statuses == {"UP"} else "DOWN"


async def _health_body() -> dict:
    comps = await _components()
    return {"status": _aggregate(comps), "components": comps}


# ── Actuator endpoints ────────────────────────────────────────────────────────
@actuator.get("/health", summary="Aggregate health")
async def actuator_health(request: Request, response: Response) -> dict:
    _guard(request)
    body = await _health_body()
    response.status_code = 200 if body["status"] == "UP" else 503
    return body


@actuator.get("/health/liveness", summary="Liveness")
async def actuator_liveness(request: Request) -> dict:
    _guard(request)
    return {"status": "UP"}


@actuator.get("/health/readiness", summary="Readiness")
async def actuator_readiness(request: Request, response: Response) -> dict:
    _guard(request)
    comps = await _components()
    ready = comps["store"]["status"] == "UP" and comps["mcp"]["status"] == "UP"
    response.status_code = 200 if ready else 503
    return {"status": "UP" if ready else "OUT_OF_SERVICE",
            "components": {"store": comps["store"], "mcp": comps["mcp"]}}


@actuator.get("/info", summary="App / build / runtime info")
async def actuator_info(request: Request) -> dict:
    _guard(request)
    try:
        tools_loaded = len(tool_registry._cache or [])
    except Exception:  # noqa: BLE001
        tools_loaded = None
    try:
        sessions = count_sessions()
    except Exception:  # noqa: BLE001
        sessions = None
    return {
        "app": {"name": cfg.APP_NAME, "version": cfg.APP_VERSION, "env": cfg.ENV},
        # Build metadata is injected at deploy time (env or a build_info file). rsync
        # deploys carry no .git, so these are env-sourced with 'unknown' fallbacks.
        "build": {
            "commit": os.getenv("BUILD_COMMIT") or os.getenv("GIT_COMMIT") or "unknown",
            "time": os.getenv("BUILD_TIME") or "unknown",
            "python": sys.version.split()[0],
        },
        "runtime": {
            "uptime_seconds": round(time.time() - _START_TS, 1),
            "llm": {
                "provider": cfg.LLM_PROVIDER,
                "model": cfg.LLM_MODEL,
                "num_ctx": cfg.LLM_NUM_CTX,
                "max_tokens": cfg.LLM_MAX_TOKENS,
            },
            "embed_model": cfg.EMBED_MODEL,
            "store_backend": store_health().get("backend"),
            "auth_mode": cfg.PLATFORM_AUTH_MODE,
            "tools_loaded": tools_loaded,
            "sessions": sessions,
        },
        "features": {
            "auth_enabled": flags.auth_enabled,
            "streaming_enabled": flags.streaming_enabled,
            "tool_rag_enabled": flags.tool_rag_enabled,
            "tool_risk_confirmation": flags.tool_risk_confirmation,
            "chatbot_read_only": flags.chatbot_read_only,
            "strict_grounding": flags.strict_grounding,
        },
        "metrics": M.summary(),
    }


@actuator.get("/prometheus", include_in_schema=False)
async def actuator_prometheus(request: Request) -> Response:
    _guard(request)
    return _render_metrics()


router.include_router(actuator)


# ── Aliases (convenience / back-compat) ───────────────────────────────────────
@router.get("/health", summary="Health (alias)")
@router.get("/api/health", include_in_schema=False)
async def health_alias(response: Response) -> dict:
    body = await _health_body()
    response.status_code = 200 if body["status"] == "UP" else 503
    # flat shape kept for older callers
    return {"status": body["status"], "app": cfg.APP_NAME, "version": cfg.APP_VERSION,
            "env": cfg.ENV, "components": body["components"]}


@router.get("/metrics", include_in_schema=False)
async def metrics_alias() -> Response:
    return _render_metrics()


def _render_metrics() -> Response:
    if not flags.metrics_enabled:
        return Response(status_code=404)
    M.build_info.labels(cfg.APP_VERSION, cfg.ENV, cfg.LLM_PROVIDER,
                        store_health().get("backend", "?")).set(1)
    M.active_sessions.set(count_sessions())
    body, ctype = M.render()
    return Response(content=body, media_type=ctype)
