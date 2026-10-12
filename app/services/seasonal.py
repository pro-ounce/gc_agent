"""
services/seasonal.py
────────────────────
Seasonal workflow hints. During the relevant part of the budget cycle, promote the workflows that
matter then — e.g. baseline generation + data calls during the formulation season — as high-priority
suggestion balloons in the widget. Each hint's `send` is the exact TAUGHT phrasing the trigger
recognises (see flows._BASELINE_INTENT_RE / maybe_start), so tapping a seasonal hint opens the REAL
guided flow rather than a fake intro.

The season windows are month-based and intentionally simple + TUNABLE (a later iteration can move
them to the /admin OpenSearch tunables or derive them from the fiscal calendar). The current month
plus the current application decide which hints fire. Returns ready balloon dicts ({"label","send"}).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Each season: the months it spans (1-12), the application code it applies to (None = any app), and
# the workflow hints to promote (label, send). `send` MUST be a phrasing the flow trigger recognises.
_SEASONS: list[dict[str, Any]] = [
    {
        "name": "Formulation",
        # Agency budget-formulation window (TUNABLE): spring through the start of the new FY, when
        # teams actively build next-year baselines and run data calls. Adjust to your cycle.
        "months": set(range(3, 11)),            # Mar–Oct
        "app": "FORMULATION",
        "hints": [
            ("🗓️ Generate a formulation baseline", "generate a formulation baseline"),
            ("📣 Set up a data call", "set up a data call"),
        ],
    },
    # Add more seasons/apps here (execution, reporting cycle, …) — keep each `send` a taught phrasing.
]


def current_season(app_code: str | None, when: datetime | None = None) -> str | None:
    """Name of the active season for `app_code` at `when` (default: now, UTC), or None."""
    code = (app_code or "").strip().upper()
    month = (when or datetime.now(timezone.utc)).month
    for s in _SEASONS:
        if month in s["months"] and (s["app"] is None or s["app"] == code):
            return s["name"]
    return None


def seasonal_hints(app_code: str | None, when: datetime | None = None) -> list[dict[str, str]]:
    """High-priority balloons for the current season + application, or [] when out of season.

    `when` defaults to now (UTC). Each returned dict is a ready balloon: {"label", "send"}.
    """
    code = (app_code or "").strip().upper()
    month = (when or datetime.now(timezone.utc)).month
    out: list[dict[str, str]] = []
    for s in _SEASONS:
        if month in s["months"] and (s["app"] is None or s["app"] == code):
            out.extend({"label": lbl, "send": send} for lbl, send in s["hints"])
    return out
