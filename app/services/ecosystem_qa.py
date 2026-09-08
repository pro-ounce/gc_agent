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

import random
import re
from typing import Any

from . import ecosystem as eco
from . import intent as intent_mod
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
_WHO_ACCESS = re.compile(
    r"\bwho (?:can|has|have|is|are|uses?)\b|\bwhich users?\b|\b(?:list|show|how many) users?\b|"
    r"\busers? (?:in|with|for|of|that|who)\b|\bwho('?s| are)\b", re.I)
_ABOUT_CUE = re.compile(
    r"\b(about|what('?s| is| does)|tell me about|describe|explain|overview of|"
    r"workflow|how does .* work|what can .* do)\b", re.I)
# "what can <user> do", "access for <user>", "<user>'s access", "what does <user> have
# access to" — captures the username. 'i'/'you'/'we'/'my' are handled by _MY_ACCESS above.
_NAMED_ACCESS = [
    re.compile(r"what can ([A-Za-z0-9._@-]+) (?:do|access)\b", re.I),
    re.compile(r"what (?:does|do) ([A-Za-z0-9._@-]+) have access to\b", re.I),
    re.compile(r"\baccess (?:for|of) ([A-Za-z0-9._@-]+)", re.I),
    re.compile(r"\bapplications? (?:for|of) ([A-Za-z0-9._@-]+)", re.I),
    re.compile(r"\b([A-Za-z0-9._@-]+)'s (?:access|applications?|roles?)\b", re.I),
]
_STOP = {"i", "you", "we", "my", "me", "us", "the", "a", "an", "this", "that", "user", "everyone"}


async def handle(message: str, headers: dict[str, str] | None) -> FlowResult | None:
    msg = (message or "").strip()
    if not msg:
        return None

    if _MY_ACCESS.search(msg):
        return await _my_access(headers)

    # An application named in the message steers app-specific answers.
    app = await _mentioned_app(msg, headers)
    if app:
        if _WHO_ACCESS.search(msg):
            return await _app_users(app, headers)
        if _ROLES_CUE.search(msg):
            return await _app_roles(app, headers)
        if _ABOUT_CUE.search(msg) or _norm(msg) == _norm(app["name"]) or _norm(msg) == _norm(app["code"]):
            return await _about_app(app, headers)

    # A named user's access ("what can GCADMIN do"). Only if no application matched above.
    user = _extract_user(msg)
    if user:
        fr = await _named_access(user, headers)
        if fr is not None:
            return fr

    if _LIST_APPS.search(msg):
        return await _list_apps(headers, show_all=bool(re.search(r"\b(all|every|unlicensed|catalog(ue)?|disabled)\b", msg, re.I)))

    # ── semantic fallback: catch natural rewordings the regex missed ──
    name, score = await intent_mod.classify(msg)
    if name:
        cur = app or await _current_app(headers)   # named app, else the one they're viewing
        if name == "list_apps":
            return await _list_apps(headers)
        if name == "my_access":
            return await _my_access(headers)
        if name == "user_access" and user:
            fr = await _named_access(user, headers)
            if fr is not None:
                return fr
        if cur and name == "roles_app":
            return await _app_roles(cur, headers, from_current=app is None)
        if cur and name == "who_access":
            return await _app_users(cur, headers, from_current=app is None)
        if cur and name == "about_app":
            return await _about_app(cur, headers, from_current=app is None)
    return None


async def _current_app(headers: dict[str, str] | None) -> dict[str, Any] | None:
    """The application the user is currently viewing (from the X-Selected-App header)."""
    code = eco.selected_app_code(headers)
    return await eco.resolve_app(code, headers) if code else None


# ── helpers ────────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _chip(label: str, send: str, icon: str = "app") -> dict[str, str]:
    return {"label": label, "send": send, "icon": icon}


def _say(*variants: str) -> str:
    """Pick one friendly phrasing so repeated asks don't read like a form letter."""
    return random.choice(variants)


def _here(app: dict[str, Any], from_current: bool) -> str:
    """A warm acknowledgement when we inferred the app from the screen they're on."""
    return f"Since you're in **{app['name']}**, " if from_current else ""


