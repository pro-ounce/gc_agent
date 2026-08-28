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


def is_active(session: Any) -> bool:
    f = session.metadata.get("flow")
    return isinstance(f, dict) and f.get("name") == "onboard"


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


async def handle(session: Any, message: str, headers: dict[str, str] | None) -> FlowResult | None:
    """Advance the active flow for this turn. Returns None if no flow is active (fall
    through to normal routing) — including when the user pivots to another known task."""
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    msg = (message or "").strip()

    # Pivot escape: an unrelated known task (create another user, run a report) ends
    # onboarding and falls through to normal routing.
    from ..services import skills
    other = skills.match(msg)
    if other and other.name in ("create_user", "generate_report"):
        session.metadata.pop("flow", None)
        return None

    return await _onboard(session, flow, msg, headers)


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
