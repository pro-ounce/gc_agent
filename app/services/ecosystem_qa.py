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
from ..models.chat import UIBlock
from .intent_schema import extract_intent, route as route_intent
from ..commons.logger import get_logger

log = get_logger(__name__)


def _table_block(title: str, columns: list[str], rows: list[list[Any]]) -> UIBlock:
    """A structured table the widget renders with its block component (not a markdown dump)."""
    return UIBlock(type="table", title=title, columns=columns, rows=rows)


def _list_block(title: str, items: list[str]) -> UIBlock:
    """A structured set (e.g. a user's roles in one app) — rendered as chips/rows, not prose."""
    return UIBlock(type="list", title=title, items=items)


def _roles_count(n: int) -> str:
    """Compact access summary — '7 roles' — so an overview doesn't dump every role name."""
    return f"{n} role" if n == 1 else f"{n} roles"


async def _entity_table(tool: str, headers: dict[str, str] | None, cols: list[tuple[str, str]],
                        title: str, lead: str, cap: int = 50,
                        keep=None, fmt: dict[str, Any] | None = None,
                        empty: str = "") -> FlowResult | None:
    """Fetch a read tool and render a capped structured table. `cols` = [(Header, row_key)];
    `keep` filters rows; `fmt` maps a row_key → a value formatter. Returns None on total failure
    so the query falls back to the LLM router (Option B) rather than dead-ending."""
    try:
        res = await tool_registry.execute(tool, {}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.bind(func="entity_table", tool=tool).warning(f"fetch failed: {exc}")
        return None
    rows_in = [r for r in (data or []) if isinstance(r, dict)]
    if keep:
        rows_in = [r for r in rows_in if keep(r)]
    if not rows_in:
        return FlowResult(message=empty) if empty else None
    fmt = fmt or {}
    rows = [[(fmt[k](r.get(k)) if k in fmt else str(r.get(k) or "")) for _, k in cols]
            for r in rows_in[:cap]]
    total = len(rows_in)
    note = f"\n\n_Showing {cap} of {total}._" if total > cap else ""
    return FlowResult(message=lead.format(n=total) + note,
                      blocks=[_table_block(title, [h for h, _ in cols], rows)])


async def _users_list(headers: dict[str, str] | None) -> FlowResult | None:
    return await _entity_table(
        "getActiveUsers_get", headers,
        [("Username", "userName"), ("Name", "fullName"), ("Email", "emailAddress")],
        "Active users", "There are **{n} active users** — here's a sample:", cap=40)


async def _roles_catalog(headers: dict[str, str] | None) -> FlowResult | None:
    return await _entity_table(
        "getAllApplicationRoles_get", headers,
        [("Role", "roleName"), ("Application", "applicationName"), ("Description", "roleDescription")],
        "All application roles", "There are **{n} roles** across every application:", cap=50,
        keep=lambda r: str(r.get("enabled", "Y")).upper() != "N")


async def _fund_groups(headers: dict[str, str] | None) -> FlowResult | None:
    return await _entity_table(
        "getAllFundGroups_get", headers,
        [("Fund group", "name"), ("Code", "code"), ("Description", "description")],
        "Fund groups", "There are **{n} fund groups**:", cap=50,
        keep=lambda r: str(r.get("enabled", "Y")).upper() != "N")


async def _fiscal_years(headers: dict[str, str] | None) -> FlowResult | None:
    return await _entity_table(
        "getAllFiscalYears_post", headers,
        [("Fiscal year", "fiscalYear"), ("Type", "fyTypeDesc"), ("Status", "enabled")],
        "Fiscal years", "There are **{n} fiscal years**:", cap=50,
        fmt={"enabled": lambda v: "Active" if str(v).upper() == "Y" else "Inactive"})


# Orgs and sub-orgs live in ONE self-referential table, split only by parentOrganizationId:
#   parentOrganizationId == -1  → a top-level org / PROGRAM OFFICE
#   parentOrganizationId  >  0  → a DIVISION (sub-org) under that parent org
# getOrganizations_get returns the program offices (all -1); getAllDivisions_post returns the
# divisions (all > 0). We still assert the split in code so a listing can never mislabel rows
# if an endpoint ever starts returning the whole table.
def _parent_org(r: dict[str, Any]) -> str:
    return str(r.get("parentOrganizationId", "")).strip()


def _enabled(r: dict[str, Any]) -> bool:
    return str(r.get("enabled", "Y")).upper() != "N"


async def _organizations(headers: dict[str, str] | None) -> FlowResult | None:
    # Program offices only: parentOrganizationId == -1.
    return await _entity_table(
        "getOrganizations_get", headers,
        [("Organization", "organizationName"), ("Code", "organizationCode"),
         ("Description", "organizationDescription")],
        "Organizations", "There are **{n} organizations** (program offices):", cap=50,
        keep=lambda r: _enabled(r) and _parent_org(r) == "-1")


async def _divisions(headers: dict[str, str] | None) -> FlowResult | None:
    # Divisions only: a real parent org (parentOrganizationId > 0), never a program office.
    return await _entity_table(
        "getAllDivisions_post", headers,
        [("Division", "organizationName"), ("Code", "organizationCode"),
         ("Description", "organizationDescription")],
        "Divisions", "There are **{n} divisions**:", cap=50,
        keep=lambda r: _enabled(r) and _parent_org(r) not in ("", "-1"))

# ── intent cues ────────────────────────────────────────────────────────────────
_MY_ACCESS = re.compile(
    r"\b(my (access|applications?|apps|roles)|what can i (do|access)|apps? i have|"
    r"applications? i have|what (do|have) i have access to)\b", re.I)
_LIST_APPS = re.compile(
    r"\b(what|which|list|show|see|all|how many)\b[^.?]{0,30}\bapplications?\b|"
    r"\bapplication (list|catalog(ue)?)\b|\bwhat('?s| is) in the (platform|ecosystem|system)\b", re.I)
_ROLES_CUE = re.compile(r"\broles?\b", re.I)
# This module is READ-ONLY. A message with an assignment/mutation verb ("assign role X to
# user Y", "grant access", "revoke …") must fall straight through to the skill/flow path —
# never be hijacked as a "roles in <app>" listing just because it contains the word "role".
_MUTATION_CUE = re.compile(
    r"\b(assign|re-?assign|reassign|grant|allocate|provision|revoke|un-?assign|de-?assign|"
    r"remove|deactivate|activate|enable|disable|onboard)\b"
    r"|\b(make|set|mark)\b[^.?]{0,60}\b(default|favou?rite)\b"   # edit: make/set … default/favourite
    r"|\bdefault (app|application)\b|\bfavou?rite\b", re.I)
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
    # "applications/apps/access/roles assigned to <user>", "granted to <user>"
    re.compile(r"\b(?:applications?|apps?|access|roles?) (?:assigned|granted|given) to (?:the )?([A-Za-z0-9._@-]+)", re.I),
    # "what applications does <user> have", "which apps has <user>"
    re.compile(r"what (?:applications?|apps?|access|roles?) (?:does|do|has|have) ([A-Za-z0-9._@-]+)\b", re.I),
    # bare "assigned to <user>" (mutations already bailed above; a bogus name falls through)
    re.compile(r"\bassigned to (?:the )?([A-Za-z0-9._@-]+)", re.I),
]
_STOP = {"i", "you", "we", "my", "me", "us", "the", "a", "an", "this", "that", "user", "everyone"}


