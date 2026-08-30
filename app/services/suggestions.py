"""
services/suggestions.py
───────────────────────
Context-aware skill suggestions ("balloons") for the widget: given the module / screen the
user is currently on, return the most relevant things the agent can do there. Curated per
module for the headline capabilities, plus any user-created custom skills appended (they are
new capabilities the user explicitly taught the agent).

Kept data-driven and forgiving: an unknown module falls back to a sensible general set.
"""
from __future__ import annotations

from typing import Any

# module code (lower) → [(balloon label, message to send)]
_MODULE_BALLOONS: dict[str, list[tuple[str, str]]] = {
    "administration": [
        ("➕ Create a user", "create a user"),
        ("🔑 Assign an application", "assign an application"),
        ("📊 User access report", "generate a user access report"),
        ("✨ Create a new skill", "create a skill"),
        ("💡 What can you do?", "what can you do"),
    ],
    "reporting": [
        ("📊 User access report", "generate a user access report"),
        ("🔎 Look up a user", "look up user "),
        ("💡 What can you do?", "what can you do"),
    ],
}
# Hubs / aliases that map onto a canonical module set.
_ALIASES = {
    "smarthub": "administration", "smart-hub": "administration", "admin": "administration",
    "admin-app": "administration", "user": "administration", "users": "administration",
    "administration-app": "administration", "reporting-app": "reporting",
}
_DEFAULT: list[tuple[str, str]] = [
    ("📱 Show my applications", "show my applications"),
    ("🎭 What roles do I have?", "what roles do i have"),
    ("🔎 Look up a user", "look up user "),
    ("💡 What can you do?", "what can you do"),
]


def _canon(module: str) -> str:
    m = (module or "").strip().lower()
    m = _ALIASES.get(m, m)
    # strip common prefixes/suffixes: "prj241001_fe_admin-app" → "admin-app" → "administration"
    for pre in ("prj241001_fe_", "prj261001_fe_", "fe_"):
        if m.startswith(pre):
            m = m[len(pre):]
    m = _ALIASES.get(m, m)
    if m.endswith("-app"):
        m = _ALIASES.get(m, m[:-4])
    return _ALIASES.get(m, m)


def module_suggestions(module: str, limit: int = 6) -> list[dict[str, Any]]:
    """Balloons for the given module: curated headline capabilities + custom skills."""
    key = _canon(module)
    base = list(_MODULE_BALLOONS.get(key, _DEFAULT))
    out: list[dict[str, Any]] = [{"label": lbl, "send": send} for lbl, send in base]

    # Append user-created skills as balloons (their trigger keyword is the message to send).
    try:
        from .skills import SKILLS
        from .skill_store import _read_file  # names of persisted custom skills
        custom = {d.get("name") for d in _read_file() if isinstance(d, dict)}
        seen_sends = {b["send"].strip().lower() for b in out}
        for s in SKILLS:
            if s.name in custom and s.keywords:
                send = s.keywords[0]
                if send.lower() not in seen_sends:
                    out.append({"label": f"✨ {(s.summary or s.name).capitalize()}", "send": send})
                    seen_sends.add(send.lower())
    except Exception:  # noqa: BLE001 — suggestions must never break the bootstrap
        pass

    return out[:limit]
