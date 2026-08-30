"""
services/flows.py — deterministic, chip-driven guided conversations ("flows").

A flow is a small state machine kept in ``session.metadata["flow"]``. It runs BEFORE
the LLM / skill routing so a weak model never has to manage multi-step orchestration:
each turn is interpreted deterministically (yes / no · an application name · a role
name · "show me the list") and the flow emits the next prompt plus chips (suggestions
the widget renders and prefills on click).

Currently one flow — ``onboard``: right after a user is created, guide the admin through
granting access one application at a time —

    offer_apps ──yes──▶ pick_app ──app chosen──▶ pick_role ──role chosen──▶ confirm_assign
        ▲                                                                        │
        └───────────────────── "assign another?" ◀──(confirm succeeds)───────────┘

The actual write goes through the normal confirm step (``addUserApplicationAndRole_post``
+ the registry's name→id resolvers), so the flow only *collects* app + role and hands off.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..commons.logger import get_logger
from ..mcp.tool_registry import tool_registry

log = get_logger(__name__)

# Intent cues. _YES deliberately includes the flow's own verbs ("assign", "another",
# "more"); _NO the exit words. At a pick step an unrecognised line just re-shows the list.
_YES = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|please|assign|grant|add|another|more|continue|go ahead|do it)\b", re.I)
_NO = re.compile(r"\b(no|nope|not now|later|skip|done|finish|finished|stop|cancel|exit|quit|nothing|nevermind|never mind|that'?s all)\b", re.I)

MAX_CHIPS = 8  # chips shown before we fall back to "type a name to search"


@dataclass
class FlowResult:
    """A guided turn's output. Either a plain prompt (message + chips) or a hand-off to
    the confirm step (``pending`` set). ``done`` marks the flow finished (state cleared)."""
    message: str
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None   # {tool_name, tool_args, summary} → emit confirm
    done: bool = False


def _chip(label: str, send: str | None = None, icon: str | None = None) -> dict[str, Any]:
    return {"label": label, "send": send if send is not None else label, "icon": icon}


_FLOWS = ("onboard", "create_user", "create_skill")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SKILL_INTENT_RE = re.compile(
    r"\b(create|add|make|build|teach|define|register)\b.{0,20}\bskill\b", re.I)

# Mandatory basic account fields collected one-by-one. Chip labels resolve to codes via
# the admin lookups at execute time ("Local"→L inside "Local Account"), so we pass names.
TYPE_CHIPS = ["Local", "External", "Global", "Stale"]
CAT_CHIPS = ["Core", "Temporary", "Service", "API", "Customer", "Maintenance", "Testing", "Help & Support"]


def is_active(session: Any) -> bool:
    f = session.metadata.get("flow")
    return isinstance(f, dict) and f.get("name") in _FLOWS


# ── data helpers ──────────────────────────────────────────────────────────────

async def _apps(headers: dict[str, str] | None) -> list[dict[str, Any]]:
    """Enabled applications as {id, name, code}."""
    out: list[dict[str, Any]] = []
    try:
        res = await tool_registry.execute("getAllApplications_get", {}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        for a in (data or []):
            if not isinstance(a, dict) or str(a.get("enabled", "Y")).upper() == "N":
                continue
            name = str(a.get("applicationName") or a.get("applicationCode") or "").strip()
            if name and a.get("applicationId") not in (None, ""):
                out.append({"id": a["applicationId"], "name": name, "code": str(a.get("applicationCode") or "").strip()})
    except Exception as exc:  # noqa: BLE001 — a guided list must never crash the turn
        log.bind(func="flow_apps").warning(f"app list failed: {exc}")
    return out


async def _roles(headers: dict[str, str] | None, app_id: Any) -> list[dict[str, Any]]:
    """Roles for an application as {id, name}."""
    out: list[dict[str, Any]] = []
    try:
        res = await tool_registry.execute("getApplicationRolesByAppId_get", {"applicationId": app_id}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        seen: set[str] = set()
        for r in (data or []):
            if not isinstance(r, dict) or str(r.get("enabled", "Y")).upper() == "N":
                continue
            name = str(r.get("roleName") or r.get("role") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append({"id": r.get("applicationRoleId"), "name": name})
    except Exception as exc:  # noqa: BLE001
        log.bind(func="flow_roles").warning(f"role list failed: {exc}")
    return out


def _match(msg: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a free-text line to one item by name/code — exact, then contains."""
    m = msg.strip().lower()
    if not m:
        return None
    for it in items:
        names = [str(it.get("name", "")).lower(), str(it.get("code", "")).lower()]
        if any(n and n == m for n in names):
            return it
    # contains (only when unambiguous): the message names exactly one item
    hits = [it for it in items
            if any(n and (n in m or m in n) for n in (str(it.get("name", "")).lower(), str(it.get("code", "")).lower()))]
    return hits[0] if len(hits) == 1 else None