async def handle(message: str, headers: dict[str, str] | None) -> FlowResult | None:
    msg = (message or "").strip()
    if not msg:
        return None

    # Read-only module: never intercept a mutation (assign/grant/revoke a role, etc.) — let it
    # reach the assign_access skill / guided flow instead of answering with a role listing.
    if _MUTATION_CUE.search(msg):
        return None

    # ── Option A: parse the query into a structured intent and dispatch on it ──
    # An application named in the message resolves to its dict + seeds the slot extractor.
    app = await _mentioned_app(msg, headers)
    apps_map: dict[str, str] = {}
    if app:
        for k in (app.get("name"), app.get("code")):
            if k:
                apps_map[str(k).lower()] = str(app.get("code") or "")
    intent = extract_intent(msg, apps_map)
    r = route_intent(intent)

    # UNKNOWN / mutation / low confidence → fall through to the LLM router (Option B).
    # First, honour current-app context: a bare "roles?" / "who has access?" while the user
    # is viewing an application is about THAT application.
    if r in ("", "skill_or_flow") or intent.confidence < 0.6:
        cur = await _current_app(headers) if not app else None
        if cur and intent.entity == "role":
            log.bind(func="intent_router", route="app_roles", ctx="current").info("intent_route")
            return await _app_roles(cur, headers, from_current=True)
        if cur and (intent.entity == "user" or intent.action == "who"):
            log.bind(func="intent_router", route="app_users", ctx="current").info("intent_route")
            return await _app_users(cur, headers, from_current=True)
        log.bind(func="intent_router", route="escalate", entity=intent.entity,
                 subject=intent.subject, conf=intent.confidence).info("intent_escalate")
        return None

    fr = await _dispatch(r, intent, app, headers)
    log.bind(func="intent_router", route=r, entity=intent.entity, subject=intent.subject,
             app=intent.app, conf=intent.confidence, answered=fr is not None).info("intent_route")
    return fr