async def _mentioned_app(msg: str, headers: dict[str, str] | None) -> dict[str, Any] | None:
    """The application referenced in the message, if any — matched on WHOLE-WORD tokens only
    (the code, or a distinctive word of the name), never substrings, so 'budgeting' no longer
    matches 'Budget Analytics'. Picks the longest match so 'formulation' beats a short code."""
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
            hit = k in words                        # whole-word match only
            if hit and (best is None or len(k) > best[0]):
                best = (len(k), a)
    return best[1] if best else None


def _tbl(headers_row: list[str], rows: list[list[str]]) -> str:
    line = "| " + " | ".join(headers_row) + " |\n| " + " | ".join("---" for _ in headers_row) + " |\n"
    line += "\n".join("| " + " | ".join(c or "—" for c in r) + " |" for r in rows)
    return line


async def _list_apps(headers: dict[str, str] | None, show_all: bool = False) -> FlowResult:
    apps_all = await eco.applications(headers)
    if not apps_all:
        return FlowResult(message="I couldn't load the application catalogue just now.")
    apps = apps_all if show_all else [a for a in apps_all if eco.is_available(a)]
    unlicensed = len(apps_all) - len([a for a in apps_all if eco.is_available(a)])
    if show_all:
        rows = [[a["name"], a["code"], "✅" if eco.is_available(a) else "—", (a["desc"] or "")[:52]] for a in apps]
        head = _say(f"Here's the full catalogue — all **{len(apps)} applications** in the environment",
                    f"Sure, the whole lineup — **{len(apps)} applications** in the environment")
        cols = ["Application", "Code", "Licensed", "About"]
    else:
        rows = [[a["name"], a["code"], (a["desc"] or "")[:60]] for a in apps]
        head = _say(f"Here's everything you can jump into on this license — **{len(apps)} applications**",
                    f"You've got **{len(apps)} applications** available on this license",
                    f"Happy to help — **{len(apps)} applications** are live on this license")
        cols = ["Application", "Code", "About"]
    msg = head + ":\n\n" + _tbl(cols, rows)
    if not show_all and unlicensed:
        msg += f"\n\n*{unlicensed} more exist in the environment but aren't licensed — just say “list all applications” to see them.*"
    msg += "\n\nWant a closer look? Try *“about \\<application\\>”*, *“roles in \\<application\\>”*, or *“my access”*."
    chips = [_chip("About Formulation", "about Formulation"),
             _chip("Roles in Formulation", "roles in Formulation"),
             _chip("My access", "my access", icon="role")]
    return FlowResult(message=msg, suggestions=chips)


async def _about_app(app: dict[str, Any], headers: dict[str, str] | None,
                     from_current: bool = False) -> FlowResult:
    roles = await eco.roles_for(app, headers)
    wf = eco.workflow_for(app["code"])
    lead = (f"Since you're in **{app['name']}**, here's the rundown:" if from_current
            else _say(f"Here's the rundown on **{app['name']}**:",
                      f"Sure — here's a quick look at **{app['name']}**:",
                      f"Happy to. **{app['name']}** in a nutshell:"))
    parts = [lead, f"### {app['name']}  \n`{app['code']}`"]
    if not eco.is_available(app):
        parts.append("> ⚠️ *Not available on the current license.*")
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


async def _app_roles(app: dict[str, Any], headers: dict[str, str] | None,
                     from_current: bool = False) -> FlowResult:
    roles = await eco.roles_for(app, headers)
    if not roles:
        return FlowResult(message=f"Hmm, I can't see any roles defined for **{app['name']}** right now.")
    rows = [[("🛡️ " if r["admin"] else "") + r["name"], (r["desc"] or "")[:70]] for r in roles]
    lead = _here(app, from_current) + _say(f"**{app['name']}** has **{len(roles)} roles** — here they are:",
                                           f"there are **{len(roles)} roles** in **{app['name']}**:")
    msg = lead[0].upper() + lead[1:] + "\n\n" + _tbl(["Role", "Description"], rows)
    return FlowResult(message=msg, suggestions=[_chip(f"About {app['name'].split()[0]}", f"about {app['code']}")])


def _extract_user(msg: str) -> str | None:
    """Pull a username out of an access question, ignoring self/pronoun words."""
    for pat in _NAMED_ACCESS:
        m = pat.search(msg)
        if m:
            tok = m.group(1).strip().strip(".'")
            if tok and tok.lower() not in _STOP and len(tok) >= 2:
                return tok
    return None


