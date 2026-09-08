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


# No-store so a deploy's new admin UI shows immediately (the console must never be a stale
# cached copy after we ship a change).
_NOCACHE = {"Cache-Control": "no-store, must-revalidate"}


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    _guard(request)
    if _ADMIN_HTML.exists():
        return FileResponse(str(_ADMIN_HTML), headers=_NOCACHE)
    return JSONResponse({"detail": "admin UI not found"}, status_code=404)


@router.get("/admin.js", include_in_schema=False)
async def admin_js(request: Request):
    # Served here (not from /static) so it shares the /admin IP-guard and stays
    # RBAC-exempt; also lets the strict CSP keep script-src 'self' (no inline).
    _guard(request)
    if _ADMIN_JS.exists():
        return FileResponse(str(_ADMIN_JS), media_type="application/javascript", headers=_NOCACHE)
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


def _parse_ua(ua: str) -> tuple[str, str]:
    """Rough browser + OS from a User-Agent — enough to spot an unexpected client."""
    u = (ua or "").lower()
    if not u:
        return "", ""
    browser = ("curl/script" if ("curl" in u or "python-" in u or "httpx" in u or "wget" in u)
               else "Edge" if "edg" in u
               else "Chrome" if ("chrome" in u or "crios" in u)
               else "Firefox" if "firefox" in u
               else "Safari" if "safari" in u
               else "Unknown")
    osn = ("Windows" if "windows" in u
           else "iOS" if ("iphone" in u or "ipad" in u)
           else "macOS" if ("mac os" in u or "macintosh" in u)
           else "Android" if "android" in u
           else "Linux" if "linux" in u
           else "Unknown")
    return browser, osn


# The origins we expect the agent to be reached from; anything else is flagged for review.
_EXPECTED_ORIGIN = "proounce"


def _audit_flags(t: dict) -> list[str]:
    """Cheap intrusion signals: an off-domain origin, a scripted/absent client, or errors."""
    flags: list[str] = []
    origin = str(t.get("origin") or "")
    ua = str(t.get("user_agent") or "")
    if origin and _EXPECTED_ORIGIN not in origin.lower():
        flags.append("off-domain origin")
    if ua and any(s in ua.lower() for s in ("curl", "python-", "httpx", "wget")):
        flags.append("non-browser client")
    if t.get("errors"):
        flags.append("errors")
    return flags


@router.get("/admin/audit", summary="Audit trail — who did what, when, from where (FedRAMP)")
async def admin_audit(request: Request):
    """A governance/audit view over recent turns: one row per request with the actor
    (username / user id), source IP, session + trace ids, the application & role in effect,
    the question, the tools/mutations invoked, the outcome, and any errors. Optional filters
    ?user=, ?app=, ?mutations_only=1. Backed by the persisted (restart-durable) metrics trail."""
    _guard(request)
    limit = int(request.query_params.get("limit", "200"))
    fuser = (request.query_params.get("user") or "").strip().lower()
    fapp = (request.query_params.get("app") or "").strip().lower()
    muts_only = request.query_params.get("mutations_only") in ("1", "true", "yes")
    susp_only = request.query_params.get("suspicious_only") in ("1", "true", "yes")
    from ..mcp.tool_registry import is_mutation
    rows = []
    for t in _recent_turns(limit):
        tools = t.get("tools") or []
        muts = [x for x in tools if is_mutation(x)]
        if muts_only and not muts:
            continue
        if fuser and fuser not in str(t.get("user_name") or t.get("user_id") or "").lower():
            continue
        if fapp and fapp not in str(t.get("app") or "").lower():
            continue
        flags = _audit_flags(t)
        if susp_only and not flags:
            continue
        browser, osn = _parse_ua(t.get("user_agent") or "")
        rows.append({
            "ts": t.get("ts"),
            "user": t.get("user_name") or t.get("user_id"),
            "user_id": t.get("user_id"),
            "client_ip": t.get("client_ip"),
            "user_agent": t.get("user_agent"),
            "browser": browser,
            "os": osn,
            "origin": t.get("origin"),
            "flags": flags,
            "session_id": t.get("session_id"),
            "trace_id": t.get("trace_id"),
            "request_id": t.get("request_id"),
            "app": t.get("app"),
            "role": t.get("role"),
            "answered_by": t.get("answered_by"),
            "question": t.get("question"),
            "tools": tools,
            "mutations": muts,
            "outcome": t.get("outcome"),
            "errors": t.get("errors") or [],
        })
    return {"count": len(rows), "entries": rows}