async def _dispatch(r: str, intent: Any, app: dict[str, Any] | None,
                    headers: dict[str, str] | None) -> FlowResult | None:
    """Run the handler an Intent routed to. App-scoped routes fall back to the application the
    user is viewing when none was named. A route with no handler yet returns None → the LLM
    router (Option B) answers it (that is the safe fallback, not a wrong guess)."""
    if r in ("app_roles", "app_users", "about_app", "my_access_in_app") and not app:
        app = await _current_app(headers)
        if not app:
            return None
    from_current = app is not None and intent.app == ""

    if r == "list_apps":
        return await _list_apps(headers, show_all=bool(intent.filters.get("show_all")))
    if r in ("my_access", "my_roles_all"):
        return await _my_access(headers)
    if r == "named_access":
        return await _named_access(intent.user, headers)
    if r == "my_access_in_app":
        return await _my_access_in_app(app, headers, getattr(intent, "raw", ""))
    if r == "my_offices":
        return await _my_offices(app, headers, role=_office_role(intent.raw))
    if r == "app_roles":
        return await _app_roles(app, headers, from_current=from_current)
    if r == "app_users":
        return await _app_users(app, headers, from_current=from_current)
    if r == "about_app":
        return await _about_app(app, headers, from_current=from_current)
    if r == "roles_catalog":
        # "roles" while viewing an app → that app; a bare "list all roles" → the full catalogue.
        cur = await _current_app(headers)
        return await _app_roles(cur, headers, from_current=True) if cur else await _roles_catalog(headers)
    if r in ("users_list", "users_in_app"):
        return await _users_list(headers)
    if r == "fund_groups":
        return await _fund_groups(headers)
    if r == "fiscal_years":
        return await _fiscal_years(headers)
    if r == "organizations":
        return await _organizations(headers)
    if r == "divisions":
        return await _divisions(headers)
    # org_level (sub-orgs / program offices) is tree-relative + fund-group-scoped → LLM fallback
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
    msg += "\n\nWant a closer look? Try *“about [application]”*, *“roles in [application]”*, or *“my access”*."
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


