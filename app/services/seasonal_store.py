"""
services/seasonal_store.py
──────────────────────────
Admin-editable, persisted SEASON definitions for the widget's seasonal workflow hints. Adding or
removing a season (and editing its app / active months / hints) is pure DATA from /admin — no code
change. Mirrors services/balloon_store.py:
  • OpenSearch KV (redis-compatible) — survives restarts + git deploys.
  • A flat JSON file (gitignored) — human-inspectable, hand-editable, wins over KV.
Forgiving throughout: any backend hiccup degrades to the built-in federal-calendar seed, never raises.

A season is: {"name", "app" (code or None=any), "months" ("3-10"/"11-2"/"3,4,5"), "enabled" (bool),
"hints": [{"label","send"}]}. `send` MUST be a phrasing the flow trigger recognises so a tap opens
the REAL guided flow.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from ..commons.logger import get_logger
from ..connections import redis_get_json, redis_set_json, redis_delete

log = get_logger(__name__)

_KV_KEY = "admin:seasonal_seasons"
_FILE = os.environ.get("SEASONS_FILE", "seasonal_seasons.json")
_MAX_SEASONS = 24
_MAX_HINTS = 6
_TTL = 20.0

# Built-in seed: the gc360 Ecosystem Module Activation Calendar — the federal budget lifecycle mapped
# to the platform's applications (Antideficiency Act / OMB Circular A-11 framing). Source: the gc360
# federal budget lifecycle PPTX (swimlane Gantt + month×module matrix). Fiscal year runs Oct 1 – Sep 30.
# `months` are the window (calendar-month numbers, wrap-aware) when each module's workflow is the
# priority, so a hint surfaces at the right time; windows are TUNABLE from /admin. Each hint's `send`
# is either a real guided action (Formulation baseline, Reporting user-access report) or a natural ask
# the agent answers conversationally. Used when the store is empty; an admin replaces/extends it.
# Only the 6 modules backed by a live application are seeded (CPIC + Acquisition have no app yet).
_FEDERAL_CALENDAR: list[dict[str, Any]] = [
    {   # Budget development · CJ · Congressional liaison — budget call → OMB → President's Budget
        "name": "Formulation", "app": "FORMULATION", "months": "10-5", "enabled": True,
        "hints": [
            {"label": "🗓️ Generate a formulation baseline", "send": "generate a formulation baseline"},
            {"label": "📣 Set up a data call", "send": "set up a data call"},
        ],
    },
    {   # Obligations · Expenditures · Apportionment — full-FY execution, ADA compliance monitoring
        "name": "Execution", "app": "EXECUTION", "months": "1-12", "enabled": True,
        "hints": [
            {"label": "📊 Check obligations & expenditures", "send": "what is the obligation and expenditure status this quarter?"},
        ],
    },
    {   # Fund allocation · Sub-allotments · Mid-year realignment
        "name": "Allocation Planner", "app": "CRP", "months": "10-6", "enabled": True,
        "hints": [
            {"label": "💰 Review fund allocations", "send": "show current fund allocations and any mid-year realignment"},
        ],
    },
    {   # WCF rate development · IAA pricing · Customer negotiations
        "name": "Cost Model", "app": "COST_MODEL", "months": "10-6", "enabled": True,
        "hints": [
            {"label": "📐 Review WCF rates & IAA pricing", "send": "show the WCF rate model and IAA pricing status"},
        ],
    },
    {   # Dashboards · KPIs · Transparency & audit reporting — continuous
        "name": "Analytics & Reporting", "app": "REPORTING", "months": "1-12", "enabled": True,
        "hints": [
            {"label": "📈 Generate a user access report", "send": "generate a user access report"},
        ],
    },
    {   # Positions · Hiring plans · Onboarding — annual workforce cycle
        "name": "Workforce Planning", "app": "PEOPLE_PLANNER", "months": "1-12", "enabled": True,
        "hints": [
            {"label": "👥 Review workforce & hiring plan", "send": "show the workforce planning and hiring status"},
        ],
    },
]

_cache: list[dict[str, Any]] | None = None
_cache_exp: float = 0.0


def _now() -> float:
    try:
        return time.monotonic()
    except Exception:  # noqa: BLE001
        return time.time()


def _clean_season(s: Any) -> dict[str, Any] | None:
    """Coerce arbitrary input into a safe season dict, or None if it isn't usable."""
    if not isinstance(s, dict):
        return None
    name = str(s.get("name") or "").strip()[:40]
    app_raw = s.get("app")
    app = None if app_raw in (None, "", "any", "ANY", "Any") else str(app_raw).strip().upper()[:40]
    months = str(s.get("months") or "").strip()[:60]
    en = s.get("enabled", True)
    enabled = en if isinstance(en, bool) else str(en).strip().lower() in ("1", "true", "yes", "on")
    hints: list[dict[str, str]] = []
    for h in (s.get("hints") or []):
        if not isinstance(h, dict):
            continue
        lbl = str(h.get("label") or "").strip()[:60]
        snd = str(h.get("send") or h.get("message") or lbl).strip()[:200]
        if lbl and snd:
            hints.append({"label": lbl, "send": snd})
        if len(hints) >= _MAX_HINTS:
            break
    if name and months and hints:
        return {"name": name, "app": app, "months": months, "enabled": enabled, "hints": hints}
    return None


def _clean_list(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in (raw if isinstance(raw, list) else []):
        c = _clean_season(s)
        if c:
            out.append(c)
        if len(out) >= _MAX_SEASONS:
            break
    return out


def _read_file() -> list[dict[str, Any]] | None:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f)
            cleaned = _clean_list(data)
            return cleaned or None
    except Exception as exc:  # noqa: BLE001
        log.warning(f"seasons file read failed: {exc}")
    return None


def _write_file(seasons: list[dict[str, Any]]) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(seasons, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"seasons file write failed: {exc}")


def federal_calendar() -> list[dict[str, Any]]:
    """The built-in federal-calendar seed (a fresh copy)."""
    return _clean_list(_FEDERAL_CALENDAR)


def get_seasons() -> list[dict[str, Any]]:
    """Admin-defined seasons: file override → KV → built-in federal-calendar seed. Cached briefly."""
    global _cache, _cache_exp
    if _cache is not None and _now() < _cache_exp:
        return _cache
    seasons = _read_file()
    if seasons is None:
        try:
            seasons = _clean_list(redis_get_json(_KV_KEY)) or None
        except Exception as exc:  # noqa: BLE001
            log.warning(f"seasons KV read failed: {exc}")
            seasons = None
    if not seasons:
        seasons = federal_calendar()
    _cache = seasons
    _cache_exp = _now() + _TTL
    return seasons


def set_seasons(raw: Any) -> list[dict[str, Any]]:
    """Validate + persist the full season list (KV + file). Returns the cleaned list."""
    global _cache, _cache_exp
    seasons = _clean_list(raw)
    try:
        redis_set_json(_KV_KEY, seasons)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"seasons KV write failed: {exc}")
    _write_file(seasons)
    _cache = seasons
    _cache_exp = _now() + _TTL
    log.bind(func="seasonal_store").info(f"seasons updated: {[s['name'] for s in seasons]}")
    return seasons


def reset() -> list[dict[str, Any]]:
    """Clear overrides → back to the built-in federal-calendar seed."""
    global _cache, _cache_exp
    try:
        redis_delete(_KV_KEY)
    except Exception:  # noqa: BLE001
        pass
    try:
        if os.path.exists(_FILE):
            os.remove(_FILE)
    except Exception:  # noqa: BLE001
        pass
    _cache = None
    _cache_exp = 0.0
    return get_seasons()
