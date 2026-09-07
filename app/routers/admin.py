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

from ..commons.logger import get_logger, recent_logs
from ..routers.health import _guard  # reuse the actuator IP allow-list guard
from ..services import backup_service, runtime_config, system_metrics

log = get_logger(__name__)

router = APIRouter(tags=["admin"])

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_ADMIN_HTML = _STATIC_DIR / "admin.html"
_ADMIN_JS = _STATIC_DIR / "admin.js"


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    _guard(request)
    if _ADMIN_HTML.exists():
        return FileResponse(str(_ADMIN_HTML))
    return JSONResponse({"detail": "admin UI not found"}, status_code=404)


@router.get("/admin.js", include_in_schema=False)
async def admin_js(request: Request):
    # Served here (not from /static) so it shares the /admin IP-guard and stays
    # RBAC-exempt; also lets the strict CSP keep script-src 'self' (no inline).
    _guard(request)
    if _ADMIN_JS.exists():
        return FileResponse(str(_ADMIN_JS), media_type="application/javascript")
    return JSONResponse({"detail": "admin.js not found"}, status_code=404)


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


# ── per-application balloon overrides (admin-editable widget suggestions) ────────
@router.get("/admin/balloons", summary="Applications + their pinned balloon overrides")
async def admin_get_balloons(request: Request):
    _guard(request)
    from .platform import _forward_headers
    from ..services import ecosystem as eco, balloon_store
    apps = await eco.applications(_forward_headers(request))
    return {
        "applications": [
            {"code": a["code"], "name": a["name"], "available": eco.is_available(a),
             "category": a["category"]}
            for a in apps
        ],
        "overrides": balloon_store.all_overrides(),
    }


@router.get("/admin/balloons/{app}/preview", summary="Agent-generated balloons for an application")
async def admin_preview_balloons(request: Request, app: str):
    _guard(request)
    from .platform import _forward_headers
    from ..services.suggestions import module_suggestions
    return {"app": app.upper(), "balloons": await module_suggestions(app, _forward_headers(request))}


@router.post("/admin/balloons", summary="Pin balloon overrides for an application")
async def admin_set_balloons(request: Request):
    _guard(request)
    from ..services import balloon_store
    body = await request.json()
    app = str((body or {}).get("app") or (body or {}).get("code") or "").strip()
    if not app:
        return JSONResponse({"error": "application code required (field 'app')"}, status_code=400)
    saved = balloon_store.set_overrides(app, (body or {}).get("balloons"))
    return {"app": app.upper(), "balloons": saved, "overrides": balloon_store.all_overrides()}


@router.delete("/admin/balloons/{app}", summary="Clear an application's balloon override")
async def admin_delete_balloons(request: Request, app: str):
    _guard(request)
    from ..services import balloon_store
    balloon_store.delete_overrides(app)
    return {"app": app.upper(), "overrides": balloon_store.all_overrides()}


@router.get("/admin/logs", summary="Recent in-memory logs (turns, prompts, errors)")
async def admin_logs(request: Request):
    _guard(request)
    limit = int(request.query_params.get("limit", "150"))
    event = request.query_params.get("event") or None
    level = request.query_params.get("level") or None
    return {"logs": recent_logs(limit=limit, event=event, level=level)}


@router.get("/admin/system", summary="Host CPU/GPU/memory/disk + LLM device placement")
async def admin_system(request: Request):
    _guard(request)
    import asyncio
    # snapshot() shells out to nvidia-smi + reads /proc + hits Ollama — off the event loop.
    return await asyncio.to_thread(system_metrics.snapshot)


@router.get("/admin/turns", summary="Recent chat turns: prompt, answer, metrics, errors")
async def admin_turns(request: Request):
    """Correlates the in-memory log ring by request_id into one record per chat turn —
    prompt, answer snippet, latency/token/tool metrics, and any errors — for live
    performance + response-validity analysis without shell access."""
    _guard(request)
    limit = int(request.query_params.get("limit", "40"))
    return {"turns": _recent_turns(limit)}


