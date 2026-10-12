"""
services/seasonal.py
────────────────────
Seasonal workflow hints. During the relevant part of the budget cycle, promote the workflows that
matter then — e.g. baseline generation + data calls during the formulation season — as high-priority
suggestion balloons in the widget. Each hint's `send` is the exact TAUGHT phrasing the trigger
recognises (see flows._BASELINE_INTENT_RE / maybe_start), so tapping a seasonal hint opens the REAL
guided flow rather than a fake intro.

The season windows are ADMIN-TUNABLE from /admin (runtime_config): a master toggle plus a per-season
"active months" string that an admin can edit WITHOUT a restart. The code values here are the
fallback defaults. The current month + current application decide which hints fire. Returns ready
balloon dicts ({"label","send"}).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Master on/off + one "active months" tunable per season, surfaced in /admin via tunable_specs().
ENABLED_KEY = "SEASONAL_HINTS_ENABLED"


def _months_key(name: str) -> str:
    return f"SEASON_{name.strip().upper().replace(' ', '_')}_MONTHS"


# Each season: a default "active months" string (TUNABLE per season), the application code it applies
# to (None = any app), and the workflow hints to promote (label, send). `send` MUST be a phrasing the
# flow trigger recognises. Months accept "3-10" (range), "11-2" (wraps year-end), or "3,4,5" (list).
_SEASONS: list[dict[str, Any]] = [
    {
        "name": "Formulation",
        "months": "3-10",                       # Mar–Oct: agencies build next-year baselines + data calls
        "app": "FORMULATION",
        "hints": [
            ("🗓️ Generate a formulation baseline", "generate a formulation baseline"),
            ("📣 Set up a data call", "set up a data call"),
        ],
    },
    # Add more seasons/apps here — each gets its own admin "active months" tunable automatically.
]


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


def _active_months(season: dict[str, Any]) -> set[int]:
    """The season's active months — the admin override if set, else the code default."""
    raw = season.get("months", "")
    try:
        from . import runtime_config
        raw = runtime_config.get_str(_months_key(season["name"])) or raw
    except Exception:  # noqa: BLE001
        pass
    return _parse_months(raw)


def tunable_specs() -> list[dict[str, Any]]:
    """Admin-tunable specs for runtime_config: a master toggle + a per-season 'active months'
    string. Surfaced in the /admin UI; defaults are the code values above."""
    specs: list[dict[str, Any]] = [{
        "key": ENABLED_KEY, "label": "Seasonal hints", "group": "Seasonal hints", "type": "bool",
        "default": True,
        "description": "Lead the widget suggestions with season-relevant workflows during the budget cycle.",
    }]
    for s in _SEASONS:
        specs.append({
            "key": _months_key(s["name"]), "label": f"{s['name']} — active months",
            "group": "Seasonal hints", "type": "string", "default": s["months"],
            "description": (f"Months (1-12) when the {s['name']} hints show for "
                            f"{s['app'] or 'any app'}. e.g. '3-10', '11-2' (wraps), or '3,4,5'."),
        })
    return specs


def current_season(app_code: str | None, when: datetime | None = None) -> str | None:
    """Name of the active season for `app_code` at `when` (default: now, UTC), or None."""
    if not _enabled():
        return None
    code = (app_code or "").strip().upper()
    month = (when or datetime.now(timezone.utc)).month
    for s in _SEASONS:
        if (s["app"] is None or s["app"] == code) and month in _active_months(s):
            return s["name"]
    return None


def seasonal_hints(app_code: str | None, when: datetime | None = None) -> list[dict[str, str]]:
    """High-priority balloons for the current season + application, or [] when out of season / off.

    `when` defaults to now (UTC). Each returned dict is a ready balloon: {"label", "send"}.
    """
    if not _enabled():
        return []
    code = (app_code or "").strip().upper()
    month = (when or datetime.now(timezone.utc)).month
    out: list[dict[str, str]] = []
    for s in _SEASONS:
        if (s["app"] is None or s["app"] == code) and month in _active_months(s):
            out.extend({"label": lbl, "send": send} for lbl, send in s["hints"])
    return out