async def _named_access(user: str, headers: dict[str, str] | None) -> FlowResult | None:
    """A named user's application access + roles, grouped by application. Uses the admin-wide
    assignment set (the per-user tools are caller-scoped). Returns None (fall through to normal
    routing) when the name matches no user — so a false-positive match doesn't dead-end."""
    # Pass the username so the caller-scope isn't injected (the backend returns the platform
    # set; we filter to the target — correct whether or not the backend honours the filter).
    try:
        res = await tool_registry.execute("getAllUserApplicationRoles_post", {"userName": user}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
    except Exception:  # noqa: BLE001
        data = None
    rows = [r for r in (data or []) if isinstance(r, dict)
            and str(r.get("userName") or "").strip().lower() == user.strip().lower()]
    if not rows:
        return None
    full = str(rows[0].get("fullName") or "").strip() or str(rows[0].get("userName") or user).strip()
    by_app: dict[str, list[str]] = {}
    for r in rows:
        app = str(r.get("applicationName") or r.get("applicationCode") or "").strip()
        role = str(r.get("roleName") or r.get("role") or "").strip()
        if app and role and role not in by_app.setdefault(app, []):
            by_app[app].append(role)
    if not by_app:
        return None
    lead = _say(f"Here's what **{full}** ({user}) can get into — **{len(by_app)} applications**:",
                f"**{full}** ({user}) has access to **{len(by_app)} applications**:")
    lines = [lead, ""]
    for app in sorted(by_app):
        lines.append(f"- **{app}** — {', '.join(sorted(by_app[app]))}")
    return FlowResult(message="\n".join(lines),
                      suggestions=[_chip("List applications", "list applications")])


async def _app_users(app: dict[str, Any], headers: dict[str, str] | None,
                     from_current: bool = False) -> FlowResult:
    """The users who have access to an application (with their roles in it). Filters the
    admin-wide assignment set by applicationId; capped for readability."""
    try:
        res = await tool_registry.execute("getAllUserApplicationRoles_post", {"userName": "*"}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
    except Exception:  # noqa: BLE001
        data = None
    rows = [r for r in (data or []) if isinstance(r, dict) and str(r.get("applicationId") or "") == str(app["id"])]
    if not rows:
        return FlowResult(message=f"Looks like nobody has access to **{app['name']}** yet.")
    by_user: dict[str, dict[str, Any]] = {}
    for r in rows:
        u = str(r.get("userName") or "").strip()
        if not u:
            continue
        e = by_user.setdefault(u, {"name": str(r.get("fullName") or "").strip(), "roles": []})
        role = str(r.get("roleName") or r.get("role") or "").strip()
        if role and role not in e["roles"]:
            e["roles"].append(role)
    n = len(by_user)
    cap = 20
    listed = sorted(by_user.items())[:cap]
    trows = [[u, e["name"], ", ".join(e["roles"][:3]) + (" …" if len(e["roles"]) > 3 else "")]
             for u, e in listed]
    pre = f"Since you're in **{app['name']}**, " if from_current else ""
    head = pre + _say(f"**{n} people** can get into **{app['name']}**",
                      f"**{n} users** have access to **{app['name']}**")
    head = head[0].upper() + head[1:]
    if n > cap:
        head += f" — here are the first {cap}"
    msg = head + ":\n\n" + _tbl(["User", "Name", "Roles"], trows)
    return FlowResult(message=msg, suggestions=[_chip(f"Roles in {app['name'].split()[0]}", f"roles in {app['code']}", icon="role")])


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
        return FlowResult(message="Hmm, I don't see any application access on your account yet.")
    by_app: dict[str, list[str]] = {}
    for r in rows:
        app = str(r.get("applicationName") or r.get("applicationCode") or "").strip()
        role = str(r.get("roleName") or r.get("role") or "").strip()
        if app and role and role not in by_app.setdefault(app, []):
            by_app[app].append(role)
    lines = [_say(f"Here's what you can get into — **{len(by_app)} applications**:",
                  f"You've got access to **{len(by_app)} applications**:",
                  f"Nice — you can jump into **{len(by_app)} applications**:"), ""]
    for app in sorted(by_app):
        lines.append(f"- **{app}** — {', '.join(sorted(by_app[app]))}")
    return FlowResult(message="\n".join(lines),
                      suggestions=[_chip("List all applications", "list applications")])
