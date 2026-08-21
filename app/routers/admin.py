"""
routers/admin.py
────────────────
Self-served agent config UI (mirrors the RAG app's /admin). A single page at GET /admin
lets an operator tune the runtime parameters (model, retrieval top-K, iterations, history
budget, toggles) without editing .env.local or restarting — saves persist to OpenSearch and
take effect on the next turn via runtime_config's short-TTL cache.

Gated by the same loopback/allow-list as the actuator (ACTUATOR_ALLOWED_IPS), so it's
reachable on the ops LAN exactly like /actuator/* — not exposed through the public gateway.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app.commons.logger import get_logger
from app.routers.health import _guard  # reuse the actuator IP allow-list guard
from app.services import runtime_config

log = get_logger(__name__)

router = APIRouter(tags=["admin"])

_ADMIN_HTML = Path(__file__).resolve().parent.parent / "static" / "admin.html"


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    _guard(request)
    if _ADMIN_HTML.exists():
        return FileResponse(str(_ADMIN_HTML))
    return JSONResponse({"detail": "admin UI not found"}, status_code=404)


@router.get("/admin/config", summary="Current runtime config (values + defaults)")
async def admin_get_config(request: Request):
    _guard(request)
    return {"params": runtime_config.get_all()}


@router.post("/admin/config", summary="Update runtime config overrides")
async def admin_set_config(request: Request):
    _guard(request)
    body = await request.json()
    updates = body.get("updates", body) if isinstance(body, dict) else {}
    applied = runtime_config.set_many(updates)
    return {"applied": applied, "params": runtime_config.get_all()}


@router.post("/admin/config/reset", summary="Reset all overrides to .env defaults")
async def admin_reset_config(request: Request):
    _guard(request)
    runtime_config.reset()
    return {"params": runtime_config.get_all()}