# Access-type codes (budget_user_details.accessType) → readable names, from the authoritative
# BUDGET_USER_ACCESS_TYPES lookup. NOTE: the app-role vocabulary (USER_ROLE lookup:
# planningApprover/planningFacilitator) is SEPARATE from these access types (AAA Program Office
# Approver / AAF Program Office Facilitator) — see the access-model open questions. Aliases
# (CFO/DCFO) collapse a code and its short form so a user's access types dedup cleanly.
_ACCESS_TYPE_NAMES = {
    # fund-group level
    "FG": "Fund Group Admin", "FGB": "Fund Group Budget", "FGE": "Fund Group Executive",
    "EI": "Fund Group Executive Inquiry",
    # program-office level (UI terminology)
    "AAA": "Program Office Approver", "AAF": "Program Office Facilitator", "B": "Budget Facilitator",
    # division level
    "DP": "Division Planner", "FD": "FMD Division Director",
    # cross-cutting
    "AU": "Acquisition Users", "IU": "Inquiry User",
    # executive
    "C": "Chief Financial Officer", "CFO": "Chief Financial Officer",
    "DC": "Deputy Chief Financial Officer", "DCFO": "Deputy Chief Financial Officer",
    "CO": "Commissioner", "D": "Deputy Commissioner", "AC": "Assistant Commissioner",
    "DAC": "Deputy Assistant Commissioner",
}


def _access_label(r: dict[str, Any]) -> str:
    """Readable access-type name for a budget_user_details row — prefer the lookup mapping of
    the code, then the row's own value/desc, then the raw code."""
    code = str(r.get("accessType") or "").strip()
    mapped = _ACCESS_TYPE_NAMES.get(code.upper())
    if mapped:
        return mapped
    return str(r.get("accessTypeValue") or r.get("accessTypeDesc") or code).strip()


def _cap_join(items: list[str], cap: int = 8) -> str:
    """Comma-join with a '+N more' tail so a super-admin's long list doesn't wall the card."""
    items = [i for i in items if i]
    if not items:
        return "—"
    if len(items) <= cap:
        return ", ".join(items)
    return ", ".join(items[:cap]) + f" +{len(items) - cap} more"


async def _named_access(user: str, headers: dict[str, str] | None) -> FlowResult | None:
    """Who a named user *really* is: their application role(s) AND access type(s) per
    application — because capability is the role (what they can open) crossed with the access
    type (what data they see), and neither alone tells you. Returns None (fall through) when the
    name matches no user, so a false-positive match doesn't dead-end."""
    u = user.strip().lower()
    # Roles — the admin-wide assignment set, filtered to the target user.
    try:
        r1 = await tool_registry.execute("getAllUserApplicationRoles_post", {"userName": user}, headers)
        roles_data = r1.output.get("data") if getattr(r1, "success", False) and isinstance(r1.output, dict) else None
    except Exception:  # noqa: BLE001
        roles_data = None
    role_rows = [r for r in (roles_data or []) if isinstance(r, dict)
                 and str(r.get("userName") or "").strip().lower() == u]
    # Access types — the budget_user_details assignment set, filtered to the target user.
    try:
        r2 = await tool_registry.execute("getBudgetUsers_post", {}, headers)
        bud_data = r2.output.get("data") if getattr(r2, "success", False) and isinstance(r2.output, dict) else None
    except Exception:  # noqa: BLE001
        bud_data = None
    bud_rows = [r for r in (bud_data or []) if isinstance(r, dict)
                and str(r.get("userName") or "").strip().lower() == u]

    if not role_rows and not bud_rows:
        return None

    full = ""
    for r in (role_rows or bud_rows):
        full = (str(r.get("fullName") or "").strip()
                or f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip())
        if full:
            break
    full = full or user

    apps: dict[str, dict[str, list[str]]] = {}
    for r in role_rows:
        app = str(r.get("applicationName") or r.get("applicationCode") or "").strip()
        if not app:
            continue
        e = apps.setdefault(app, {"roles": [], "access": []})
        role = str(r.get("roleName") or r.get("role") or "").strip()
        if role and role not in e["roles"]:
            e["roles"].append(role)
    for r in bud_rows:
        app = str(r.get("applicationName") or r.get("applicationCode") or "").strip()
        if not app:
            continue
        e = apps.setdefault(app, {"roles": [], "access": []})
        at = _access_label(r)
        if at and at not in e["access"]:
            e["access"].append(at)
    if not apps:
        return None

    rows = [[app, _cap_join(sorted(apps[app]["roles"])), _cap_join(sorted(apps[app]["access"]))]
            for app in sorted(apps)]
    lead = _say(f"**{full}** ({user}) — role × access type across **{len(apps)} applications**. "
                "Capability is the **role** (what they can open) crossed with the **access type** "
                "(what data they see), so read them together:")
    return FlowResult(
        message=lead,
        blocks=[_table_block(f"{full} — role × access type",
                             ["Application", "Role(s)", "Access type(s)"], rows)],
        suggestions=[_chip("List applications", "list applications")])