def _list_chips(items: list[dict[str, Any]], icon: str, tail: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    chips = [_chip(it["name"], it["name"], icon=icon) for it in items[:MAX_CHIPS]]
    note = ""
    if len(items) > MAX_CHIPS:
        note = f"\n\n_Showing {MAX_CHIPS} of {len(items)} — or just type a name._"
    return chips + tail, note


# ── flow entry points ─────────────────────────────────────────────────────────

def start_onboarding(session: Any, first_name: str | None, user_name: str, user_id: Any = None) -> FlowResult:
    """Called right after a user is created — arm the onboarding flow and offer access."""
    who = (first_name or user_name or "The user").strip()
    session.metadata["flow"] = {
        "name": "onboard", "firstName": who, "userName": user_name, "userId": user_id,
        "stage": "offer_apps", "assigned": [],
        "currentAppId": None, "currentAppName": None, "currentRoleName": None,
    }
    return FlowResult(
        message=f"🎉 **{who}**'s account is ready. Want to give {who} access to an application now?",
        suggestions=[_chip("Assign an application", "assign an application", icon="app"),
                     _chip("Not now", "not now", icon="skip")],
    )


def maybe_start(session: Any, message: str) -> FlowResult | None:
    """Start a guided flow from a fresh intent (when none is active). Currently: a
    create-user request WITHOUT enough detail (no email present) opens the guided intake;
    a fully-detailed 'create user … email …' message is left to the normal skill path."""
    if is_active(session):
        return None
    # Skill authoring intent takes precedence ("create a skill" must not read as create-user).
    if _SKILL_INTENT_RE.search(message or ""):
        return start_create_skill(session, message or "")
    from ..services import skills
    sk = skills.match(message or "")
    if sk and sk.name == "create_user" and not re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", message or ""):
        return start_create(session)
    return None


async def handle(session: Any, message: str, headers: dict[str, str] | None) -> FlowResult | None:
    """Advance the active flow for this turn. Returns None if no flow is active (fall
    through to normal routing) — including when the user pivots to another known task."""
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    name = flow.get("name")
    msg = (message or "").strip()

    if name == "onboard":
        # Pivot escape: an unrelated known task (create another user, run a report) ends
        # onboarding and falls through to normal routing.
        from ..services import skills
        other = skills.match(msg)
        if other and other.name in ("create_user", "generate_report"):
            session.metadata.pop("flow", None)
            return None
        return await _onboard(session, flow, msg, headers)

    if name == "create_user":
        return await _create(session, flow, msg, headers)

    if name == "create_skill":
        return await _create_skill(session, flow, msg, headers)

    return None


async def _onboard(session: Any, flow: dict[str, Any], msg: str, headers: dict[str, str] | None) -> FlowResult:
    stage = flow.get("stage")
    who = flow["firstName"]

    if stage == "offer_apps":
        if _NO.search(msg) and not _YES.search(msg):
            return _finish(session, flow)
        flow["stage"] = "pick_app"
        return await _prompt_app(flow, headers)

    if stage == "pick_app":
        if _NO.search(msg) and not _match(msg, await _apps(headers)):
            return _finish(session, flow)
        apps = await _apps(headers)
        app = _match(msg, apps)
        if app:
            flow["currentAppId"] = app["id"]
            flow["currentAppName"] = app["name"]
            flow["stage"] = "pick_role"
            return await _prompt_role(flow, headers)
        return await _prompt_app(flow, headers, apps=apps)   # unrecognised → re-show the list

    if stage == "pick_role":
        roles = await _roles(headers, flow["currentAppId"])
        if _NO.search(msg) and not _match(msg, roles):
            # back out of this app rather than ending the whole flow
            flow["stage"] = "offer_apps"
            flow["currentAppId"] = flow["currentAppName"] = None
            return _offer_another(flow, prefix="No problem. ")
        role = _match(msg, roles)
        if role:
            flow["currentRoleName"] = role["name"]
            flow["stage"] = "confirm_assign"
            app = flow["currentAppName"]
            summary = f"Assign **{app} · {role['name']}** to **{who}**. Shall I go ahead?"
            # Pass names; the registry resolves userName/roleName→id (numeric appId given).
            args = {"userId": flow["userName"], "applicationId": flow["currentAppId"],
                    "applicationRoleId": role["name"]}
            return FlowResult(message=summary,
                              pending={"tool_name": "addUserApplicationAndRole_post",
                                       "tool_args": args, "summary": summary})
        return await _prompt_role(flow, headers, roles=roles)   # unrecognised → re-show roles

    # confirm_assign is resolved by the confirm handler (after_assign / on_decline);
    # any stray turn there just re-offers.
    return _offer_another(flow)


async def _prompt_app(flow: dict[str, Any], headers: dict[str, str] | None,
                      apps: list[dict[str, Any]] | None = None) -> FlowResult:
    apps = apps if apps is not None else await _apps(headers)
    who = flow["firstName"]
    if not apps:
        return FlowResult(message=f"Which application should **{who}** have access to? Tell me the name.",
                          suggestions=[_chip("Not now", "not now", icon="skip")])
    chips, note = _list_chips(apps, "app", [_chip("Not now", "not now", icon="skip")])
    return FlowResult(message=f"Which application should **{who}** have access to? Pick one or type a name:{note}",
                      suggestions=chips)


async def _prompt_role(flow: dict[str, Any], headers: dict[str, str] | None,
                       roles: list[dict[str, Any]] | None = None) -> FlowResult:
    roles = roles if roles is not None else await _roles(headers, flow["currentAppId"])
    who, app = flow["firstName"], flow["currentAppName"]
    if not roles:
        return FlowResult(message=f"I couldn't find roles for **{app}**. Type a role name, or pick another application.",
                          suggestions=[_chip("Choose another application", "assign another application", icon="app"),
                                       _chip("Not now", "not now", icon="skip")])
    chips, note = _list_chips(roles, "role", [_chip("Back to applications", "assign another application", icon="app")])
    return FlowResult(message=f"And what role should **{who}** have in **{app}**? Pick one or type a name:{note}",
                      suggestions=chips)


def _offer_another(flow: dict[str, Any], prefix: str = "") -> FlowResult:
    who = flow["firstName"]
    return FlowResult(
        message=f"{prefix}Would you like to assign another application to **{who}**?",
        suggestions=[_chip("Assign another application", "assign another application", icon="app"),
                     _chip("Finish onboarding", "finish onboarding", icon="check")],
    )


def _finish(session: Any, flow: dict[str, Any]) -> FlowResult:
    who = flow["firstName"]
    assigned = flow.get("assigned", [])
    session.metadata.pop("flow", None)
    if assigned:
        lines = "\n".join(f"- **{a}**" for a in assigned)
        return FlowResult(message=f"🎉 **{who}** is all set. Access granted:\n{lines}", done=True)
    return FlowResult(
        message=f"All done — **{who}**'s account is ready. You can assign access anytime just by asking.",
        done=True,
    )


# ── create-user guided intake ───────────────────────────────────────────────

async def _user_exists(user_name: str, headers: dict[str, str] | None) -> bool:
    try:
        res = await tool_registry.execute("getUserByUserName_get", {"userName": user_name}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        if isinstance(data, list):
            return bool(data)
        return bool(data)
    except Exception:  # noqa: BLE001
        return False


def start_create(session: Any) -> FlowResult:
    """Begin creating a user — first offer HOW: all at once, or one question at a time."""
    session.metadata["flow"] = {"name": "create_user", "stage": "mode", "data": {}}
    return FlowResult(
        message=("Let's set up a new user. You can give me everything in one message, or I can "
                 "ask one question at a time — whichever you prefer."),
        suggestions=[_chip("One question at a time", "one at a time", icon="list"),
                     _chip("I'll give it all at once", "all at once", icon="app")],
    )


async def _create(session: Any, flow: dict[str, Any], msg: str, headers: dict[str, str] | None) -> FlowResult:
    stage = flow.get("stage")
    data = flow.setdefault("data", {})

    if stage == "mode":
        low = msg.lower()
        if any(w in low for w in ("all at once", "one message", "everything", "at once", "bulk", "paste")):
            flow["stage"] = "bulk_wait"
            return FlowResult(message=(
                "Great — send it all in one message: **first name, last name, username, email, "
                "account type, and account category**.\n\n"
                "_Example: “Jane Doe, username JDOE, jane@agency.gov, Local account, Core account”._"))
        # default → guided one-by-one
        flow["stage"] = "first"
        return FlowResult(message="Let's begin. What's the new user's **first name**?")

    if stage == "bulk_wait":
        # Hand the detailed message to the normal skill path (LLM collects + validates).
        session.metadata.pop("flow", None)
        return None  # type: ignore[return-value]

    if stage == "first":
        if not msg:
            return FlowResult(message="What's the new user's **first name**?")
        data["firstName"] = msg
        flow["stage"] = "last"
        return FlowResult(message=f"Thanks. And **{msg}**'s **last name**?")

    if stage == "last":
        if not msg:
            return FlowResult(message="What's the **last name**?")
        data["lastName"] = msg
        flow["stage"] = "username"
        suggested = (data.get("firstName", "")[:1] + msg).upper().replace(" ", "")
        return FlowResult(
            message=f"What **username** should they sign in with? _(e.g. {suggested})_")

    if stage == "username":
        uname = msg.strip().upper().replace(" ", "")
        if not uname:
            return FlowResult(message="Please give me a **username**.")
        if await _user_exists(uname, headers):
            return FlowResult(message=f"**{uname}** is already taken. Please pick a different username.")
        data["userName"] = uname
        flow["stage"] = "email"
        return FlowResult(message=f"Got it — **{uname}**. What's their **email address**?")

    if stage == "email":
        email = msg.strip()
        if not _EMAIL_RE.match(email):
            return FlowResult(message="That doesn't look like a valid email. Please enter a valid **email address**.")
        data["emailAddress"] = email
        flow["stage"] = "type"
        return FlowResult(
            message=("What **account type**? _Local = internal staff · External = outside partner · "
                     "Global = cross-tenant · Stale = dormant._"),
            suggestions=[_chip(t, t, icon="app") for t in TYPE_CHIPS])

    if stage == "type":
        choice = _pick_label(msg, TYPE_CHIPS)
        if not choice:
            return FlowResult(message="Please pick an **account type**:",
                              suggestions=[_chip(t, t, icon="app") for t in TYPE_CHIPS])
        data["accountType"] = choice
        flow["stage"] = "category"
        return FlowResult(
            message=("And the **account category**? _Core = standard user · Service/API = system "
                     "accounts · Temporary/Testing = short-lived · Help & Support = support desk._"),
            suggestions=[_chip(c, c, icon="role") for c in CAT_CHIPS])

    if stage == "category":
        choice = _pick_label(msg, CAT_CHIPS)
        if not choice:
            return FlowResult(message="Please pick an **account category**:",
                              suggestions=[_chip(c, c, icon="role") for c in CAT_CHIPS])
        data["accountCategory"] = choice
        flow["stage"] = "confirm"
        who = f"{data.get('firstName','')} {data.get('lastName','')}".strip()
        summary = (f"Create user **{who}** — username **{data['userName']}**, {data['emailAddress']}, "
                   f"**{data['accountType']}** / **{data['accountCategory']}**. Shall I go ahead?")
        return FlowResult(message=summary,
                          pending={"tool_name": "addUser_post", "tool_args": dict(data), "summary": summary})

    # stage == "confirm" (awaiting the Confirm/Cancel buttons)
    return FlowResult(message="Please use **Confirm** or **Cancel** above to finish creating the user.")


def _pick_label(msg: str, labels: list[str]) -> str | None:
    m = msg.strip().lower()
    if not m:
        return None
    for lb in labels:
        if lb.lower() == m or lb.lower() in m or m in lb.lower():
            return lb
    # first word match (e.g. "local account" → "Local")
    first = m.split()[0] if m.split() else ""
    for lb in labels:
        if first and lb.lower().startswith(first):
            return lb
    return None


# ── create-skill guided intake (author a new skill at runtime) ─────────────────

def _slug(text: str) -> str:
    from . import skills as _sk
    base = re.sub(r"[^a-z0-9]+", "_", (text or "skill").strip().lower()).strip("_")[:32] or "skill"
    name, i = base, 2
    existing = {s.name for s in _sk.SKILLS}
    while name in existing:
        name, i = f"{base}_{i}", i + 1
    return name


def start_create_skill(session: Any, message: str = "") -> FlowResult:
    session.metadata["flow"] = {"name": "create_skill", "stage": "purpose", "data": {}}
    # Capture an inline purpose ("create a skill TO look up a license") so we don't re-ask.
    m = re.search(r"\bskill\b\s+(?:to|for|that|which|:)?\s*(.+)$", message or "", re.I)
    purpose = (m.group(1).strip().rstrip(".") if m else "")
    if len(purpose) >= 4:
        flow = session.metadata["flow"]
        flow["data"]["summary"] = purpose
        flow["stage"] = "keywords"
        return FlowResult(message=(f"Got it — a skill to **{purpose}**. What words or phrases "
                                   "should **trigger** it? List a few, comma-separated."))
    return FlowResult(message=(
        "Let's teach me a new skill. In one sentence, what should it **do**? "
        "_(e.g. “deactivate a user account”, “add a license to an organization”)_"))


def _skill_confirm(data: dict[str, Any]) -> FlowResult:
    req = ", ".join(data.get("required", [])) or "nothing extra"
    kws = ", ".join(data.get("keywords", []))
    return FlowResult(
        message=(f"Ready to create this skill:\n"
                 f"• **Does:** {data.get('summary','')}\n"
                 f"• **Triggers on:** {kws}\n"
                 f"• **Runs:** {data.get('tool','')}\n"
                 f"• **Asks the user for:** {req}\n\nCreate it?"),
        suggestions=[_chip("Create skill", "create skill", icon="check"),
                     _chip("Cancel", "cancel", icon="skip")])


def _finish_skill(session: Any, spec: dict[str, Any] | None) -> FlowResult:
    session.metadata.pop("flow", None)
    if not spec:
        return FlowResult(message="No problem — I didn't create the skill.", done=True)
    kw = (spec.get("keywords") or [spec["name"]])[0]
    return FlowResult(message=(f"✅ Done — I learned **{spec['name']}**. "
                               f"Try it by saying “{kw}”."), done=True)


async def _create_skill(session: Any, flow: dict[str, Any], msg: str, headers: dict[str, str] | None) -> FlowResult:
    from . import skill_store
    stage = flow.get("stage")
    data = flow.setdefault("data", {})

    if stage == "purpose":
        if not msg:
            return FlowResult(message="Describe in one sentence what the skill should do.")
        data["summary"] = msg.strip().rstrip(".")
        flow["stage"] = "keywords"
        return FlowResult(message=(f"Got it — “{data['summary']}”. What words or phrases should "
                                   "**trigger** it? List a few, comma-separated. "
                                   "_(e.g. deactivate user, disable account)_"))

    if stage == "keywords":
        kws = [k.strip().lower() for k in re.split(r"[,;]", msg) if k.strip()]
        if not kws:
            return FlowResult(message="Give me at least one trigger phrase (comma-separated).")
        data["keywords"] = kws
        flow["stage"] = "tool"
        sugg = await skill_store.suggest_tools(data.get("summary", ""), 6)
        data["_sugg"] = [s["name"] for s in sugg]
        if not sugg:
            return FlowResult(message="Which MCP tool should it run? Type the exact tool name.")
        lines = "\n".join(
            f"• **{s['name']}** — {s['desc'] or 'No description available.'}"
            + (f"  \n  _needs: {', '.join(s['required'])}_" if s.get("required") else "")
            for s in sugg)
        return FlowResult(
            message=(f"Which action should it run?\n\n{lines}\n\n"
                     "Tap a tool to use it, or type **about <tool>** to see full details first."),
            suggestions=[_chip(s["name"], s["name"], icon="app") for s in sugg])

    if stage == "tool":
        sugg_names = data.get("_sugg", [])
        # "about <tool>" / "details <tool>" / "what is <tool>" → show full detail, don't pick yet.
        m = re.match(r"^\s*(?:about|details?|more(?:\s+about)?|explain|what\s*'?s?\s*is|tell me about)\s+(.+)$",
                     msg, re.I)
        if m:
            tn = await skill_store.find_tool(m.group(1).strip())
            det = await skill_store.tool_detail(tn) if tn else None
            if det:
                needs = ", ".join(det["required"]) or "nothing extra"
                allp = ", ".join(det["params"]) or "—"
                others = [_chip(n, n, icon="app") for n in sugg_names if n != det["name"]]
                return FlowResult(
                    message=(f"**{det['name']}**\n{det['desc'] or 'No description available.'}\n\n"
                             f"_Required inputs: {needs}_\n_All inputs: {allp}_\n\n"
                             "Use this tool, or pick another below."),
                    suggestions=[_chip(f"Use {det['name']}", det["name"], icon="check")] + others)
            return FlowResult(message=f"I couldn't find a tool called “{m.group(1).strip()}”. Pick one below.",
                              suggestions=[_chip(n, n, icon="app") for n in sugg_names])
        picked = await skill_store.find_tool(msg)
        if not picked:
            return FlowResult(
                message=(f"I couldn't find a tool called “{msg}”. Pick one below, type an exact "
                         "tool name, or type **about <tool>** for details."),
                suggestions=[_chip(n, n, icon="app") for n in sugg_names])
        data["tool"] = picked
        data["required"] = await skill_store.tool_required_fields(picked)
        flow["stage"] = "review"
        reqtxt = ", ".join(data["required"]) if data["required"] else "nothing extra"
        return FlowResult(
            message=f"Using **{picked}**. It will ask the user for: **{reqtxt}**. Look right?",
            suggestions=[_chip("Looks good", "looks good", icon="check"),
                         _chip("Change the fields", "change fields", icon="list")])

    if stage == "review":
        low = msg.lower()
        if any(w in low for w in ("change", "adjust", "edit", "different")):
            flow["stage"] = "edit_fields"
            return FlowResult(message="Type the fields it should ask the user for, comma-separated _(or say “none”)_.")
        flow["stage"] = "confirm"
        return _skill_confirm(data)

    if stage == "edit_fields":
        if msg.strip().lower() in ("none", "no", ""):
            data["required"] = []
        else:
            data["required"] = [f.strip() for f in re.split(r"[,;]", msg) if f.strip()]
        flow["stage"] = "confirm"
        return _skill_confirm(data)

    if stage == "confirm":
        low = msg.lower()
        if any(w in low for w in ("cancel", "stop", "start over", "no")):
            return _finish_skill(session, None)
        spec = {
            "name": _slug(data.get("summary") or (data.get("keywords") or ["skill"])[0]),
            "keywords": data.get("keywords", []), "tool": data["tool"],
            "required": data.get("required", []), "summary": data.get("summary", ""),
            "hint": "", "defaults": {},
        }
        skill_store.save_custom_skill(spec)
        return _finish_skill(session, spec)

    return _skill_confirm(data)


# ── confirm-step callbacks (called by chat_service after the assign confirm) ────

def after_assign(session: Any) -> FlowResult | None:
    """The in-flow assign succeeded → record it and offer another application."""
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    app, role = flow.get("currentAppName"), flow.get("currentRoleName")
    if app and role:
        flow["assigned"].append(f"{app} · {role}")
    flow["currentAppId"] = flow["currentAppName"] = flow["currentRoleName"] = None
    flow["stage"] = "offer_apps"
    tick = f"✅ **{app} · {role}** assigned. " if (app and role) else ""
    return _offer_another(flow, prefix=tick)


def on_decline(session: Any) -> FlowResult | None:
    """The user declined the in-flow assign → drop this app, offer another."""
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    flow["currentAppId"] = flow["currentAppName"] = flow["currentRoleName"] = None
    flow["stage"] = "offer_apps"
    return _offer_another(flow, prefix="Okay, skipped that one. ")
