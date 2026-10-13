"""
services/seasonal.py
────────────────────
Seasonal workflow hints. During the relevant part of the budget cycle, promote the workflows that
matter then — e.g. baseline generation + data calls during the formulation season — as high-priority
suggestion balloons in the widget. Each hint's `send` is the exact TAUGHT phrasing the trigger
recognises (see flows._BASELINE_INTENT_RE / maybe_start), so tapping a seasonal hint opens the REAL
guided flow rather than a fake intro.

Seasons are DATA, not code: the list (name / app / active months / hints / enabled) lives in
services/seasonal_store.py and is fully editable from /admin — adding or removing a season, or
loading the federal-calendar seed, needs no deploy. A master on/off toggle (SEASONAL_HINTS_ENABLED)
lives in runtime_config. The current month + current application decide which hints fire. Returns
ready balloon dicts ({"label","send"}).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import seasonal_store

ENABLED_KEY = "SEASONAL_HINTS_ENABLED"


def _parse_months(spec: str) -> set[int]:
    """Parse an 'active months' string → a set of month numbers (1-12). Supports comma-separated
    values and ranges, including year-end wrap ('11-2' → Nov,Dec,Jan,Feb). Bad tokens are ignored."""
    out: set[int] = set()
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                out.update(range(a, b + 1) if a <= b else list(range(a, 13)) + list(range(1, b + 1)))
            else:
                out.add(int(part))
        except ValueError:
            continue
    return {m for m in out if 1 <= m <= 12}


def _enabled() -> bool:
    try:
        from . import runtime_config
        return runtime_config.get_bool(ENABLED_KEY)
    except Exception:  # noqa: BLE001 — never break the bootstrap on a config read
        return True


def _matches(season: dict[str, Any], app_code: str, month: int) -> bool:
    if not season.get("enabled", True):
        return False
    app = season.get("app")
    if app is not None and str(app).strip().upper() != app_code:
        return False
    return month in _parse_months(season.get("months", ""))


def tunable_specs() -> list[dict[str, Any]]:
    """The one scalar tunable for runtime_config/admin: the master on/off. The season DEFINITIONS
    (add/remove/edit) are data-driven via seasonal_store + the /admin/seasons endpoints."""
    return [{
        "key": ENABLED_KEY, "label": "Calendar hints", "group": "Calendar", "type": "bool",
        "default": True,
        "description": "Lead the widget suggestions with calendar-relevant workflows during the "
                       "budget cycle. Edit the calendar entries themselves under the Calendar tab.",
    }]


def current_season(app_code: str | None, when: datetime | None = None) -> str | None:
    """Name of the active season for `app_code` at `when` (default: now, UTC), or None."""
    if not _enabled():
        return None
    code = (app_code or "").strip().upper()
    month = (when or datetime.now(timezone.utc)).month
    for s in seasonal_store.get_seasons():
        if _matches(s, code, month):
            return s.get("name")
    return None


def active_seasons(when: datetime | None = None) -> list[str]:
    """Names of every season whose window is open at `when` (default: now, UTC), IGNORING which
    application is in context — for the /admin indicator ('what's in season right now')."""
    if not _enabled():
        return []
    month = (when or datetime.now(timezone.utc)).month
    return [s.get("name") for s in seasonal_store.get_seasons()
            if s.get("enabled", True) and month in _parse_months(s.get("months", ""))]


def seasonal_hints(app_code: str | None, when: datetime | None = None) -> list[dict[str, str]]:
    """High-priority balloons for the current season + application, or [] when out of season / off.

    `when` defaults to now (UTC). Each returned dict is a ready balloon: {"label", "send"}.
    """
    if not _enabled():
        return []
    code = (app_code or "").strip().upper()
    month = (when or datetime.now(timezone.utc)).month
    out: list[dict[str, str]] = []
    for s in seasonal_store.get_seasons():
        if _matches(s, code, month):
            out.extend({"label": h["label"], "send": h["send"]} for h in s.get("hints", []))
    return out