async def _app_users(app: dict[str, Any], headers: dict[str, str] | None,
                     from_current: bool = False) -> FlowResult:
    """Who works in an application and — the part that matters — HOW their access is scoped.
    Everyone with a role can open the app; the ACCESS TYPE is what filters the data they see,
    constrains their actions, and limits them to their assigned offices/divisions. So the
    listing pairs each user's role(s) with their access type(s) in this app."""
    try:
        r1 = await tool_registry.execute("getAllUserApplicationRoles_post", {"userName": "*"}, headers)
        rdata = r1.output.get("data") if getattr(r1, "success", False) and isinstance(r1.output, dict) else None
    except Exception:  # noqa: BLE001
        rdata = None
    role_rows = [r for r in (rdata or []) if isinstance(r, dict) and str(r.get("applicationId") or "") == str(app["id"])]
    try:
        r2 = await tool_registry.execute("getBudgetUsers_post", {}, headers)
        bdata = r2.output.get("data") if getattr(r2, "success", False) and isinstance(r2.output, dict) else None
    except Exception:  # noqa: BLE001
        bdata = None
    bud_rows = [r for r in (bdata or []) if isinstance(r, dict) and str(r.get("applicationId") or "") == str(app["id"])]

    if not role_rows and not bud_rows:
        return FlowResult(message=f"Looks like nobody works in **{app['name']}** yet.")

    by_user: dict[str, dict[str, Any]] = {}
    for r in role_rows:
        u = str(r.get("userName") or "").strip()
        if not u:
            continue
        e = by_user.setdefault(u, {"roles": [], "access": []})
        role = str(r.get("roleName") or r.get("role") or "").strip()
        if role and role not in e["roles"]:
            e["roles"].append(role)
    for r in bud_rows:
        u = str(r.get("userName") or "").strip()
        if not u:
            continue
        e = by_user.setdefault(u, {"roles": [], "access": []})
        at = _access_label(r)
        if at and at not in e["access"]:
            e["access"].append(at)

    n = len(by_user)
    cap = 30
    listed = sorted(by_user.items())[:cap]
    trows = [[u, _cap_join(sorted(e["roles"]), 4), _cap_join(sorted(e["access"]), 4)] for u, e in listed]
    pre = f"Since you're in **{app['name']}**, " if from_current else ""
    head = pre + _say(f"**{n} people** work in **{app['name']}** — everyone with a role can open it; "
                      "what differs is their **access type**, which filters the data they see, "
                      "constrains their actions, and scopes them to their offices/divisions")
    head = head[0].upper() + head[1:]
    if n > cap:
        head += f" (first {cap})"
    return FlowResult(
        message=head + ":",
        blocks=[_table_block(f"{app['name']} — users", ["User", "Role(s)", "Access type(s)"], trows)],
        suggestions=[_chip(f"Roles in {app['name'].split()[0]}", f"roles in {app['code']}", icon="role")])


