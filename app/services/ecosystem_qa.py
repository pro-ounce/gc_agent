"""
services/ecosystem_qa.py
────────────────────────
Deterministic, read-only answers about the GC360 ecosystem — the applications, the roles
inside each, the workflows they run, and the caller's own access. Rendered straight from the
cached ecosystem model (services/ecosystem.py) so the answers are exact and never
hallucinated. Runs before skill/LLM routing (like services/meta.py); returns None to fall
through when the message isn't an ecosystem question.
"""
from __future__ import annotations

import re
from typing import Any

from . import ecosystem as eco
from .flows import FlowResult
from ..mcp.tool_registry import tool_registry

# ── intent cues ────────────────────────────────────────────────────────────────
_MY_ACCESS = re.compile(
    r"\b(my (access|applications?|apps|roles)|what can i (do|access)|apps? i have|"
    r"applications? i have|what (do|have) i have access to)\b", re.I)
_LIST_APPS = re.compile(
    r"\b(what|which|list|show|see|all|how many)\b[^.?]{0,30}\bapplications?\b|"
    r"\bapplication (list|catalog(ue)?)\b|\bwhat('?s| is) in the (platform|ecosystem|system)\b", re.I)
_ROLES_CUE = re.compile(r"\broles?\b", re.I)
_ABOUT_CUE = re.compile(
    r"\b(about|what('?s| is| does)|tell me about|describe|explain|overview of|"
    r"workflow|how does .* work|what can .* do)\b", re.I)


async def handle(message: str, headers: dict[str, str] | None) -> FlowResult | None:
    msg = (message or "").strip()
    if not msg:
        return None

    if _MY_ACCESS.search(msg):
        return await _my_access(headers)

    # An application named in the message steers app-specific answers.
    app = await _mentioned_app(msg, headers)
    if app:
        if _ROLES_CUE.search(msg):
            return await _app_roles(app, headers)
        if _ABOUT_CUE.search(msg) or _norm(msg) == _norm(app["name"]) or _norm(msg) == _norm(app["code"]):
            return await _about_app(app, headers)

    if _LIST_APPS.search(msg):
        return await _list_apps(headers)

    return None


# ── helpers ────────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _chip(label: str, send: str, icon: str = "app") -> dict[str, str]:
    return {"label": label, "send": send, "icon": icon}


async def _mentioned_app(msg: str, headers: dict[str, str] | None) -> dict[str, Any] | None:
    """The application referenced in the message, if any — matched on the code or the leading
    significant word of the name. Picks the longest match so 'formulation' beats a short code."""
    nmsg = _norm(msg)
    words = set(re.findall(r"[a-z0-9]+", msg.lower()))
    best: tuple[int, dict[str, Any]] | None = None
    for a in await eco.applications(headers):
        keys: list[str] = []
        code = a["code"].lower()
        if len(code) >= 3:
            keys.append(code)                       # e.g. formulation, execution, crp, reporting
        first = re.findall(r"[a-z0-9]+", a["name"].lower())
        if first and len(first[0]) >= 4 and first[0] not in ("data", "user"):
            keys.append(first[0])                   # e.g. formulation, execution, allocation
        for k in keys:
            hit = (k in words) or (len(k) >= 6 and k in nmsg)
            if hit and (best is None or len(k) > best[0]):
                best = (len(k), a)
    return best[1] if best else None


def _tbl(headers_row: list[str], rows: list[list[str]]) -> str:
    line = "| " + " | ".join(headers_row) + " |\n| " + " | ".join("---" for _ in headers_row) + " |\n"
    line += "\n".join("| " + " | ".join(c or "—" for c in r) + " |" for r in rows)
    return line


async def _list_apps(headers: dict[str, str] | None) -> FlowResult:
    apps = await eco.applications(headers)
    if not apps:
        return FlowResult(message="I couldn't load the application catalogue just now.")
    rows = [[a["name"], a["code"], (a["desc"] or "")[:60]] for a in apps]
    msg = (f"There are **{len(apps)} applications** in GC360:\n\n"
           + _tbl(["Application", "Code", "About"], rows)
           + "\n\nAsk me *“about \\<application\\>”*, *“roles in \\<application\\>”*, or *“my access”*.")
    chips = [_chip("About Formulation", "about Formulation"),
             _chip("Roles in Formulation", "roles in Formulation"),
             _chip("My access", "my access", icon="role")]
    return FlowResult(message=msg, suggestions=chips)


async def _about_app(app: dict[str, Any], headers: dict[str, str] | None) -> FlowResult:
    roles = await eco.roles_for(app, headers)
    wf = eco.workflow_for(app["code"])
    parts = [f"### {app['name']}  \n`{app['code']}`"]
    desc = app["desc"] or app["info"]
    if desc:
        parts.append(desc)
    if wf and wf.get("workflow"):
        parts.append("**Workflow** — " + wf["workflow"])
        if wf.get("stages"):
            parts.append("**Stages**: " + " → ".join(wf["stages"]))
    if roles:
        shown = ", ".join(r["name"] for r in roles[:12])
        more = f" *(+{len(roles) - 12} more)*" if len(roles) > 12 else ""
        parts.append(f"**Roles ({len(roles)})**: {shown}{more}")
    msg = "\n\n".join(parts)
    chips = [_chip(f"Roles in {app['name'].split()[0]}", f"roles in {app['code']}", icon="role")]
    for label, send in (wf or {}).get("entry", []):
        chips.append(_chip(label, send, icon="check"))
    return FlowResult(message=msg, suggestions=chips)


async def _app_roles(app: dict[str, Any], headers: dict[str, str] | None) -> FlowResult:
    roles = await eco.roles_for(app, headers)
    if not roles:
        return FlowResult(message=f"**{app['name']}** has no roles I can see.")
    rows = [[("🛡️ " if r["admin"] else "") + r["name"], (r["desc"] or "")[:70]] for r in roles]
    msg = (f"**{app['name']}** has **{len(roles)} roles**:\n\n"
           + _tbl(["Role", "Description"], rows))
    return FlowResult(message=msg, suggestions=[_chip(f"About {app['name'].split()[0]}", f"about {app['code']}")])


async def _my_access(headers: dict[str, str] | None) -> FlowResult:
    """The caller's own application access + roles, grouped by application. Uses the
    caller-scoped tool (the backend forces it to the caller's own id)."""
    try:
        res = await tool_registry.execute("getActiveUserAppRolesByUserId_post", {}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
    except Exception:  # noqa: BLE001
        data = None
    rows = [r for r in (data or []) if isinstance(r, dict)]
    if not rows:
        return FlowResult(message="I couldn't find any application access on your account.")
    by_app: dict[str, list[str]] = {}
    for r in rows:
        app = str(r.get("applicationName") or r.get("applicationCode") or "").strip()
        role = str(r.get("roleName") or r.get("role") or "").strip()
        if app and role and role not in by_app.setdefault(app, []):
            by_app[app].append(role)
    lines = [f"You have access to **{len(by_app)} applications**:", ""]
    for app in sorted(by_app):
        lines.append(f"- **{app}** — {', '.join(sorted(by_app[app]))}")
    return FlowResult(message="\n".join(lines),
                      suggestions=[_chip("List all applications", "list applications")])
