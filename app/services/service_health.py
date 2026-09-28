"""
services/service_health.py
──────────────────────────
Liveliness + tier map for the whole GC platform, backing the admin console's Health tab.

Two honest signals, no more:

  • Liveliness  — a reachability probe. We open an HTTP request to the service's own port
    and treat ANY response (even a 401/400/404 from an auth-guarded actuator) as UP: it
    proves the connector is listening and the app is serving. A refused connection or a
    timeout is DOWN. We deliberately do NOT try to read the guarded /actuator/health body —
    that needs a platform token, and reachability is the strongest thing we can assert
    without impersonating a user.

  • Tiers       — the same architecture layering the Docs tab already names (Edge → Gateway
    → Platform services → Tool bridge → Agent), so the console tells one consistent story.

Restart is OPT-IN and ALLOW-LISTED. Each restartable service maps to a FIXED command (a
systemd unit or a supervisor program) that is run with an argument list — never a shell
string, never anything derived from the request body — behind the same actuator IP guard as
the rest of /admin. Disabled entirely unless ALLOW_SERVICE_RESTART is on. This keeps the
console from ever becoming an arbitrary-command surface.

Note on the gc-mw modules: gateway, authentication, administration, support, smarthub,
reporting, formulation and mcp all run inside ONE Tomcat (gc-mw.service), so a restart of
any of them restarts the whole middleware. The UI says so before it acts.
"""
from __future__ import annotations

import asyncio
import subprocess
import time

import httpx

from ..commons.config import cfg
from ..commons.flags import flags
from ..commons.logger import get_logger

log = get_logger(__name__)

# Host the platform services are reachable on from the agent box (they're co-located, so
# localhost by default; overridable for a split deploy).
_HOST = cfg.SERVICE_HEALTH_HOST

# ── the platform service registry ───────────────────────────────────────────────
# tier order here is the render order in the console. `unit` names the restart target
# (resolved through _RESTART_UNITS below); None = not restartable from the console.
_MW = "gc-mw"          # shared middleware Tomcat — all these appBases live in it
_SERVICES: list[dict] = [
    # key             name                    tier                  scheme  port    unit
    ("gateway",       "API Gateway",          "Gateway / Edge",     "https", 19010, _MW),
    ("authentication","Authentication",       "Platform services",  "http",  19020, _MW),
    ("administration","Administration",       "Platform services",  "http",  19030, _MW),
    ("support",       "Support",              "Platform services",  "http",  19040, _MW),
    ("smarthub",      "Smart Hub",            "Platform services",  "http",  19050, _MW),
    ("reporting",     "Reporting",            "Platform services",  "http",  19060, _MW),
    ("formulation",   "Formulation",          "Platform services",  "http",  19070, _MW),
    ("mcp",           "MCP tool bridge",      "Tool bridge",        "http",  19170, _MW),
]
_SERVICES = [
    {"key": k, "name": n, "tier": t, "scheme": s, "port": p, "unit": u}
    for (k, n, t, s, p, u) in _SERVICES
]

# Fixed, allow-listed restart commands. The ONLY commands the restart endpoint may run.
# gcusr has passwordless sudo for systemctl; the agent runs under supervisor, so it restarts
# itself through supervisorctl (its own dedicated NOPASSWD entry) rather than systemd.
_RESTART_UNITS: dict[str, list[str]] = {
    "gc-mw":       ["sudo", "-n", "systemctl", "restart", "gc-mw.service"],
    "opensearch":  ["sudo", "-n", "systemctl", "restart", "gc-opensearch.service"],
    "ollama":      ["sudo", "-n", "systemctl", "restart", "ollama.service"],
    "agent":       ["sudo", "-n", "/apps/supervisor/bin/supervisorctl", "restart", "ai-agent-service"],
}
# A restart of the shared middleware bounces every module in it — surfaced as a warning.
_SHARED_UNIT_NOTE = {
    "gc-mw": "Restarts the shared middleware Tomcat — every platform service (gateway, "
             "administration, smarthub, reporting, formulation, mcp …) bounces together.",
    "agent": "Restarts the agent itself — this console will briefly disconnect, then recover.",
}


async def _probe(client: httpx.AsyncClient, svc: dict) -> dict:
    """Reachability probe for one service. Any HTTP status = UP; connect error/timeout = DOWN."""
    url = f"{svc['scheme']}://{_HOST}:{svc['port']}/"
    started = time.perf_counter()
    try:
        resp = await client.get(url)
        ms = round((time.perf_counter() - started) * 1000, 1)
        return {"status": "UP", "http": resp.status_code, "latency_ms": ms, "probe_url": url}
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        ms = round((time.perf_counter() - started) * 1000, 1)
        return {"status": "DOWN", "http": None, "latency_ms": ms, "probe_url": url,
                "error": type(exc).__name__}
    except httpx.HTTPError as exc:
        # A read/timeout AFTER connecting still means the port accepted us → the connector
        # is up but slow/unresponsive. Report DEGRADED rather than a hard DOWN.
        ms = round((time.perf_counter() - started) * 1000, 1)
        return {"status": "DEGRADED", "http": None, "latency_ms": ms, "probe_url": url,
                "error": type(exc).__name__}