@router.get("/admin/docs", summary="Live capability + architecture reference (from code)")
async def admin_docs(request: Request):
    """A self-documenting reference for the admin console: the agent's capabilities (skills,
    deterministic Q&A intents, guided flows) generated from the live code, plus the layered
    architecture. Always current — it reflects what's actually registered right now."""
    _guard(request)
    from ..services import skills as _skills
    from ..mcp.tool_registry import is_mutation
    from ..services.skill_store import _read_file as _custom_file
    custom = {d.get("name") for d in _custom_file() if isinstance(d, dict)}
    skills_doc = [{
        "name": s.name,
        "summary": s.summary or s.name,
        "examples": list(s.keywords[:6]),
        "needs": list(s.required),
        "tool": s.tool,
        "mutation": is_mutation(s.tool),
        "custom": s.name in custom,
    } for s in _skills.SKILLS]
    intents = [
        ["List applications", "“list applications”, “which planners exist”"],
        ["About an application", "“about Formulation”, “what does this app do”"],
        ["Roles in an application", "“roles in Formulation”"],
        ["Who can access an application", "“who can access Formulation”"],
        ["My access", "“my access”, “what can I get into”"],
        ["A user's access", "“what can GCADMIN do”, “access for jsmith”"],
    ]
    flows = [
        ["Create a user", "guided intake → assign apps & roles"],
        ["Set up a data call", "create → attendees → reminder"],
        ["Onboard access", "assign application + role, one at a time"],
        ["Create a skill", "teach the agent a new action at runtime"],
    ]
    architecture = [
        {"layer": "Client", "detail": "GC360 shell + agent widget (X-Selected-App)"},
        {"layer": "Edge", "detail": "Apache gc.conf — /api→gateway, /gc-agent→agent (ops)"},
        {"layer": "Gateway", "detail": "Spring Cloud Gateway :19010 — agent route /reply"},
        {"layer": "Agent", "detail": "FastAPI :17024 — harness (no GPU) first, else inference (qwen2.5:14b GPU)"},
        {"layer": "Tool bridge", "detail": "MCP service :19170 — 1,176 tools"},
        {"layer": "Middleware", "detail": "gc-mw.service — one Tomcat, an appBase per application"},
        {"layer": "Data", "detail": "Oracle DEV_COMPASS"},
    ]
    return {
        "skills": skills_doc,
        "intents": intents,
        "flows": flows,
        "architecture": architecture,
        "diagram_url": "https://claude.ai/code/artifact/b2514d32-4632-40f5-8fbc-9fd666a26aca",
    }


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
    by_app: dict[str, int] = {}
    by_role: dict[str, int] = {}
    tok_rates: list[float] = []
    llm_mss: list[float] = []
    grounded = pinned = inference = 0
    for t in turns:
        src = t.get("answered_by") or ("inference" if t.get("llm_ms") else "unknown")
        by_source[src] = by_source.get(src, 0) + 1
        if t.get("app"):
            by_app[str(t["app"])] = by_app.get(str(t["app"]), 0) + 1
        if t.get("role"):
            by_role[str(t["role"])] = by_role.get(str(t["role"]), 0) + 1
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
        "by_app": by_app,
        "by_role": by_role,
        "inference_perf": {
            "avg_tokens_per_sec": avg(tok_rates),
            "avg_llm_ms": avg(llm_mss),
        },
        "shaping": {
            "grounded_pct": round(100 * grounded / inference, 1) if inference else 0.0,
            "skill_pinned_pct": round(100 * pinned / inference, 1) if inference else 0.0,
        },
    }


@router.get("/admin/turn/{request_id}", summary="One turn's full workflow timeline (replay/live)")
async def admin_turn_timeline(request: Request, request_id: str):
    """Reconstruct a single turn's end-to-end path from the log ring — the ordered steps the
    request flowed through (route → retrieval → each LLM call → each tool → answer) with the
    stats on each — so the UI can draw its workflow live (poll while in-flight) or replay."""
    _guard(request)
    return _turn_timeline(request_id)