@router.get("/admin/inference", summary="Harness-vs-inference mix + inference performance")
async def admin_inference(request: Request):
    """Aggregate the recent turns into the three levers: how many questions the HARNESS
    answered deterministically (no GPU) vs INFERENCE (an LLM call), the model's throughput
    (tokens/sec, llm latency), and what SHAPING was applied (grounding, skill-pinned)."""
    _guard(request)
    limit = int(request.query_params.get("limit", "200"))
    turns = _recent_turns(limit)
    total = len(turns)
    by_source: dict[str, int] = {}
    tok_rates: list[float] = []
    llm_mss: list[float] = []
    grounded = pinned = inference = 0
    for t in turns:
        src = t.get("answered_by") or ("inference" if t.get("llm_ms") else "unknown")
        by_source[src] = by_source.get(src, 0) + 1
        if src == "inference":
            inference += 1
            if t.get("tok_per_s"):
                tok_rates.append(float(t["tok_per_s"]))
            if t.get("llm_ms"):
                llm_mss.append(float(t["llm_ms"]))
            if t.get("grounded"):
                grounded += 1
            if t.get("skill"):
                pinned += 1
    harness = total - inference
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0.0  # noqa: E731
    return {
        "total": total,
        "harness": harness,
        "inference": inference,
        "harness_pct": round(100 * harness / total, 1) if total else 0.0,
        "by_source": by_source,
        "inference_perf": {
            "avg_tokens_per_sec": avg(tok_rates),
            "avg_llm_ms": avg(llm_mss),
        },
        "shaping": {
            "grounded_pct": round(100 * grounded / inference, 1) if inference else 0.0,
            "skill_pinned_pct": round(100 * pinned / inference, 1) if inference else 0.0,
        },
    }


def _recent_turns(limit: int = 40) -> list[dict]:
    turns: dict[str, dict] = {}
    order: list[str] = []
    for r in recent_logs(limit=800):          # newest-first
        f = r.get("fields") or {}
        rid = f.get("request_id")
        if not rid:
            continue
        t = turns.get(rid)
        if t is None:
            t = {"request_id": rid, "ts": r.get("ts"), "errors": []}
            turns[rid] = t
            order.append(rid)
        ev = f.get("event")
        if ev == "chat_prompt":
            t.update({"question": f.get("question"), "session_id": f.get("session_id"),
                      "user_id": f.get("user_id"), "mode": f.get("mode"), "ts": r.get("ts")})
        elif ev == "turn_summary":
            t.update({"total_ms": f.get("total_ms"), "llm_ms": f.get("llm_ms"),
                      "tools_ms": f.get("tools_ms"), "retrieval_ms": f.get("retrieval_ms"),
                      "tokens_in": f.get("prompt_tokens"), "tokens_out": f.get("completion_tokens"),
                      "iterations": f.get("iterations"), "tools": f.get("tools_used"),
                      "outcome": f.get("outcome"),
                      "answered_by": f.get("answered_by"), "tok_per_s": f.get("tok_per_s"),
                      "skill": f.get("skill"), "grounded": f.get("grounded")})
        elif ev == "turn_source":                 # non-streaming turns tag their source here
            t.setdefault("answered_by", f.get("answered_by"))
            if f.get("question"):
                t.setdefault("question", f.get("question"))
            if f.get("answer"):
                t.setdefault("answer", f.get("answer"))
            if f.get("skill"):
                t.setdefault("skill", f.get("skill"))
            if f.get("model"):
                t.setdefault("model", f.get("model"))
        elif ev == "chat_answer":
            t.update({"answer": f.get("answer"), "blocks": f.get("blocks")})
            if f.get("error"):
                t["errors"].append(str(f.get("error")))
        elif ev == "tool_error" and r.get("msg"):
            t["errors"].append(r.get("msg"))          # HTTP-200 envelope business error
        if r.get("level") == "ERROR" and r.get("msg"):
            t["errors"].append(r.get("msg"))
    # Only real chat turns (a captured prompt or answer) — drop bare health/admin request ids.
    out = [turns[rid] for rid in order if turns[rid].get("question") or turns[rid].get("answer")]
    return out[:limit]


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
