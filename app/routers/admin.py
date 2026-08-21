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
from app.services import backup_service, runtime_config

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


@router.get("/admin/backup", summary="Backup overview — repo, schedule, snapshots, stats")
async def admin_backup_overview(request: Request):
    _guard(request)
    return backup_service.overview()


@router.post("/admin/backup", summary="Take a manual snapshot now (e.g. before a major push)")
async def admin_take_backup(request: Request):
    _guard(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    label = (body or {}).get("label") if isinstance(body, dict) else None
    wait = bool((body or {}).get("wait")) if isinstance(body, dict) else False
    try:
        result = backup_service.create_snapshot(label=label, wait=wait)
    except backup_service.BackupError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return result


@router.post("/admin/backup/schedule", summary="Create/update the daily snapshot schedule")
async def admin_set_schedule(request: Request):
    _guard(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    cron = (body or {}).get("cron")
    retention = (body or {}).get("retention_days")
    enabled = (body or {}).get("enabled", True)
    if not cron or retention is None:
        return JSONResponse({"detail": "cron and retention_days are required"}, status_code=400)
    try:
        return backup_service.set_schedule(cron=str(cron), retention_days=int(retention), enabled=bool(enabled))
    except backup_service.BackupError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@router.post("/admin/backup/schedule/toggle", summary="Enable/disable the snapshot schedule")
async def admin_toggle_schedule(request: Request):
    _guard(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    enabled = bool((body or {}).get("enabled", True))
    try:
        return backup_service.toggle_schedule(enabled)
    except backup_service.BackupError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