def _turn_timeline(rid: str) -> dict:
    """Ordered workflow nodes for one request_id, assembled from the log ring: the request,
    the routing decision, then the execution steps in time order (RAG retrieval, each LLM
    call, and EVERY MCP tool call — harness handlers call tools directly, so their tool steps
    come from the mcp `execute_tool` completion logs, not turn.tool()), then the answer."""
    recs = [r for r in recent_logs(limit=2000) if (r.get("fields") or {}).get("request_id") == rid]
    recs.reverse()  # oldest-first
    meta: dict = {"request_id": rid, "done": False}
    prompt_step = route_step = None
    exec_steps: list[dict] = []      # retrieval / llm / tool, in execution (timestamp) order
    answer_text = None
    for r in recs:
        f = r.get("fields") or {}
        ev, ts, msg = f.get("event"), r.get("ts"), (r.get("msg") or "")
        for k in ("user_name", "user_id", "client_ip", "trace_id", "session_id", "app", "role", "user_agent", "origin"):
            if f.get(k) and not meta.get(k):
                meta[k] = f.get(k)
        if ev == "chat_prompt":
            meta.update({"question": f.get("question"), "session_id": f.get("session_id"),
                         "user_id": f.get("user_id"), "mode": f.get("mode"), "ts": ts})
            prompt_step = {"kind": "prompt", "label": "Request", "ts": ts,
                           "detail": f"{f.get('tool_count', 0)} tools retrieved"}
        elif ev == "turn_source":
            meta["answered_by"] = f.get("answered_by")
            meta.setdefault("question", f.get("question"))
            if f.get("answer"):
                answer_text = f.get("answer")
            route_step = _route_step(f.get("answered_by"), ts)
        elif ev == "turn_step" and f.get("step") == "retrieval":
            exec_steps.append({"kind": "retrieval", "label": "Tool retrieval", "ts": ts,
                               "ms": f.get("phase_ms"), "detail": "RAG tool selection"})
        elif ev == "turn_step" and f.get("step") == "llm":
            exec_steps.append({"kind": "llm", "label": f"LLM call #{f.get('iteration', '')}".strip(),
                               "ts": ts, "ms": f.get("call_ms"),
                               "tokens_in": f.get("prompt_tokens"), "tokens_out": f.get("completion_tokens"),
                               "detail": f"{f.get('prompt_tokens', 0)}→{f.get('completion_tokens', 0)} tok"})
        elif f.get("func") == "execute_tool" and "completed in" in msg:
            exec_steps.append({"kind": "tool", "label": f.get("tool") or "tool", "ts": ts,
                               "ms": f.get("duration_ms"), "detail": "MCP tool call"})
        elif ev == "turn_summary":
            meta.update({"done": True, "answered_by": f.get("answered_by") or meta.get("answered_by"),
                         "total_ms": f.get("total_ms"), "llm_ms": f.get("llm_ms"),
                         "tools_ms": f.get("tools_ms"), "retrieval_ms": f.get("retrieval_ms"),
                         "tokens_in": f.get("prompt_tokens"), "tokens_out": f.get("completion_tokens"),
                         "tok_per_s": f.get("tok_per_s"), "iterations": f.get("iterations"),
                         "outcome": f.get("outcome"), "skill": f.get("skill"), "grounded": f.get("grounded")})
        elif ev == "chat_answer":
            meta.update({"done": True})
            answer_text = f.get("answer")
    if route_step is None and meta.get("answered_by"):   # stream inference has no turn_source
        route_step = _route_step(meta.get("answered_by"), meta.get("ts"))
    steps = [s for s in (prompt_step, route_step) if s] + exec_steps
    if answer_text is not None:
        meta["answer"] = answer_text
        meta["done"] = True
        steps.append({"kind": "answer", "label": "Answer", "ts": None, "detail": (answer_text or "")[:120]})
    meta["steps"] = steps
    return meta


def _route_step(answered_by: str | None, ts) -> dict:
    harness = answered_by not in (None, "inference")
    return {"kind": "route", "label": f"Route → {answered_by}", "ts": ts, "answered_by": answered_by,
            "detail": "answered by the harness (no model)" if harness else "handed to the model"}


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
        # Audit identity is stamped on every record — capture it from whichever arrives.
        for k in ("user_name", "user_id", "client_ip", "trace_id", "session_id", "app", "role", "user_agent", "origin"):
            if f.get(k) and not t.get(k):
                t[k] = f.get(k)
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
                      "skill": f.get("skill"), "grounded": f.get("grounded"),
                      "app": f.get("app"), "role": f.get("role")})
        elif ev == "turn_source":                 # non-streaming turns tag their source here
            t.setdefault("answered_by", f.get("answered_by"))
            if f.get("app"):
                t.setdefault("app", f.get("app"))
            if f.get("role"):
                t.setdefault("role", f.get("role"))
            if f.get("question"):
                t.setdefault("question", f.get("question"))
            if f.get("answer"):
                t.setdefault("answer", f.get("answer"))
            if f.get("skill"):
                t.setdefault("skill", f.get("skill"))
            if f.get("model"):
                t.setdefault("model", f.get("model"))
            if f.get("grounded") is not None:
                t.setdefault("grounded", f.get("grounded"))
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