async def snapshot() -> dict:
    """Probe every platform service concurrently plus fold in the agent's own health
    (self + store + llm from the actuator), grouped by tier for the console."""
    async with httpx.AsyncClient(timeout=cfg.SERVICE_PROBE_TIMEOUT, verify=False,
                                 follow_redirects=False) as client:
        results = await asyncio.gather(*[_probe(client, s) for s in _SERVICES])

    by_tier: dict[str, list] = {}
    for svc, res in zip(_SERVICES, results):
        unit = svc["unit"]
        by_tier.setdefault(svc["tier"], []).append({
            "key": svc["key"], "name": svc["name"], "port": svc["port"], "scheme": svc["scheme"],
            **res,
            "restart_unit": unit,
            "restartable": bool(unit) and flags.service_restart_enabled and unit in _RESTART_UNITS,
            "restart_note": _SHARED_UNIT_NOTE.get(unit or ""),
        })

    # Agent tier — the agent process (serving this request, so UP) plus its own dependencies.
    agent_tier = await _agent_tier()
    tiers = [{"tier": t, "services": by_tier[t]} for t in
             ["Gateway / Edge", "Platform services", "Tool bridge"] if t in by_tier]
    tiers.append(agent_tier)

    total = sum(len(t["services"]) for t in tiers)
    down = sum(1 for t in tiers for s in t["services"] if s["status"] == "DOWN")
    degraded = sum(1 for t in tiers for s in t["services"] if s["status"] == "DEGRADED")
    import datetime as _dt
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": total, "up": total - down - degraded, "down": down, "degraded": degraded,
        "restart_enabled": flags.service_restart_enabled,
        "tiers": tiers,
    }


async def _agent_tier() -> dict:
    """The agent + its dependency components (store, llm) from the actuator health body."""
    services: list[dict] = [{
        "key": "agent", "name": "GC Agent (this service)", "port": cfg.PORT, "scheme": "http",
        "status": "UP", "http": 200, "latency_ms": 0.0, "probe_url": "in-process",
        "restart_unit": "agent",
        "restartable": flags.service_restart_enabled and "agent" in _RESTART_UNITS,
        "restart_note": _SHARED_UNIT_NOTE.get("agent"),
    }]
    try:
        from ..routers.health import _components
        comps = await _components()
    except Exception as exc:  # noqa: BLE001
        comps = {}
        log.bind(func="service_health").warning(f"agent components probe failed: {exc}")
    _dep = {"store": ("OpenSearch store", "opensearch"), "llm": ("LLM (Ollama)", "ollama"),
            "mcp": (None, None)}   # mcp already listed as a platform service; skip the dup
    for ck, (label, unit) in _dep.items():
        if label is None or ck not in comps:
            continue
        st = str(comps[ck].get("status", "UNKNOWN")).upper()
        st = "UP" if st == "UP" else ("DEGRADED" if st == "DEGRADED" else "DOWN")
        services.append({
            "key": ck, "name": label, "port": None, "scheme": None,
            "status": st, "http": None, "latency_ms": None,
            "probe_url": "actuator", "details": comps[ck].get("details") or {},
            "restart_unit": unit,
            "restartable": bool(unit) and flags.service_restart_enabled and unit in _RESTART_UNITS,
            "restart_note": None,
        })
    return {"tier": "Agent", "services": services}


class RestartError(Exception):
    pass


def restart(key: str) -> dict:
    """Restart the service `key` maps to. Allow-listed, non-shell, guarded by a feature flag.
    The agent's own unit is fired detached (it kills this process) so the HTTP 200 flushes
    first; every other unit is run synchronously and its result reported."""
    if not flags.service_restart_enabled:
        raise RestartError("service restart is disabled (set ALLOW_SERVICE_RESTART=1 to enable)")
    # Resolve key → unit. Accept either a service key (e.g. 'administration') or a unit id.
    unit = None
    if key in _RESTART_UNITS:
        unit = key
    else:
        for s in _SERVICES:
            if s["key"] == key:
                unit = s["unit"]
                break
        if unit is None and key == "agent":
            unit = "agent"
    if not unit or unit not in _RESTART_UNITS:
        raise RestartError(f"{key!r} is not a restartable service")
    cmd = _RESTART_UNITS[unit]
    log.bind(func="service_health", event="service_restart", unit=unit, requested=key).warning(
        f"admin console requested restart of {unit}")

    if unit == "agent":
        # Detached — supervisorctl will stop/start this very process; don't wait on it.
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            raise RestartError(f"failed to launch restart: {exc}") from exc
        return {"unit": unit, "requested": key, "status": "restarting",
                "detail": "agent restart issued; this console will reconnect shortly"}

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired as exc:
        raise RestartError(f"restart of {unit} timed out after 90s") from exc
    except Exception as exc:  # noqa: BLE001
        raise RestartError(f"restart of {unit} failed: {exc}") from exc
    ok = proc.returncode == 0
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not ok:
        raise RestartError(f"restart of {unit} exited {proc.returncode}: {err or out or 'no output'}")
    return {"unit": unit, "requested": key, "status": "restarted", "rc": proc.returncode,
            "detail": out or f"{unit} restarted"}
