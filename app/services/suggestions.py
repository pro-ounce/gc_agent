"""
services/suggestions.py
───────────────────────
Context-aware suggestions ("balloons") for the widget: given the application the user is
currently in, offer the most relevant things the agent can do or answer there. Driven by the
live ecosystem catalogue (services/ecosystem.py) so every LICENSED application gets sensible
balloons — the do-able workflow entry points curated for it, plus ecosystem Q&A about it —
not just the two that were hand-curated. Custom (user-taught) skills are appended.

Layering, highest priority first:
  1. admin/store overrides (hand-curated headline sets for a few hubs)   ← _MODULE_BALLOONS
  2. the application's curated workflow entry points (real actions)      ← ecosystem._WORKFLOWS
  3. ecosystem Q&A about the current application (about / roles / access)
  4. user-created custom skills (new capabilities the user taught)
Falls back to a sensible general set for an unknown / no-current-application context.
"""
from __future__ import annotations

from typing import Any

from . import ecosystem as eco

# Hand-curated headline balloons for a few hubs, kept as an override for where we want a very
# specific set. Everything else is generated from the catalogue.
_MODULE_BALLOONS: dict[str, list[tuple[str, str]]] = {
    "ADMINISTRATION": [
        ("➕ Create a user", "create a user"),
        ("🔑 Assign an application", "assign an application"),
        ("📊 User access report", "generate a user access report"),
        ("✨ Create a new skill", "create a skill"),
    ],
    "REPORTING": [
        ("📊 User access report", "generate a user access report"),
        ("🔎 Look up a user", "look up user "),
    ],
}
# fe module / hub aliases → application code
_ALIASES = {
    "smarthub": "SMART_HUB", "smart-hub": "SMART_HUB",
    "admin": "ADMINISTRATION", "admin-app": "ADMINISTRATION", "administration-app": "ADMINISTRATION",
    "user": "ADMINISTRATION", "users": "ADMINISTRATION",
    "reporting-app": "REPORTING", "formulation-app": "FORMULATION",
}
_DEFAULT: list[dict[str, Any]] = [
    {"label": "📱 List applications", "send": "list applications"},
    {"label": "🎭 My access", "send": "my access"},
    {"label": "🔎 Look up a user", "send": "look up user "},
    {"label": "💡 What can you do?", "send": "what can you do"},
]

# per-appCategory generic actions when an application has no curated workflow entry points.
_CATEGORY_HINTS: dict[str, list[tuple[str, str]]] = {
    "Analytics": [("📈 Open dashboards", "what can you do")],
}


def _strip(module: str) -> str:
    m = (module or "").strip()
    for pre in ("prj241001_fe_", "prj261001_fe_", "fe_"):
        if m.lower().startswith(pre):
            m = m[len(pre):]
    if m.lower().endswith("-app"):
        m = m[:-4]
    return m


def _chip(label: str, send: str) -> dict[str, str]:
    return {"label": label, "send": send}


async def _current_app(module: str, headers: dict[str, str] | None) -> dict[str, Any] | None:
    """Resolve the widget's module string to an application in the catalogue."""
    raw = _strip(module)
    if not raw:
        return None
    code = _ALIASES.get(raw.lower(), raw)
    return await eco.resolve_app(code, headers)


async def module_suggestions(module: str, headers: dict[str, str] | None = None,
                             limit: int = 6) -> list[dict[str, Any]]:
    """Balloons for the current application, built from the catalogue + curated entry points +
    ecosystem Q&A + custom skills. Best-effort: any failure degrades to the default set."""
    try:
        return await _build(module, headers, limit)
    except Exception:  # noqa: BLE001 — the widget bootstrap must never fail on suggestions
        return _DEFAULT[:limit]


async def _build(module: str, headers: dict[str, str] | None, limit: int) -> list[dict[str, Any]]:
    app = await _current_app(module, headers)

    # No known current application → general starter set.
    if app is None:
        return (_append_custom(list(_DEFAULT)))[:limit]

    # An application that exists but the license doesn't enable → steer to what IS available.
    if not eco.is_available(app):
        return [
            _chip(f"⚠️ {app['name']} isn't licensed", f"about {app['code']}"),
            _chip("📱 Available applications", "list applications"),
            _chip("🎭 My access", "my access"),
        ][:limit]

    out: list[dict[str, Any]] = []
    short = app["name"].split()[0]

    # 1) admin/store override for this application, if any.
    for lbl, send in _MODULE_BALLOONS.get(app["code"], []):
        out.append(_chip(lbl, send))

    # 2) curated workflow entry points (real, do-able actions).
    wf = eco.workflow_for(app["code"]) or {}
    for lbl, send in wf.get("entry", []):
        out.append(_chip(f"⚡ {lbl}", send))

    # 3) ecosystem Q&A about the current application.
    for lbl, send in (
        (f"📖 About {short}", f"about {app['code']}"),
        (f"🎭 Roles in {short}", f"roles in {app['code']}"),
        ("🔑 My access", "my access"),
    ):
        out.append(_chip(lbl, send))

    # category-specific nudge (only when nothing curated added an action).
    if not wf.get("entry"):
        for lbl, send in _CATEGORY_HINTS.get(app["category"], []):
            out.append(_chip(lbl, send))

    out = _dedup(out)
    out = _append_custom(out)
    return out[:limit]


def _dedup(chips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in chips:
        key = c["send"].strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _append_custom(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append user-created skills as balloons (their first trigger keyword is the message)."""
    try:
        from .skills import SKILLS
        from .skill_store import _read_file
        custom = {d.get("name") for d in _read_file() if isinstance(d, dict)}
        seen = {c["send"].strip().lower() for c in out}
        for s in SKILLS:
            if s.name in custom and s.keywords:
                send = s.keywords[0]
                if send.lower() not in seen:
                    out.append({"label": f"✨ {(s.summary or s.name).capitalize()}", "send": send})
                    seen.add(send.lower())
    except Exception:  # noqa: BLE001
        pass
    return out