def _pick(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = str(d.get(k) or "").strip()
        if v:
            return v
    return ""


# "my offices as a Planning Approver" / "where can I work as Budget Facilitator" → the role.
_OFFICE_ROLE = re.compile(r"\bas (?:an? |the )?([A-Za-z][A-Za-z /&.-]{2,40}?)(?:\s+in\b|[?.!,]|$)", re.I)


def _office_role(query: str) -> str:
    m = _OFFICE_ROLE.search(query or "")
    return m.group(1).strip() if m else ""


async def _my_offices_for_role(app: dict[str, Any], role: str,
                               headers: dict[str, str] | None) -> FlowResult:
    """The caller's offices FOR one security role — via getFilteredOrganizationsByUser_put
    (role + all fund groups), backed by USER_OFFICES_SECURITY_ROLE_V (WHERE ROLE = role)."""
    app_id = str(app["id"])
    try:
        res = await tool_registry.execute(
            "getFilteredOrganizationsByUser_put",
            {"applicationId": app_id, "role": role, "fundGroupIds": []}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
    except Exception:  # noqa: BLE001
        data = None
    orgs = [o for o in (data or []) if isinstance(o, dict)]
    if not orgs:
        return FlowResult(
            message=f"No offices are assigned to you as **{role}** in **{app['name']}** "
                    "(or that isn't a security role you hold here).")
    role_shown = _pick(orgs[0], "roleName", "role") or role
    names = sorted({_pick(o, "organizationName", "name", "organizationCode") for o in orgs} - {""})
    lead = _say(f"As a **{role_shown}** in **{app['name']}**, you can act in **{len(names)} offices**:")
    return FlowResult(
        message=lead,
        blocks=[_table_block(f"My offices as {role_shown} — {app['name']}", ["Office"],
                             [[n] for n in names])])


async def _my_offices(app: dict[str, Any] | None, headers: dict[str, str] | None,
                      role: str = "") -> FlowResult | None:
    """The caller's own offices — the organizations they can act in for the current
    application, grouped by fund group. Caller-scoped: driven by the logged-in user's token
    via the by-user cascade (fund groups → organizations), backed by USER_OFFICES_SECURITY_ROLE_V.
    When a security role is named, scope to that role via the filtered API. Returns None to
    fall through when no application is in context."""
    if not app:
        app = await _current_app(headers)
    if not app:
        return None
    if role:
        return await _my_offices_for_role(app, role, headers)
    app_id = str(app["id"])
    try:
        fg_res = await tool_registry.execute("getFundGroupsByUser_post", {"applicationId": app_id}, headers)
        fgs = fg_res.output.get("data") if getattr(fg_res, "success", False) and isinstance(fg_res.output, dict) else None
    except Exception:  # noqa: BLE001
        fgs = None
    fgs = [f for f in (fgs or []) if isinstance(f, dict)]
    if not fgs:
        return FlowResult(message=f"You don't have any fund-group access in **{app['name']}** yet.")

    groups: list[tuple[str, list[str]]] = []
    for f in fgs:
        fid = _pick(f, "fundGroupId", "id")
        fname = _pick(f, "fundGroupName", "name", "fundGroupCode", "code") or fid
        try:
            org_res = await tool_registry.execute(
                "getOrganizationsByUser_post", {"applicationId": app_id, "fundGroupId": fid}, headers)
            orgs = org_res.output.get("data") if getattr(org_res, "success", False) and isinstance(org_res.output, dict) else None
        except Exception:  # noqa: BLE001
            orgs = None
        names = sorted({_pick(o, "organizationName", "name", "organizationCode")
                        for o in (orgs or []) if isinstance(o, dict)} - {""})
        groups.append((fname, names))

    total = sum(len(n) for _, n in groups)
    if not total:
        return FlowResult(
            message=f"You have access to **{len(groups)} fund groups** in **{app['name']}**, "
                    "but no offices are assigned to you under them yet.")
    lead = _say(f"In **{app['name']}**, you can act in **{total} offices** across "
                f"**{len(groups)} fund groups**:")
    rows = [[fname, _cap_join(names, 8) if names else "—"] for fname, names in groups]
    return FlowResult(
        message=lead,
        blocks=[_table_block(f"My offices — {app['name']}", ["Fund group", "Offices"], rows)])


# "do I have <role> …", "am I a <role>", "have I got <role>" — captures the role phrase so a
# self-access question about ONE role gets a direct yes/no, not just a list.
_ASK_ROLE = re.compile(
    r"\b(?:do i have|have i got|am i(?: an?| the)?|i have|got)\s+(?:the\s+|an?\s+)?"
    r"(.+?)\s*(?:\brole\b|\baccess\b|\bin\b|\bfor\b|[?.]|$)", re.I)


def _asked_role(query: str) -> str:
    """The specific role a self-access question names ('do I have super admin role' → 'super
    admin'), or '' when none is named (a plain 'my roles in X' listing, or 'access to <app>').
    Strips role/access nouns and leading prepositions so 'access to formulation' / 'in
    formulation' collapse to the bare app word (which the caller then rejects against the app)."""
    m = _ASK_ROLE.search(query or "")
    if not m:
        return ""
    ph = re.sub(r"\b(role|access|permission)s?\b", "", m.group(1), flags=re.I)
    ph = re.sub(r"^\s*(?:the|an?|any|to|in|for|of|at|on|with|from)\s+", "", ph, flags=re.I).strip(" .?")
    if re.fullmatch(r"(?:the|an?|any|to|in|for|of|at|on|with|from)?", ph, re.I):
        return ""
    return ph if 2 <= len(ph) <= 40 else ""


async def _my_access_in_app(app: dict[str, Any], headers: dict[str, str] | None,
                            query: str = "") -> FlowResult:
    """The caller's own roles in ONE named application (e.g. 'my roles in FORMULATION'). When the
    query names a specific role ('do I have super admin role for formulation') it answers yes/no
    from the caller's ACTUAL roles — never a list of all the app's roles."""
    try:
        res = await tool_registry.execute("getActiveUserAppRolesByUserId_post", {}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
    except Exception:  # noqa: BLE001
        data = None
    want = {_norm(app.get("name")), _norm(app.get("code"))} - {""}
    roles: list[str] = []
    for r in (data or []):
        if not isinstance(r, dict):
            continue
        an = _norm(r.get("applicationName") or "")
        ac = _norm(r.get("applicationCode") or "")
        if an in want or ac in want:
            role = str(r.get("roleName") or r.get("role") or "").strip()
            if role and role not in roles:
                roles.append(role)
    name = app.get("name") or app.get("code")
    asked = _asked_role(query)
    if asked and _norm(asked) in want:      # captured the app name ("in/to <app>"), not a role
        asked = ""
    if not roles:
        base = (f"No — you don't have the **{asked}** role in **{name}** "
                f"(you have no roles there at all)." if asked
                else f"You don't have any roles in **{name}** right now.")
        return FlowResult(message=base,
                          suggestions=[_chip("My access", "my access"),
                                       _chip(f"Roles in {name}", f"roles in {name}")])
    # Direct yes/no when a specific role was named — grounded in the caller's real roles.
    if asked:
        an = _norm(asked)
        hit = next((r for r in roles if an and (an in _norm(r) or _norm(r) in an)), None)
        if hit:
            others = len(roles) - 1
            extra = f" (plus {others} other {'role' if others == 1 else 'roles'})" if others else ""
            lead = _say(f"✅ Yes — you have **{hit}** in **{name}**{extra}.")
        else:
            lead = _say(f"No — you don't have **{asked}** in **{name}**. "
                        f"Your **{len(roles)}** role(s) there:")
        return FlowResult(message=lead, blocks=[_list_block(f"Your roles in {name}", sorted(roles))])
    plural = "role" if len(roles) == 1 else "roles"
    lead = _say(f"In **{name}**, you have **{len(roles)} {plural}**:",
                f"Your **{name}** access — **{len(roles)} {plural}**:")
    return FlowResult(message=lead, blocks=[_list_block(f"Your roles in {name}", sorted(roles))])


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
    lead = _say(f"You can jump into **{len(by_app)} applications** — ask about any one to see its roles:",
                f"You've got access to **{len(by_app)} applications** — tell me which to see the roles:")
    rows = [[app, _roles_count(len(by_app[app]))] for app in sorted(by_app)]
    return FlowResult(
        message=lead,
        blocks=[_table_block("Your access", ["Application", "Roles"], rows)],
        suggestions=[_chip("List all applications", "list applications")])
