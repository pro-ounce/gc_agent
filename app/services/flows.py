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
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..commons.logger import get_logger
from ..mcp.tool_registry import tool_registry
from . import workflow as _wf


async def _wf_exec(tool: str, args: dict, headers: dict | None) -> dict:
    """ToolExec adapter for the node-graph engine — runs a tool via the registry (which applies
    the name→id resolvers on mutations) and hands back its {data,…} envelope. Carries the
    registry-level success flag as ``_ok`` so a Mutate node can tell a real write from a
    rejected one (a business error comes back HTTP-200 with success:false). Access-governing
    mutations are authority-gated here too, so a workflow (e.g. grant_access) can't write
    without the caller holding an admin role in Administration."""
    if is_access_mutation(tool):
        allowed, why = await authorize_onboarding(headers)
        if not allowed:
            return {"data": None, "message": why, "_ok": False, "_denied": True}
    res = await tool_registry.execute(tool, args, headers)
    out = res.output if isinstance(res.output, dict) else {"data": res.output}
    return {**out, "_ok": bool(getattr(res, "success", True))}


def _wf_to_fr(step: "_wf.RunStep") -> "FlowResult":
    # Always give the user a visible way out: a Cancel chip on every in-progress step
    # (unless the step already offers one). It sends "cancel" → the flow fail-safe exit.
    chips = [_chip(o) for o in step.options]
    if not step.done and not any(str(o).strip().lower() == "cancel" for o in step.options):
        chips.append(_chip("✕ Cancel", "cancel"))
    return FlowResult(message=step.message, suggestions=chips, done=step.done)


def _maybe_start_workflow(session: Any, message: str) -> "FlowResult | None":
    """Open a node-graph workflow if the message matches one's trigger. The first node must be
    an Ask (rendered synchronously here); async-start workflows are a later addition."""
    for w in _wf.REGISTRY.values():
        if re.search(w.trigger, message or "", re.I):
            run_id = f"{w.id}-{uuid.uuid4().hex[:12]}"
            state = {"wf": w.id, "node": w.start, "data": {}}
            session.metadata["flow"] = {"name": "workflow", "wf": w.id, "state": state,
                                        "run_id": run_id, "seq": 0}
            start_node = w.nodes[w.start]
            if isinstance(start_node, _wf.Ask):
                _wf_audit("run_started", run_id, wf=w.id, title=w.title)
                return _wf_to_fr(_wf._render(start_node, state["data"]))
            session.metadata.pop("flow", None)   # unsupported start shape → don't hijack
            return None
    return None

log = get_logger(__name__)


def _wf_audit(event: str, run_id: str, **fields: Any) -> None:
    """Structured workflow audit. Emits a run-level record (``run_id``) and per-node sub-records
    into the same structured log as the turn — which already carries session_id / request_id —
    so a workflow run ties to its nodes AND to the individual chat records."""
    log.bind(event=event, run_id=run_id, **fields).info(f"workflow {event}: {run_id}")


# ── New-intent break: don't let an active workflow swallow an unrelated request ──────
# A line that clearly opens a NEW request (a read-query, or another workflow's trigger) typed
# mid-flow should offer to switch — not be consumed as a step answer. Kept high-precision: these
# openers never look like a username / an app or role pick, so a real answer won't trip them.
_NEW_INTENT = re.compile(
    r"^\s*(who\s+can\s+access\b|who\s+has\s+access\b|roles?\s+in\b|about\b|"
    r"list\s+(the\s+)?(applications?|apps|roles?|users?|offices?|divisions?|organi[sz]ations?)\b|"
    r"show\s+(me\s+)?(the\s+)?(applications?|apps|roles?|users?|offices?|divisions?|organi[sz]ations?)\b|"
    r"my\s+access\b|what\s+can\s+\w|where\s+can\s+i\s+work\b|what\s+is\b|help\b)", re.I)
# Chip sentinels for the switch prompt — unambiguous, never a real step answer.
_SW_YES = re.compile(r"^__switch__$|^\s*(switch|yes|yep|yeah|go ahead|do that|the other one)\b", re.I)
_SW_NO = re.compile(r"^__stay__$|^\s*(stay|no|nope|continue|finish|keep going|this one)\b", re.I)


def _is_new_intent(msg: str, active_wf: Any) -> bool:
    """True when `msg` reads as a fresh request rather than an answer to the current step."""
    m = (msg or "").strip()
    if not m:
        return False
    for other in _wf.REGISTRY.values():        # another workflow's trigger → clearly a new intent
        if other.id != active_wf.id and re.search(other.trigger, m, re.I):
            return True
    return bool(_NEW_INTENT.search(m))


def _switch_offer(w: Any, msg: str) -> "FlowResult":
    """Ask whether to break out of the active workflow to handle a freshly-typed request."""
    q = msg.strip()
    q = (q[:70] + "…") if len(q) > 72 else q
    return FlowResult(
        message=f"You're partway through **{w.title}**. Switch to “{q}”, or finish this first?",
        suggestions=[_chip("↪ Switch to that", "__switch__"), _chip(f"Stay in {w.title}", "__stay__")])

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
    blocks: list[Any] = field(default_factory=list)  # structured UIBlocks (table/fields/list)
    reroute: str | None = None  # user broke out of the flow → answer THIS message as a fresh turn
    progress: dict[str, Any] | None = None  # workflow rail: {title, current, total, steps:[{label,value,state}]}
    ack: str | None = None      # a completed-prerequisite acknowledgement to surface as a check-row


def _chip(label: str, send: str | None = None, icon: str | None = None) -> dict[str, Any]:
    return {"label": label, "send": send if send is not None else label, "icon": icon}


_FLOWS = ("onboard", "create_user", "create_skill", "data_call", "formulation_baseline",
          "workflow", "entity_create", "remove_role")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SKILL_INTENT_RE = re.compile(
    r"\b(create|add|make|build|teach|define|register)\b.{0,20}\b(skill|capabilit(y|ies)|"
    r"new (action|command|ability))\b", re.I)
# Formulation baseline generation — a 13-field guided flow ending in one confirmed mutation.
# Matched deterministically so the mutation is ALWAYS flow-gated (never LLM-driven).
_BASELINE_INTENT_RE = re.compile(
    r"\b(generate|create|run|build|new|start)\b[^.?]{0,24}\bbaseline\b|"
    r"\bbaseline\b[^.?]{0,12}\b(generation|generate)\b|\bformulation baseline\b", re.I)

# ── Fail-safe: universal exit commands, honoured at ANY stage of ANY flow ───────
# Standalone commands (whole message ≈ one of these) always cancel.
_CANCEL_STANDALONE = {
    "cancel", "stop", "abort", "quit", "exit", "discard", "nevermind", "never mind",
    "forget it", "start over", "scrap it", "scrap this", "back out", "get me out",
    "cancel this", "cancel that", "cancel it",
}
# A leading cancel verb + a flow-referring object ("cancel the skill", "stop this") cancels;
# "cancel a subscription" (no flow object) does NOT, so it can still be a keyword/value.
_CANCEL_LEAD = re.compile(
    r"^\s*(?:please\s+|let'?s\s+|just\s+|i\s+want\s+to\s+|can\s+(?:you|we)\s+)?"
    r"(cancel|stop|abort|quit|exit|discard|forget|scrap|start\s+over|back\s+out)\b", re.I)
_CANCEL_OBJ = re.compile(
    r"\b(skill|user|onboard\w*|flow|creation|process|intake|this|that|it|everything|here)\b", re.I)


def _is_cancel(msg: str) -> bool:
    m = msg.strip().lower().rstrip("!. ")
    if m in _CANCEL_STANDALONE:
        return True
    return bool(_CANCEL_LEAD.match(msg) and _CANCEL_OBJ.search(msg))


def _cancel_flow(session: Any, flow: dict[str, Any]) -> FlowResult:
    """Fail-safe exit: drop the flow and any armed confirm; nothing is written."""
    name = flow.get("name")
    if name == "workflow":
        _wf_audit("run_cancelled", flow.get("run_id", flow.get("wf", "")), wf=flow.get("wf"))
    session.metadata.pop("flow", None)
    session.pending_action_id = None
    session.metadata.pop("pending_actions", None)
    what = {"onboard": "onboarding", "create_user": "creating the user",
            "create_skill": "creating the skill",
            "formulation_baseline": "generating the baseline"}.get(name, "that")
    return FlowResult(
        message=f"Okay — I've cancelled {what}. Nothing was saved. What would you like to do next?",
        done=True)

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


async def _menus(headers: dict[str, str] | None, app_id: Any) -> list[dict[str, Any]]:
    """Menus for an application as {id, name} — the attach-a-menu picker for role creation
    (getAllMenus_post{applicationId}, the same source the admin app's role form uses)."""
    out: list[dict[str, Any]] = []
    try:
        res = await tool_registry.execute("getAllMenus_post", {"applicationId": app_id}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        seen: set[str] = set()
        for m in (data or []):
            if not isinstance(m, dict) or str(m.get("enabled", "Y")).upper() == "N":
                continue
            name = str(m.get("menuName") or m.get("menuCode") or "").strip()
            if name and name.lower() not in seen and m.get("menuId") not in (None, ""):
                seen.add(name.lower())
                out.append({"id": m.get("menuId"), "name": name})
    except Exception as exc:  # noqa: BLE001
        log.bind(func="flow_menus").warning(f"menu list failed: {exc}")
    return out


# ── entity-create guardrails: code style + uniqueness ───────────────────────────

def _codeify(text: Any, maxlen: int = 0) -> str:
    """Normalize free text to a system code: UPPER_SNAKE, alnum only, trimmed to `maxlen`
    on an underscore boundary (so a truncation never ends mid-token or with a stray '_')."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(text or "")).strip("_").upper()
    if maxlen and len(s) > maxlen:
        s = s[:maxlen].rstrip("_")
    return s


def _unique_code(source: Any, taken: set[str], maxlen: int = 30) -> str:
    """A code derived from `source` that isn't already in `taken` (compared upper-cased).
    On collision, append _2, _3, … keeping the whole thing within `maxlen`."""
    base = _codeify(source, maxlen) or "CODE"
    if base not in taken:
        return base
    i = 2
    while i < 1000:
        suffix = f"_{i}"
        core = _codeify(source, max(1, maxlen - len(suffix))) if maxlen else _codeify(source)
        cand = f"{core}{suffix}"
        if cand not in taken:
            return cand
        i += 1
    return base  # give up gracefully; the backend's own constraint is the final authority


async def _role_index(headers: dict[str, str] | None, app_id: Any,
                      flow: dict[str, Any] | None = None) -> dict[str, Any]:
    """Existing role names (lower) + codes (upper) for one application — the uniqueness set for
    role creation. Cached on the flow for the turn so name + auto-code checks share one call.
    Fail-open (empty sets) if the list can't be read; the backend still enforces its constraint."""
    if flow is not None:
        c = (flow.get("_opts", {}) or {}).get("_role_idx")
        if isinstance(c, dict) and c.get("app") == app_id:
            return c
    names: set[str] = set()
    codes: set[str] = set()
    try:
        res = await tool_registry.execute("getApplicationRolesByAppId_get", {"applicationId": app_id}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        for r in (data or []):
            if not isinstance(r, dict):
                continue
            n = str(r.get("roleName") or "").strip().lower()
            c = str(r.get("role") or "").strip().upper()
            if n:
                names.add(n)
            if c:
                codes.add(c)
    except Exception as exc:  # noqa: BLE001 — a guardrail must never crash the turn
        log.bind(func="flow_role_index").warning(f"role index failed: {exc}")
    idx = {"app": app_id, "names": names, "codes": codes}
    if flow is not None:
        flow.setdefault("_opts", {})["_role_idx"] = idx
    return idx


async def _menu_index(headers: dict[str, str] | None, app_id: Any) -> dict[str, set[str]]:
    """Existing menu names (lower) + codes (upper) for one application."""
    names: set[str] = set()
    codes: set[str] = set()
    try:
        res = await tool_registry.execute("getAllMenus_post", {"applicationId": app_id}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        for m in (data or []):
            if not isinstance(m, dict):
                continue
            n = str(m.get("menuName") or "").strip().lower()
            c = str(m.get("menuCode") or "").strip().upper()
            if n:
                names.add(n)
            if c:
                codes.add(c)
    except Exception as exc:  # noqa: BLE001
        log.bind(func="flow_menu_index").warning(f"menu index failed: {exc}")
    return {"names": names, "codes": codes}


async def _taken_values(tag: str, data: dict[str, Any], headers: dict[str, str] | None,
                        flow: dict[str, Any] | None = None) -> set[str]:
    """The set of values already taken for a uniqueness `tag`, scoped to the target application
    where relevant. Names come back lower-cased, codes upper-cased (compare accordingly)."""
    app_id = (data or {}).get("applicationId")
    if tag == "app_code":
        return {str(a.get("code", "")).upper() for a in await _apps(headers) if a.get("code")}
    if tag in ("role_name", "role_code"):
        idx = await _role_index(headers, app_id, flow)
        return idx["names"] if tag == "role_name" else idx["codes"]
    if tag in ("menu_name", "menu_code"):
        idx = await _menu_index(headers, app_id)
        return idx["names"] if tag == "menu_name" else idx["codes"]
    return set()


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


def maybe_start(session: Any, message: str, skill: Any = None) -> FlowResult | None:
    """Start a guided flow from a fresh intent (when none is active). A create-user request
    WITHOUT enough detail (no email present) opens the guided intake; a fully-detailed
    'create user … email …' message is left to the normal skill path. `skill` is the
    already-resolved skill for this turn (keyword OR semantic match) — passed so a paraphrase
    like 'onboard a new person' opens the guided intake too."""
    if is_active(session):
        return None
    # Declarative node-graph workflows (workflow.py) get first look at the message.
    wf_started = _maybe_start_workflow(session, message or "")
    if wf_started:
        return wf_started
    # Skill authoring intent takes precedence ("create a skill" must not read as create-user).
    if _SKILL_INTENT_RE.search(message or ""):
        return start_create_skill(session, message or "")
    # Formulation baseline generation → open the guided flow (mutation stays flow-gated).
    if _BASELINE_INTENT_RE.search(message or ""):
        return start_formulation_baseline(session)
    # Remove a role definition → guided, dependency-previewed, confirm-gated delete. Detected
    # before the create front door; un-assigning a role from a user is NOT caught here.
    if _detect_remove_role(message or ""):
        return start_remove_role(session)
    # Entity onboarding front door: disambiguate onboard/create BEFORE a skill can pin a
    # wrong-entity mutation ("create a role" must not become assign_access; a bare "onboard"
    # must ask, not create a user). A fully-detailed create-user message (has an email) still
    # goes to the normal skill path so nothing regresses.
    ent = _detect_entity(message or "")
    if ent is not None:
        if ent == "user":
            if not re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", message or ""):
                return start_create(session)
        elif ent in _ENTITY_CREATE:
            return start_entity_create(session, ent)
        elif ent == "AMBIGUOUS":
            from ..services import skills as _sk
            other = _sk.match(message or "")
            if other is None or other.name in ("create_user",):
                return start_entity_create(session, None)   # ask which entity
    from ..services import skills
    sk = skill if skill is not None else skills.match(message or "")
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

    # FAIL-SAFE: a cancel/abort/stop command exits ANY flow at ANY stage, cleanly.
    if _is_cancel(msg):
        return _cancel_flow(session, flow)

    if name == "workflow":
        w = _wf.REGISTRY[flow["wf"]]
        run_id = flow.get("run_id", flow["wf"])

        # New-intent break. If we offered a switch last turn, resolve their choice; otherwise
        # detect a freshly-typed request and offer to switch instead of eating it as an answer.
        pend = flow.pop("_switch", None)
        if pend is not None:
            if _SW_YES.search(msg):
                _wf_audit("run_switched", run_id, wf=w.id, to=pend[:80])
                session.metadata.pop("flow", None)              # abandon this flow…
                return FlowResult(message="", done=True, reroute=pend)  # …and answer the new ask
            if _SW_NO.search(msg):
                return _wf_to_fr(_wf._render(w.nodes[flow["state"]["node"]], flow["state"]["data"],
                                             note="Okay — let's finish this. "))
            if _is_new_intent(msg, w):        # they typed yet another new request → re-offer
                flow["_switch"] = msg
                return _switch_offer(w, msg)
            # otherwise it reads like a real answer → fall through and apply it (pend dropped)
        elif _is_new_intent(msg, w):
            flow["_switch"] = msg
            return _switch_offer(w, msg)

        step = await _wf.advance(w, flow["state"], msg, _wf_exec, headers)
        for nid in step.trace:                       # per-node sub-records under the run
            flow["seq"] = flow.get("seq", 0) + 1
            nd = w.nodes.get(nid)
            _wf_audit("node", run_id, seq=flow["seq"], node_id=nid,
                      node_type=type(nd).__name__ if nd else "?", turn_input=msg[:80])
        if step.done:
            _wf_audit("run_completed", run_id, wf=w.id)
            session.metadata.pop("flow", None)
        return _wf_to_fr(step)

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

    if name == "data_call":
        return await _data_call(session, flow, msg, headers)

    if name == "formulation_baseline":
        return await _baseline(session, flow, msg, headers)

    if name == "entity_create":
        fr = await _entity_create(session, flow, msg, headers)
        return _ep(fr, session.metadata.get("flow"))

    if name == "remove_role":
        return await _remove_role(session, flow, msg, headers)

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


# ── Formulation data-call flow: create → attendees → reminder ───────────────────

async def fetch_data_call_id(tool_args: dict[str, Any],
                             headers: dict[str, str] | None) -> tuple[Any, Any]:
    """saveDataCalls returns no id, so fetch the just-created call by title + fiscalYear
    and take the newest match — needed to thread dataCallId into attendees/reminder.
    Returns (dataCallId, applicationId); the row's applicationId scopes attendee groups."""
    title = str((tool_args or {}).get("title") or "").strip().lower()
    fy = (tool_args or {}).get("fiscalYear")
    try:
        res = await tool_registry.execute("dataCallsByFiscalYear_get", {"fiscalYear": fy}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        rows = [r for r in (data or []) if isinstance(r, dict)]
        match = [r for r in rows if str(r.get("title") or "").strip().lower() == title] or rows
        if not match:
            return None, None
        best = max(match, key=lambda r: int(str(r.get("dataCallId") or 0) or 0))
        return best.get("dataCallId"), best.get("applicationId")
    except Exception as exc:  # noqa: BLE001
        log.bind(func="fetch_data_call_id").warning(f"lookup failed: {exc}")
        return None, None


async def _dist_groups(headers: dict[str, str] | None,
                       application_id: Any = None) -> list[dict[str, Any]]:
    """Distribution groups, scoped to the data call's application when known (groups are
    per-application; offering another app's groups would be a mistake). Falls back to all
    groups if the scope yields nothing (so the flow never dead-ends)."""
    out: list[dict[str, Any]] = []
    app = str(application_id) if application_id not in (None, "") else None
    try:
        res = await tool_registry.execute("getAllDistributionGroups_get", {}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        scoped: list[dict[str, Any]] = []
        for r in (data or []):
            if not isinstance(r, dict) or str(r.get("enabled", "Y")).upper() == "N":
                continue
            name = str(r.get("groupName") or r.get("groupCode") or "").strip()
            if not name or r.get("distributionGroupId") in (None, ""):
                continue
            entry = {"id": r["distributionGroupId"], "name": name,
                     "app": str(r.get("applicationId") or "")}
            out.append(entry)
            if app and entry["app"] == app:
                scoped.append(entry)
        if app and scoped:
            return scoped
    except Exception as exc:  # noqa: BLE001
        log.bind(func="dist_groups").warning(f"group list failed: {exc}")
    return out


def start_data_call(session: Any, data_call_id: Any, title: str | None,
                    application_id: Any = None) -> FlowResult:
    """Armed after a data call is created — guide adding attendees + a reminder."""
    who = title or "the data call"
    if not data_call_id:   # couldn't resolve the id → can't guide the rest; just confirm
        session.metadata.pop("flow", None)
        return FlowResult(message=f"✅ **{who}** is set up.", done=True)
    session.metadata["flow"] = {
        "name": "data_call", "dataCallId": data_call_id, "title": who,
        "applicationId": application_id, "stage": "offer_attendees", "added": [],
    }
    return FlowResult(
        message=f"✅ Data call **{who}** created. Would you like to add attendees (a distribution group)?",
        suggestions=[_chip("Add attendees", "add attendees", icon="app"),
                     _chip("Not now", "not now", icon="skip")],
    )


async def _prompt_groups(flow: dict[str, Any], headers: dict[str, str] | None,
                         groups: list[dict[str, Any]] | None = None) -> FlowResult:
    groups = groups if groups is not None else await _dist_groups(headers, flow.get("applicationId"))
    if not groups:
        return FlowResult(message=f"Which distribution group should attend **{flow['title']}**? Type its name.",
                          suggestions=[_chip("Not now", "not now", icon="skip")])
    chips, note = _list_chips(groups, "app", [_chip("Not now", "not now", icon="skip")])
    return FlowResult(message=f"Which distribution group should attend **{flow['title']}**?{note}", suggestions=chips)


def _dc_offer_reminder(flow: dict[str, Any], prefix: str = "") -> FlowResult:
    flow["stage"] = "offer_reminder"
    return FlowResult(
        message=f"{prefix}Would you like to schedule a reminder for **{flow['title']}**?",
        suggestions=[_chip("Add a reminder", "add a reminder", icon="check"),
                     _chip("Finish", "finish", icon="check")],
    )


def _dc_finish(session: Any, flow: dict[str, Any]) -> FlowResult:
    title = flow.get("title"); added = flow.get("added", [])
    session.metadata.pop("flow", None)
    extra = ("\n" + "\n".join(f"- {a}" for a in added)) if added else ""
    return FlowResult(message=f"🎉 **{title}** is ready.{extra}", done=True)


async def _data_call(session: Any, flow: dict[str, Any], msg: str, headers: dict[str, str] | None) -> FlowResult:
    stage = flow.get("stage"); title = flow.get("title")

    if stage == "offer_attendees":
        if _NO.search(msg) and not _YES.search(msg):
            return _dc_offer_reminder(flow)
        flow["stage"] = "pick_group"
        return await _prompt_groups(flow, headers)

    if stage == "pick_group":
        groups = await _dist_groups(headers, flow.get("applicationId"))
        if _NO.search(msg) and not _match(msg, groups):
            return _dc_offer_reminder(flow, prefix="No problem. ")
        g = _match(msg, groups)
        if g:
            flow["currentGroup"] = g["name"]
            args = {"dataCallId": flow["dataCallId"], "distributionGroupId": g["id"],
                    "enabled": "Y", "versionNumber": 1}
            summary = f"Add attendees **{g['name']}** to **{title}**. Shall I go ahead?"
            return FlowResult(message=summary,
                              pending={"tool_name": "saveDataCallDistributions_post",
                                       "tool_args": args, "summary": summary})
        return await _prompt_groups(flow, headers, groups=groups)

    if stage == "offer_reminder":
        if _NO.search(msg) and not _YES.search(msg):
            return _dc_finish(session, flow)
        flow["stage"] = "pick_days"
        return FlowResult(
            message=f"How many days before the due date should the reminder go out?",
            suggestions=[_chip("3 days", "3 days", icon="check"), _chip("7 days", "7 days", icon="check"),
                         _chip("Not now", "not now", icon="skip")])

    if stage == "pick_days":
        mo = re.search(r"\d+", msg)
        if _NO.search(msg) and not mo:
            return _dc_finish(session, flow)
        if mo:
            days = int(mo.group())
            flow["currentDays"] = days
            args = {"dataCallId": flow["dataCallId"], "daysToRemind": days,
                    "emailSubject": f"Reminder: {title}", "enabled": "Y", "versionNumber": 1}
            summary = f"Schedule a reminder **{days} days** before, for **{title}**. Shall I go ahead?"
            return FlowResult(message=summary,
                              pending={"tool_name": "saveDataCallReminder_post",
                                       "tool_args": args, "summary": summary})
        return FlowResult(message="How many days before? e.g. 3")

    return _dc_finish(session, flow)


def dc_after_attendee(session: Any) -> FlowResult | None:
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    g = flow.pop("currentGroup", None)
    if g:
        flow.setdefault("added", []).append(f"Attendees: {g}")
    return _dc_offer_reminder(flow, prefix=f"✅ Added **{g}**. " if g else "")


def dc_after_reminder(session: Any) -> FlowResult | None:
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    d = flow.pop("currentDays", None)
    if d:
        flow.setdefault("added", []).append(f"Reminder: {d} days before")
    return _dc_finish(session, flow)


# ── Generic declarative picker-flow (Formulation baseline generation) ───────────
# A small step-driven engine: collect a list of fields ONE BY ONE (fetched pickers,
# multi-select, static options, or free text) and, only after the user confirms, run a
# SINGLE mutation. Kept declarative so future formulation actions add a spec, not code.

@dataclass(frozen=True)
class PickerStep:
    field: str                       # payload key this step fills
    prompt: str                      # question shown to the user
    source_tool: str = ""            # read tool that supplies options (else static/free-text)
    label_field: str = ""            # option's display key in each row
    value_field: str = ""            # option's payload key in each row
    multi: bool = False              # accumulate multiple selections
    optional: bool = False           # allow skip / "select all"
    static_options: tuple[str, ...] = ()  # fixed choices (no fetch), e.g. Y/N or Q1..Q4
    source_args: dict[str, Any] = field(default_factory=dict)


# VERIFY against live responses (scratchpad/baseline_probe.py) — source label/value field
# names are per the product spec; if a picker renders blank, correct the field here.
BASELINE_STEPS: tuple[PickerStep, ...] = (
    # Show the year, send its fiscalYearId (the payload keys off the id, not the year number).
    PickerStep("sourceFiscalYear", "Choose the **source** fiscal year (BFY)",
               "getAllFiscalYears_post", "fiscalYear", "fiscalYearId"),
    PickerStep("fiscalYear", "Choose the **target** fiscal year",
               "getAllFiscalYears_post", "fiscalYear", "fiscalYearId"),
    PickerStep("businessRuleGroups", "Choose the **baseline** business rule group(s)",
               "getAllValidationGroups_post", "groupName", "validationGroupId", multi=True,
               source_args={"groupType": "BASELINE", "applicationId": 3}),
    PickerStep("baselineTitle", "Enter a **title** for this baseline"),
    PickerStep("description", "Enter a short **description**"),
    PickerStep("orgIds", "Choose program office(s) — or select all",
               "getOrganizationsByUser_post", "organizationName", "organizationId",
               multi=True, optional=True, source_args={"applicationId": 3}),
    PickerStep("requestTypes", "Choose request type(s) — or select all",
               "getActiveRequestTypeMappings_post", "requestTypeName", "requestTypeCode",
               multi=True, optional=True),
    PickerStep("acquisitionVehicles", "Choose acquisition vehicle(s) — or select all",
               "getActiveAcquisitionVehicleMappingsIds_post",
               "acquisitionVehicleName", "acquisitionVehicleId", multi=True, optional=True),
    PickerStep("quarter", "Choose a quarter _(optional)_",
               static_options=("Q1", "Q2", "Q3", "Q4"), optional=True),
    PickerStep("isTargetApplicable", "Is **target** applicable?", static_options=("Y", "N")),
    PickerStep("isRecommended", "Include **recommended** requests?", static_options=("Y", "N")),
    PickerStep("isDeferred", "Include **deferred** requests?", static_options=("Y", "N")),
    PickerStep("isWithdrawn", "Include **withdrawn** requests?", static_options=("Y", "N")),
)

# Backend-mandatory fields with safe defaults (VERIFY fundGroupId / quarter policy with the
# product owner). Empty optional lists mean "all" per the baseline contract.
BASELINE_DEFAULTS: dict[str, Any] = {
    "applicationCode": "FORMULATION", "withdrawnRequest": "N", "deletedRequest": "N",
    "fundGroupId": 0, "quarter": "", "orgIds": [], "requestTypes": [], "acquisitionVehicles": [],
}
BASELINE_LABELS: dict[str, str] = {
    "sourceFiscalYear": "Source FY", "fiscalYear": "Target FY",
    "businessRuleGroups": "Rule group(s)", "baselineTitle": "Title", "description": "Description",
    "orgIds": "Program office(s)", "requestTypes": "Request type(s)",
    "acquisitionVehicles": "Acquisition vehicle(s)", "quarter": "Quarter",
    "isTargetApplicable": "Target applicable", "isRecommended": "Include recommended",
    "isDeferred": "Include deferred", "isWithdrawn": "Include withdrawn",
}

_DONE_RE = re.compile(r"\b(done|that'?s all|thats all|finish(ed)?|no more|next|proceed|complete|that is all)\b", re.I)
_ALL_RE = re.compile(r"\b(all|everything|every one|everyone|entire|any)\b", re.I)
_SKIP_RE = re.compile(r"\b(skip|none|not now|later|no thanks|n/?a|leave (it )?(blank|empty))\b", re.I)


def _yn(msg: str) -> str | None:
    """Map an affirmative/negative line to Y / N (None if unclear)."""
    if re.search(r"\b(y|yes|yeah|yep|true|include|applicable|do)\b", msg, re.I):
        return "Y"
    if re.search(r"\b(n|no|nope|false|exclude|don'?t|do not|not applicable)\b", msg, re.I):
        return "N"
    return None


async def _fetch_options(step: PickerStep, headers: dict[str, str] | None) -> list[dict[str, Any]]:
    """A step's options as [{value, label}] — defensive about field names and row shapes."""
    if step.static_options:
        return [{"value": o, "label": ("Yes" if o == "Y" else "No" if o == "N" else o)}
                for o in step.static_options]
    if not step.source_tool:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        res = await tool_registry.execute(step.source_tool, dict(step.source_args), headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        for r in (data or []):
            if isinstance(r, dict):
                if str(r.get("enabled", "Y")).upper() == "N":
                    continue
                val = r.get(step.value_field)
                lab = r.get(step.label_field)
                if val is None and lab is None:   # field-name miss → first usable scalar
                    scal = [v for v in r.values() if isinstance(v, (str, int, float)) and str(v).strip()]
                    if not scal:
                        continue
                    val = lab = scal[0]
                value = val if val is not None else lab
                label = str(lab if lab is not None else val)
            else:                                  # scalar list (e.g. plain fiscal years)
                value, label = r, str(r)
            key = str(value)
            if not label.strip() or key in seen:
                continue
            seen.add(key)
            out.append({"value": value, "label": label})
    except Exception as exc:  # noqa: BLE001 — a picker must never crash the turn
        log.bind(func="baseline_options", tool=step.source_tool).warning(f"option fetch failed: {exc}")
    return out


def _match_opt(msg: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a line to one option by label or value — exact, then unambiguous contains."""
    m = msg.strip().lower()
    if not m:
        return None
    for o in options:
        if m in (str(o["label"]).lower(), str(o["value"]).lower()):
            return o
    hits = [o for o in options
            if str(o["label"]).lower() in m or m in str(o["label"]).lower()]
    return hits[0] if len(hits) == 1 else None


def start_formulation_baseline(session: Any) -> FlowResult:
    """Begin the guided baseline-generation intake (sync: no fetch until the user starts)."""
    session.metadata["flow"] = {
        "name": "formulation_baseline", "stage": "intro", "idx": 0,
        "data": {}, "labels": {}, "buf": [],
    }
    return FlowResult(
        message=("Let's generate a **FORMULATION** budget baseline. I'll ask a few questions "
                 "one at a time, then show everything for your confirmation before anything runs."),
        suggestions=[_chip("Start", "start", icon="check"), _chip("Cancel", "cancel", icon="skip")],
    )


def _baseline_control_chips(step: PickerStep, has_buf: bool) -> list[dict[str, Any]]:
    tail: list[dict[str, Any]] = []
    if step.multi:
        tail.append(_chip("Done selecting" if has_buf else "Done", "done", icon="check"))
        if step.optional:
            tail.append(_chip("Select all", "select all", icon="app"))
    if step.optional and not step.multi:
        tail.append(_chip("Skip", "skip", icon="skip"))
    return tail


async def _baseline_prompt(flow: dict[str, Any], step: PickerStep,
                           headers: dict[str, str] | None, prefix: str = "",
                           options: list[dict[str, Any]] | None = None) -> FlowResult:
    """Render the current step: fetched/static chips (+ controls), or a free-text ask."""
    if not step.source_tool and not step.static_options:      # free text
        return FlowResult(message=f"{prefix}{step.prompt}")
    options = options if options is not None else await _fetch_options(step, headers)
    flow.setdefault("opts", {})[step.field] = options          # cache for matching/summary
    if not options:
        return FlowResult(message=f"{prefix}{step.prompt} _(type a value)_",
                          suggestions=_baseline_control_chips(step, bool(flow.get("buf"))))
    icon = "role" if step.multi else "app"
    chips = [_chip(str(o["label"]), str(o["label"]), icon=icon) for o in options[:MAX_CHIPS]]
    note = f"\n\n_Showing {MAX_CHIPS} of {len(options)} — or type a name._" if len(options) > MAX_CHIPS else ""
    return FlowResult(message=f"{prefix}{step.prompt}{note}",
                      suggestions=chips + _baseline_control_chips(step, bool(flow.get("buf"))))


def _baseline_summary(flow: dict[str, Any]) -> str:
    labels = flow.get("labels", {})
    lines = []
    for f in BASELINE_CONFIRM_FIELDS:
        val = labels.get(f)
        if val in (None, "", [], {}):
            val = "_(all)_" if f in ("orgIds", "requestTypes", "acquisitionVehicles") else "—"
        elif isinstance(val, list):
            val = ", ".join(str(x) for x in val) or "_(all)_"
        lines.append(f"- **{BASELINE_LABELS.get(f, f)}:** {val}")
    body = "\n".join(lines)
    return ("Ready to generate this **FORMULATION baseline**:\n"
            f"{body}\n\nShall I go ahead?")


BASELINE_CONFIRM_FIELDS = tuple(s.field for s in BASELINE_STEPS)


def _baseline_finalize(flow: dict[str, Any]) -> FlowResult:
    """Build the payload from collected values + defaults and hand off the single mutation."""
    args: dict[str, Any] = {**BASELINE_DEFAULTS, **flow.get("data", {})}
    summary = _baseline_summary(flow)
    return FlowResult(message=summary,
                      pending={"tool_name": "bobBaseLineGenerate_put", "tool_args": args,
                               "summary": "Generate a FORMULATION budget baseline"})


async def _advance_baseline(flow: dict[str, Any], headers: dict[str, str] | None,
                            prefix: str = "") -> FlowResult:
    """Move to the next step and prompt it; finalize (confirm hand-off) when past the last."""
    flow["idx"] += 1
    flow["buf"] = []
    if flow["idx"] >= len(BASELINE_STEPS):
        flow["stage"] = "confirm"
        return _baseline_finalize(flow)
    return await _baseline_prompt(flow, BASELINE_STEPS[flow["idx"]], headers, prefix=prefix)


async def _baseline(session: Any, flow: dict[str, Any], msg: str,
                    headers: dict[str, str] | None) -> FlowResult:
    if flow.get("stage") == "intro":
        if _NO.search(msg) and not _YES.search(msg):
            return _cancel_flow(session, flow)
        flow["stage"] = "collect"
        flow["idx"] = 0
        return await _baseline_prompt(flow, BASELINE_STEPS[0], headers)

    if flow.get("stage") != "collect":
        # awaiting Confirm/Cancel buttons
        return FlowResult(message="Please use **Confirm** or **Cancel** above to finish.")

    step = BASELINE_STEPS[flow["idx"]]

    # ── free-text step ──
    if not step.source_tool and not step.static_options:
        text = msg.strip()
        if not text:
            return FlowResult(message=step.prompt)
        flow["data"][step.field] = text
        flow["labels"][step.field] = text
        return await _advance_baseline(flow, headers)

    # ── static single-choice (Y/N, quarter) ──
    if step.static_options and not step.multi:
        if step.optional and _SKIP_RE.search(msg) and not _match_opt(msg, await _fetch_options(step, headers)):
            flow["data"].pop(step.field, None)          # leave default
            flow["labels"][step.field] = "—"
            return await _advance_baseline(flow, headers)
        if set(step.static_options) == {"Y", "N"}:
            yn = _yn(msg)
            if yn is None:
                return await _baseline_prompt(flow, step, headers, prefix="Please choose Yes or No. ")
            flow["data"][step.field] = yn
            flow["labels"][step.field] = "Yes" if yn == "Y" else "No"
            return await _advance_baseline(flow, headers)
        opt = _match_opt(msg, await _fetch_options(step, headers))
        if not opt:
            return await _baseline_prompt(flow, step, headers, prefix="Please pick one. ")
        flow["data"][step.field] = opt["value"]
        flow["labels"][step.field] = opt["label"]
        return await _advance_baseline(flow, headers)

    # ── fetched picker (single or multi) ──
    options = await _fetch_options(step, headers)

    if step.multi:
        buf: list[dict[str, Any]] = flow.setdefault("buf", [])
        if step.optional and _ALL_RE.search(msg) and not _match_opt(msg, options):
            flow["data"][step.field] = []               # empty == all
            flow["labels"][step.field] = "All"
            return await _advance_baseline(flow, headers)
        if _DONE_RE.search(msg) or (step.optional and _SKIP_RE.search(msg)):
            if not buf:
                if step.optional:
                    flow["data"][step.field] = []
                    flow["labels"][step.field] = "All"
                    return await _advance_baseline(flow, headers)
                return await _baseline_prompt(flow, step, headers,
                                              prefix="Pick at least one, then choose Done. ", options=options)
            flow["data"][step.field] = [b["value"] for b in buf]
            flow["labels"][step.field] = [b["label"] for b in buf]
            return await _advance_baseline(flow, headers)
        # accept one or more (comma-separated) selections this turn
        added = []
        for part in re.split(r"[,;]|\band\b", msg):
            opt = _match_opt(part, options)
            if opt and all(str(b["value"]) != str(opt["value"]) for b in buf):
                buf.append(opt)
                added.append(opt["label"])
        if not added:
            return await _baseline_prompt(flow, step, headers,
                                          prefix="I didn't catch that — pick from the list. ", options=options)
        chosen = ", ".join(b["label"] for b in buf)
        return await _baseline_prompt(flow, step, headers,
                                      prefix=f"Added **{', '.join(added)}** (so far: {chosen}). Add more or choose Done. ",
                                      options=options)

    # single fetched picker
    if step.optional and _SKIP_RE.search(msg) and not _match_opt(msg, options):
        flow["labels"][step.field] = "—"
        return await _advance_baseline(flow, headers)
    opt = _match_opt(msg, options)
    if not opt:
        return await _baseline_prompt(flow, step, headers, prefix="Please pick one. ", options=options)
    flow["data"][step.field] = opt["value"]
    flow["labels"][step.field] = opt["label"]
    return await _advance_baseline(flow, headers)


def baseline_after_generate(session: Any) -> FlowResult | None:
    """Baseline mutation confirmed & run → finish the flow with a friendly result."""
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    title = (flow.get("data", {}) or {}).get("baselineTitle") or "the baseline"
    session.metadata.pop("flow", None)
    return FlowResult(
        message=(f"🎉 **{title}** has been submitted for generation. "
                 "Baseline runs process in the background — check the FORMULATION baselines "
                 "screen for status."),
        done=True)


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
    """The user declined an in-flow confirm → resume the flow at the next natural step."""
    if not is_active(session):
        return None
    flow = session.metadata["flow"]
    if flow.get("name") == "formulation_baseline":
        # The single mutation is the whole point — declining it cancels the run cleanly.
        session.metadata.pop("flow", None)
        return FlowResult(message="Okay — I won't generate the baseline. Nothing was saved.", done=True)
    if flow.get("name") == "data_call":
        flow.pop("currentGroup", None); flow.pop("currentDays", None)
        # declining an attendee → still offer a reminder; declining a reminder → finish
        return _dc_offer_reminder(flow, prefix="Okay, skipped. ") if flow.get("stage") == "pick_group" \
            else _dc_finish(session, flow)
    if flow.get("name") == "entity_create":
        # the single create is the whole point — declining cancels the run cleanly
        label = _ENTITY_CREATE.get(flow.get("entity", ""), {}).get("label", "item")
        session.metadata.pop("flow", None)
        return FlowResult(message=f"Okay — I won't create the {label}. Nothing was saved.", done=True)
    flow["currentAppId"] = flow["currentAppName"] = flow["currentRoleName"] = None
    flow["stage"] = "offer_apps"
    return _offer_another(flow, prefix="Okay, skipped that one. ")


# ── Entity onboarding: create user / application / role / menu / privilege ───────
# "Onboard" and "create/add/set up" are overloaded across platform entities. This front
# door DISAMBIGUATES the entity first — an ambiguous request asks which entity rather than
# firing a wrong-entity mutation — then runs a small, confirm-gated create flow for the one
# the user actually meant. Each spec is declarative: the backing MCP tool, the fields to
# collect (text / app-picker / role-picker / yes-no), and safe defaults for the rest.

@dataclass(frozen=True)
class EField:
    key: str
    prompt: str
    kind: str = "text"                 # text | app_picker | role_picker | yesno
    optional: bool = False
    suggest_from: str = ""             # code field: propose UPPER(other field) as a default
    unique_among: str = ""             # legacy alias for `unique` (kept for back-compat)
    # ── guardrails ──────────────────────────────────────────────────────────────
    auto_from: str = ""                # AUTO-generate a code from this other field (UPPER_SNAKE),
                                       #   never prompted — computed + de-duped as we advance.
    maxlen: int = 0                    # reject free text longer than this (DB column width)
    is_code: bool = False              # normalize entered value to UPPER_SNAKE before storing
    unique: str = ""                   # uniqueness rule: app_code | role_name | role_code |
                                       #   menu_name | menu_code (scoped to the app where relevant)


# Field lists + defaults VERIFIED against the administration entity contracts (NOT-NULL /
# unique columns) and live records, 2026-09-28. addApplication marks only Authorization
# "required" in its schema, but the Application entity makes applicationName/Code/Order/
# description/Url/Icon NOT-NULL (Code + Order also unique) — so those are collected, order is
# auto-assigned (max+10, unique), and appState/appType are left EMPTY (every live app has "").
_ENTITY_CREATE: dict[str, dict[str, Any]] = {
    "application": {
        "label": "application", "tool": "addApplication_post",
        "fields": (
            EField("applicationName", "What's the **application name**?", maxlen=80),
            EField("applicationCode", "A unique **application code**? _(e.g. BUDGET_ANALYTICS)_",
                   suggest_from="applicationName", is_code=True, maxlen=30, unique="app_code"),
            EField("applicationShortCode", "A brief **short code / abbreviation**? _(e.g. BA)_", maxlen=30),
            EField("description", "A one-line **description**?", maxlen=4000),
            EField("applicationUrl", "The app **URL path**? _(e.g. /budget-analytics)_"),
            EField("applicationIcon", "The **icon path**? _(e.g. /assets/images/appsIcons/ba.svg)_"),
            EField("appCategory", "A **category**? _(e.g. Planner, Analytics)_", optional=True),
        ),
        "defaults": {"enabled": "Y", "isDefault": "N", "appState": "", "appType": "",
                     "applicationVersion": "1"},
        "auto_order": True,   # applicationOrder = max(existing)+10, computed at finalize (unique)
    },
    "role": {
        "label": "application role", "tool": "addApplicationRole_post",
        "fields": (
            EField("applicationId", "Which **application** is this role for?", kind="app_picker"),
            EField("roleName", "What's the role's **display name**? _(e.g. Budget Viewer)_",
                   maxlen=80, unique="role_name"),
            # Role code is AUTO-generated from the role name (UPPER_SNAKE, ≤30, de-duped within the
            # app) — never asked. Kept in the spec so it shows on the rail + confirmation.
            EField("role", "", auto_from="roleName", is_code=True, maxlen=30, unique="role_code"),
            EField("roleDescription", "A one-line **description** of the role?", maxlen=1000),
            EField("isAdmin", "Is this an **admin** role?", kind="yesno"),
            # A role must carry an attached menu (ApplicationRole.menuId). Required — pick from
            # the app's menus, or type a menu name.
            EField("menuId", "Which **menu** should this role open? _(the role's attached menu)_",
                   kind="menu_picker"),
        ),
        "defaults": {"enabled": "Y", "isChatbot": "N"},
    },
    "menu": {
        "label": "menu", "tool": "addMenu_post",
        "fields": (
            EField("applicationId", "Which **application** is this menu for?", kind="app_picker"),
            EField("menuName", "What's the **menu name**?", maxlen=120, unique="menu_name"),
            EField("menuCode", "A short **menu code**?", suggest_from="menuName",
                   is_code=True, maxlen=120, unique="menu_code"),
            EField("menuType", "What's the **menu type**? _(e.g. STANDARD)_", maxlen=50),
            EField("menuDesc", "A short **description**?", optional=True, maxlen=255),
        ),
        "defaults": {"enabled": "Y"},
    },
    "privilege": {
        "label": "role privilege", "tool": "addAppRolePrivilege_post",
        "fields": (
            EField("applicationRoleId", "Which **role** should this privilege apply to?", kind="role_picker"),
            EField("readFlag", "Allow **read**?", kind="yesno"),
            EField("writeFlag", "Allow **write / create**?", kind="yesno"),
            EField("updateFlag", "Allow **update**?", kind="yesno"),
            EField("deleteFlag", "Allow **delete**?", kind="yesno"),
        ),
        "defaults": {"enabled": "Y"},
    },
}

# Entity detection for the onboard/create front door. Requires a CREATE verb; bails on
# assign/grant/read verbs and on other known tasks (data call / skill / baseline / report),
# which are routed by their own handlers.
_EC_VERB = re.compile(r"\b(onboard|create|add|register|provision|make|set\s?up|new)\b", re.I)
_EC_NOT = re.compile(r"\b(access|assign|grant|give|remove|revoke|unassign|delete|report|"
                     r"data\s?call|skill|baseline|workflow|report)\b", re.I)
_EC_ENTITY: tuple[tuple[str, str], ...] = (
    ("user", r"\b(users?|someone|person|people|employees?|staff|new\s+hire|account)\b"),
    ("application", r"\b(applications?|apps?|modules?)\b"),
    ("role", r"\broles?\b"),
    ("menu", r"\bmenus?\b"),
    ("privilege", r"\b(privileges?|permissions?)\b"),
)


def _detect_entity(msg: str) -> str | None:
    """Classify an onboard/create request → an entity key, "AMBIGUOUS", or None (not ours).
    None means "not an entity-create intent" (let normal routing handle it)."""
    m = msg or ""
    if not _EC_VERB.search(m) or _EC_NOT.search(m):
        return None
    # "application role" / "app role" is a ROLE, not two entities.
    if re.search(r"\b(application|app)\s+roles?\b", m, re.I):
        return "role"
    hits = [e for e, p in _EC_ENTITY if re.search(p, m, re.I)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "AMBIGUOUS"
    # A create verb but no entity noun → only treat clear onboarding/new phrasings as ambiguous
    # (so "create a data call" etc., already excluded above, never reach here as a false ask).
    if re.search(r"\bonboard\b|\bnew\b", m, re.I):
        return "AMBIGUOUS"
    return None


_ENTITY_CHIP_ORDER = ("user", "application", "role", "menu", "privilege")
_ENTITY_CHIP_LABEL = {"user": "User", "application": "Application", "role": "Role",
                      "menu": "Menu", "privilege": "Privilege"}


def _disambiguate_entity() -> FlowResult:
    chips = [_chip(_ENTITY_CHIP_LABEL[e], e, icon="app") for e in _ENTITY_CHIP_ORDER]
    chips.append(_chip("Cancel", "cancel", icon="skip"))
    return FlowResult(
        message=("**Onboard what?** I can set up a few different things — tell me which and I'll "
                 "guide you:\n• **User** — a new account (and its access)\n• **Application** — a "
                 "new application/module\n• **Role** — a new application role\n• **Menu** — a new "
                 "menu\n• **Privilege** — read/write/update/delete on a role"),
        suggestions=chips)


def start_entity_create(session: Any, entity: str | None) -> FlowResult:
    """Front door for entity onboarding. `entity` known → its create flow; None → ask which."""
    if entity == "user":
        return start_create(session)
    if entity is None or entity == "AMBIGUOUS" or entity not in _ENTITY_CREATE:
        session.metadata["flow"] = {"name": "entity_create", "stage": "disambiguate", "data": {}}
        return _disambiguate_entity()
    flow = {"name": "entity_create", "entity": entity, "stage": "collect",
            "idx": 0, "data": {}, "labels": {}}
    session.metadata["flow"] = flow
    spec = _ENTITY_CREATE[entity]
    return _ep(FlowResult(message=f"Let's set up a new **{spec['label']}**. I'll ask a few things, "
                                  f"then show everything for your confirmation before anything runs.\n\n"
                                  + _efield_prompt(spec["fields"][0])), flow)


def _efield_prompt(f: "EField", data: dict[str, Any] | None = None) -> str:
    p = f.prompt
    if f.suggest_from and data and data.get(f.suggest_from):
        sug = re.sub(r"[^A-Za-z0-9]+", "_", str(data[f.suggest_from])).strip("_").upper()
        if sug:
            p += f" _(suggested: {sug})_"
    return p


async def _efield_step(flow: dict[str, Any], f: "EField", headers: dict[str, str] | None,
                       prefix: str = "") -> FlowResult:
    """Render the current field: pickers show live chips; yes/no shows Yes/No; text just asks."""
    if f.kind == "app_picker":
        apps = await _apps(headers)
        flow.setdefault("_opts", {})["app"] = apps
        chips, note = _list_chips(apps, "app", [_chip("Cancel", "cancel", icon="skip")]) if apps else ([], "")
        return FlowResult(message=f"{prefix}{f.prompt}{note}", suggestions=chips or None)
    if f.kind == "role_picker":
        # sub-picker: application first, then its roles
        if not flow.get("_role_app"):
            apps = await _apps(headers)
            flow.setdefault("_opts", {})["app"] = apps
            chips, note = _list_chips(apps, "app", [_chip("Cancel", "cancel", icon="skip")]) if apps else ([], "")
            return FlowResult(message=f"{prefix}First, which **application** is the role in?{note}",
                              suggestions=chips or None)
        roles = await _roles(headers, flow["_role_app"])
        flow.setdefault("_opts", {})["role"] = roles
        chips, note = _list_chips(roles, "role", [_chip("Cancel", "cancel", icon="skip")]) if roles else ([], "")
        return FlowResult(message=f"{prefix}{f.prompt}{note}", suggestions=chips or None)
    if f.kind == "menu_picker":
        # Prerequisite gate: a role needs an attached menu. Ask whether one already exists or
        # should be created first, before showing the picker.
        if flow.get("_menu_stage") != "pick":
            flow["_menu_stage"] = "choice"
            return FlowResult(
                message=(f"{prefix}A role opens an attached **menu**. Are you ready with the "
                         "menu, or should I set one up first?"),
                suggestions=[_chip("Pick an existing menu", "pick an existing menu", icon="menu-2"),
                             _chip("Create a new menu first", "create a new menu first", icon="plus"),
                             _chip("Cancel", "cancel", icon="skip")])
        app_id = (flow.get("data") or {}).get("applicationId")
        menus = await _menus(headers, app_id)
        flow.setdefault("_opts", {})["menu"] = menus
        tail = [_chip("Create a new menu instead", "create a new menu first", icon="plus"),
                _chip("Cancel", "cancel", icon="skip")]
        if not menus:
            return FlowResult(message=f"{prefix}{f.prompt} _(type the menu name)_", suggestions=tail)
        chips, note = _list_chips(menus, "app", tail)
        return FlowResult(message=f"{prefix}{f.prompt}{note}", suggestions=chips)
    if f.kind == "yesno":
        return FlowResult(message=f"{prefix}{f.prompt}",
                          suggestions=[_chip("Yes", "yes", icon="check"), _chip("No", "no", icon="skip")])
    return FlowResult(message=f"{prefix}{_efield_prompt(f, flow.get('data'))}")


async def _next_app_order(headers: dict[str, str] | None) -> int:
    """The next free applicationOrder (max existing + 10) — APPLICATION_ORDER is NOT-NULL +
    unique, so a new app must not collide. Falls back to a high value if the list can't load."""
    try:
        res = await tool_registry.execute("getAllApplications_get", {}, headers)
        data = res.output.get("data") if isinstance(res.output, dict) else None
        orders = [int(a["applicationOrder"]) for a in (data or [])
                  if isinstance(a, dict) and str(a.get("applicationOrder", "")).strip().lstrip("-").isdigit()]
        return (max(orders) + 10) if orders else 1000
    except Exception:  # noqa: BLE001
        return 1000


async def _entity_finalize(flow: dict[str, Any], headers: dict[str, str] | None) -> FlowResult:
    entity = flow["entity"]
    spec = _ENTITY_CREATE[entity]
    args = {**spec.get("defaults", {}), **flow.get("data", {})}
    if spec.get("auto_order"):                       # application: assign a unique order
        args["applicationOrder"] = await _next_app_order(headers)
    labels = flow.get("labels", {})
    lines = []
    for f in spec["fields"]:
        val = labels.get(f.key, flow["data"].get(f.key))
        if val in (None, ""):
            val = "—"
        lines.append(f"- **{f.key}:** {val}")
    if spec.get("auto_order"):
        lines.append(f"- **applicationOrder:** {args['applicationOrder']} _(auto)_")
    summary = (f"Ready to create this **{spec['label']}**:\n" + "\n".join(lines)
               + "\n\nShall I go ahead?")
    return FlowResult(message=summary,
                      pending={"tool_name": spec["tool"], "tool_args": args, "summary": summary})


async def _advance_entity(flow: dict[str, Any], headers: dict[str, str] | None,
                          prefix: str = "") -> FlowResult:
    spec = _ENTITY_CREATE[flow["entity"]]
    flow["idx"] += 1
    flow.pop("_role_app", None)
    if flow["idx"] >= len(spec["fields"]):
        flow["stage"] = "confirm"
        return await _entity_finalize(flow, headers)
    f: EField = spec["fields"][flow["idx"]]
    # Auto-generated code fields (e.g. role code) are never prompted — compute + de-dupe them
    # from their source field and move straight to the next field.
    if f.auto_from:
        data = flow.setdefault("data", {})
        labels = flow.setdefault("labels", {})
        source = data.get(f.auto_from) or labels.get(f.auto_from) or ""
        taken = await _taken_values(f.unique, data, headers, flow) if f.unique else set()
        value = _unique_code(source, taken, f.maxlen or 30)
        data[f.key] = value
        labels[f.key] = f"{value} · auto"
        return await _advance_entity(flow, headers, prefix=prefix)
    return await _efield_step(flow, f, headers, prefix=prefix)


async def _entity_create(session: Any, flow: dict[str, Any], msg: str,
                         headers: dict[str, str] | None) -> FlowResult:
    stage = flow.get("stage")

    if stage == "disambiguate":
        ent = None
        low = msg.strip().lower()
        for e in _ENTITY_CHIP_ORDER:
            if e in low or _ENTITY_CHIP_LABEL[e].lower() in low:
                ent = e
                break
        if ent is None:
            ent = _detect_entity(msg)
            if ent in (None, "AMBIGUOUS"):
                return _disambiguate_entity()
        session.metadata.pop("flow", None)
        return start_entity_create(session, ent)

    if stage != "collect":
        return FlowResult(message="Please use **Confirm** or **Cancel** above to finish.")

    spec = _ENTITY_CREATE[flow["entity"]]
    f: EField = spec["fields"][flow["idx"]]
    data = flow.setdefault("data", {})
    labels = flow.setdefault("labels", {})

    if f.kind == "app_picker":
        apps = (flow.get("_opts", {}) or {}).get("app") or await _apps(headers)
        app = _match(msg, apps)
        if not app:
            return await _efield_step(flow, f, headers, prefix="I didn't catch that — pick one. ")
        data[f.key] = app["id"]; labels[f.key] = app["name"]
        return await _advance_entity(flow, headers)

    if f.kind == "role_picker":
        if not flow.get("_role_app"):
            apps = (flow.get("_opts", {}) or {}).get("app") or await _apps(headers)
            app = _match(msg, apps)
            if not app:
                return await _efield_step(flow, f, headers, prefix="Pick an application first. ")
            flow["_role_app"] = app["id"]; flow["_role_app_name"] = app["name"]
            return await _efield_step(flow, f, headers)
        roles = (flow.get("_opts", {}) or {}).get("role") or await _roles(headers, flow["_role_app"])
        role = _match(msg, roles)
        if not role:
            return await _efield_step(flow, f, headers, prefix="Pick a role. ")
        data[f.key] = role["id"]; labels[f.key] = f"{flow.get('_role_app_name','')} · {role['name']}"
        return await _advance_entity(flow, headers)

    if f.kind == "menu_picker":
        low = msg.strip().lower()
        wants_new = bool(re.search(r"\b(create|new|set ?up|make|build)\b", low)) and \
            (flow.get("_menu_stage") == "choice" or "menu" in low)
        if wants_new:
            return await _start_menu_subflow(session, flow, headers)
        if flow.get("_menu_stage") == "choice":
            # anything not "create" → pick an existing one
            flow["_menu_stage"] = "pick"
            return await _efield_step(flow, f, headers)
        app_id = data.get("applicationId")
        menus = (flow.get("_opts", {}) or {}).get("menu") or await _menus(headers, app_id)
        menu = _match(msg, menus)
        if not menu:
            return await _efield_step(flow, f, headers, prefix="Pick a menu from the list, or type its name. ")
        data[f.key] = menu["id"]; labels[f.key] = menu["name"]
        return await _advance_entity(flow, headers)

    if f.kind == "yesno":
        yn = _yn(msg)
        if yn is None:
            return await _efield_step(flow, f, headers, prefix="Please choose Yes or No. ")
        data[f.key] = yn; labels[f.key] = "Yes" if yn == "Y" else "No"
        return await _advance_entity(flow, headers)

    # free text
    text = msg.strip()
    if not text:
        if f.optional:
            return await _advance_entity(flow, headers)
        return FlowResult(message=_efield_prompt(f, data))
    label = _FIELD_LABEL.get(f.key, f.key)
    if f.suggest_from and text.lower() in ("suggest", "suggested", "use suggested", "ok", "yes"):
        text = _codeify(data.get(f.suggest_from, ""), f.maxlen) or text
    # Code fields: normalize to UPPER_SNAKE (and cap width) before anything else.
    if f.is_code:
        text = _codeify(text, f.maxlen)
        if not text:
            return FlowResult(message=f"That **{label}** has no letters or digits to build a code "
                                      "from. Please enter a value with some letters or numbers.")
    # Length guard (non-code free text): reject over the column width rather than silently truncate.
    elif f.maxlen and len(text) > f.maxlen:
        return FlowResult(message=f"That **{label}** is {len(text)} characters — please keep it "
                                  f"to **{f.maxlen}** or fewer, then send it again.")
    # Uniqueness guard: name compared lower-cased, code upper-cased, scoped to the app.
    unique_tag = f.unique or f.unique_among
    if unique_tag:
        taken = await _taken_values(unique_tag, data, headers, flow)
        probe = text.upper() if "code" in unique_tag else text.lower()
        if probe in taken:
            where = "" if unique_tag == "app_code" else " in this application"
            return FlowResult(message=f"**{text}** is already taken as a **{label.lower()}**{where}. "
                                      f"Please choose a different {label.lower()}.")
    data[f.key] = text; labels[f.key] = text
    return await _advance_entity(flow, headers)


_FIELD_LABEL = {
    "applicationId": "Application", "applicationName": "Name", "applicationCode": "Code",
    "applicationShortCode": "Short code", "description": "Description", "applicationUrl": "URL",
    "applicationIcon": "Icon", "appCategory": "Category",
    "roleName": "Role name", "role": "Role code", "roleDescription": "Description",
    "isAdmin": "Admin role", "menuId": "Attached menu",
    "menuName": "Name", "menuCode": "Code", "menuType": "Type", "menuDesc": "Description",
    "applicationRoleId": "Role", "readFlag": "Read", "writeFlag": "Write",
    "updateFlag": "Update", "deleteFlag": "Delete",
}


def _entity_progress(flow: dict[str, Any]) -> dict[str, Any] | None:
    """The workflow rail for an entity_create flow: each field as done/current/todo + a final
    review step, so the widget can show where the user is and what's been captured."""
    entity = flow.get("entity")
    spec = _ENTITY_CREATE.get(entity or "")
    if not spec or flow.get("stage") not in ("collect", "confirm"):
        return None
    labels = flow.get("labels", {}) or {}
    idx = flow.get("idx", 0)
    collecting = flow.get("stage") == "collect"
    steps: list[dict[str, Any]] = []
    for i, f in enumerate(spec["fields"]):
        if f.key in labels:
            state = "done"
        elif collecting and i == idx:
            state = "current"
        else:
            state = "todo"
        steps.append({"label": _FIELD_LABEL.get(f.key, f.key),
                      "value": str(labels.get(f.key, "")), "state": state})
    steps.append({"label": "Review & confirm", "value": "",
                  "state": "current" if flow.get("stage") == "confirm" else "todo"})
    done_n = sum(1 for s in steps if s["state"] == "done")
    return {"title": f"Create {spec.get('label', entity)}",
            "current": min(done_n + 1, len(steps)), "total": len(steps), "steps": steps}


def _ep(fr: "FlowResult | None", flow: dict[str, Any] | None) -> "FlowResult | None":
    """Attach the entity progress rail to a flow result (no-op for non-entity flows)."""
    if fr is not None and fr.progress is None and flow and flow.get("name") == "entity_create":
        fr.progress = _entity_progress(flow)
    return fr


# ── Remove an application role (guided, dependency-previewed, confirm-gated) ─────
# Deleting a role definition is a HARD delete that cascades its user→role assignments, so the
# flow first SURFACES the dependents (assigned users, privileges, the attached menu) and blocks
# seeded roles — nothing is removed until the user confirms against that dependency list.

_RM_VERB = re.compile(r"\b(remove|delete|drop|deprovision|retire|decommission)\b", re.I)
_RM_ROLE_OBJ = re.compile(r"\brole\b", re.I)
# NOT a role-definition delete: un-assigning a role FROM a user, or removing access/privilege —
# those are handled by the access skills, not by deleting the role itself.
_RM_UNASSIGN = re.compile(r"\bfrom\b|\bfor\b\s+\w|@|\b(access|assignment|privilege|permission|"
                          r"membership|grant)\b|\buser\b|\busers\b", re.I)


def _detect_remove_role(msg: str) -> bool:
    """True only for 'delete/remove a ROLE definition' — never for un-assigning a role from a
    user or removing access (which the access skills own). Precision over recall: bail if unsure."""
    m = msg or ""
    if not _RM_VERB.search(m) or not _RM_ROLE_OBJ.search(m):
        return False
    if _RM_UNASSIGN.search(m):
        return False
    return True


async def _roles_full(headers: dict[str, str] | None, app_id: Any) -> list[dict[str, Any]]:
    """Enabled roles for an app with the fields the remove flow needs (id, name, code, seeded,
    attached menu, admin/description)."""
    out: list[dict[str, Any]] = []
    try:
        res = await tool_registry.execute("getApplicationRolesByAppId_get", {"applicationId": app_id}, headers)
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        for r in (data or []):
            if not isinstance(r, dict) or str(r.get("enabled", "Y")).upper() == "N":
                continue
            name = str(r.get("roleName") or r.get("role") or "").strip()
            rid = r.get("applicationRoleId")
            if name and rid not in (None, ""):
                out.append({
                    "id": rid, "name": name, "code": str(r.get("role") or "").strip(),
                    "seeded": str(r.get("isSeeded") or "").strip().lower() in ("y", "yes", "true", "1"),
                    "menuName": str(r.get("menuName") or "").strip(),
                    "menuCode": str(r.get("menuCode") or "").strip(),
                    "isAdmin": str(r.get("isAdmin") or "").strip(),
                    "description": str(r.get("roleDescription") or "").strip(),
                })
    except Exception as exc:  # noqa: BLE001
        log.bind(func="flow_roles_full").warning(f"roles_full failed: {exc}")
    return out


async def _role_dependencies(headers: dict[str, str] | None, role_id: Any) -> dict[str, Any]:
    """What depends on a role: the user→role assignments that a delete would cascade, plus any
    role privileges. Fail-open (empty) if a source can't be read — the preview says so."""
    users: list[dict[str, Any]] = []
    users_ok = True
    try:
        res = await tool_registry.execute("getAllUserApplicationRoles_post", {"userName": "*"}, headers)
        rows = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        if rows is None:
            users_ok = False
        rid = str(role_id)
        for r in (rows or []):
            if not isinstance(r, dict) or str(r.get("applicationRoleId", "")) != rid:
                continue
            nm = (f"{str(r.get('firstName','')).strip()} {str(r.get('lastName','')).strip()}").strip()
            users.append({"userName": str(r.get("userName") or "").strip(),
                          "name": nm or str(r.get("userName") or "").strip(),
                          "enabled": str(r.get("enabled") or "").strip()})
    except Exception as exc:  # noqa: BLE001
        users_ok = False
        log.bind(func="flow_role_deps").warning(f"user assignments read failed: {exc}")
    privileges = 0
    try:
        pr = await tool_registry.execute("getAllAppRolePrivileges_post", {"applicationRoleId": str(role_id)}, headers)
        pd = pr.output.get("data") if getattr(pr, "success", False) and isinstance(pr.output, dict) else None
        privileges = len(pd or [])
    except Exception:  # noqa: BLE001
        privileges = 0
    return {"users": users, "users_ok": users_ok, "privileges": privileges}


def _remove_progress(flow: dict[str, Any]) -> dict[str, Any]:
    """The workflow rail for the remove-role flow (Application → Role → Dependencies → Confirm)."""
    stage = flow.get("stage")
    data = flow.get("data", {}) or {}
    order = ["pick_app", "pick_role", "confirm"]
    labels = {"pick_app": ("Application", data.get("appName", "")),
              "pick_role": ("Role", data.get("roleLabel", "")),
              "confirm": ("Review dependencies & confirm", "")}
    cur_i = order.index(stage) if stage in order else 0
    steps = []
    for i, k in enumerate(order):
        lbl, val = labels[k]
        state = "done" if i < cur_i else "current" if i == cur_i else "todo"
        steps.append({"label": lbl, "value": str(val), "state": state})
    return {"title": "Remove application role", "current": cur_i + 1, "total": len(order), "steps": steps}


def terminal_progress(flow: dict[str, Any] | None) -> dict[str, Any] | None:
    """A completed rail for a just-finished flow: every step done + complete=True, so the widget
    can lock the workflow window as DONE and prompt for the next request instead of it vanishing."""
    if not flow:
        return None
    name = flow.get("name")
    if name == "entity_create":
        rail = _entity_progress({**flow, "stage": "confirm"})
    elif name == "remove_role":
        rail = _remove_progress({**flow, "stage": "confirm"})
    else:
        return None
    if not rail:
        return None
    for s in rail["steps"]:
        s["state"] = "done"
    rail["current"] = rail["total"]
    rail["complete"] = True
    return rail


def start_remove_role(session: Any) -> FlowResult:
    """Front door for deleting a role definition — arm the flow, ask which application first.
    (Authority is enforced by the onboarding gate before this is shown.)"""
    session.metadata["flow"] = {"name": "remove_role", "stage": "pick_app", "data": {}}
    fr = FlowResult(message=("Let's remove an **application role**. I'll show everything attached "
                             "to it — assigned users, privileges — before anything is deleted.\n\n"
                             "Which **application** is the role in? _(type its name)_"))
    fr.progress = _remove_progress(session.metadata["flow"])
    return fr


async def _remove_role(session: Any, flow: dict[str, Any], msg: str,
                       headers: dict[str, str] | None) -> FlowResult:
    stage = flow.get("stage")
    data = flow.setdefault("data", {})

    if stage == "pick_app":
        apps = await _apps(headers)
        app = _match(msg, apps)
        if not app:
            chips, note = _list_chips(apps, "app", [_chip("Cancel", "cancel", icon="skip")]) if apps else ([], "")
            return FlowResult(message=f"Pick the **application** the role is in.{note}", suggestions=chips or None)
        data["appId"] = app["id"]; data["appName"] = app["name"]
        flow["stage"] = "pick_role"
        roles = await _roles_full(headers, app["id"])
        flow["_roles"] = roles
        if not roles:
            session.metadata.pop("flow", None)
            return FlowResult(message=f"**{app['name']}** has no removable roles.", done=True)
        chips, note = _list_chips(roles, "role", [_chip("Cancel", "cancel", icon="skip")])
        fr = FlowResult(message=f"Which **role** in **{app['name']}** should I remove?{note}", suggestions=chips)
        fr.progress = _remove_progress(flow)
        return fr

    if stage == "pick_role":
        roles = flow.get("_roles") or await _roles_full(headers, data.get("appId"))
        role = _match(msg, roles)
        if not role:
            chips, note = _list_chips(roles, "role", [_chip("Cancel", "cancel", icon="skip")]) if roles else ([], "")
            return FlowResult(message=f"Pick a **role** to remove.{note}", suggestions=chips or None)
        if role.get("seeded"):
            session.metadata.pop("flow", None)
            return FlowResult(message=(f"**{role['name']}** is a **seeded** role and can't be deleted "
                                       "(the platform protects seeded data). Nothing was changed."), done=True)
        data["roleId"] = role["id"]; data["roleName"] = role["name"]; data["roleCode"] = role.get("code", "")
        data["roleLabel"] = f"{data['appName']} · {role['name']}"
        flow["stage"] = "confirm"
        deps = await _role_dependencies(headers, role["id"])
        users = deps["users"]; privs = deps["privileges"]
        # Dependency block for the confirmation.
        lines = [f"Ready to remove **{role['name']}** _(`{role.get('code','')}`)_ from "
                 f"**{data['appName']}**. Here's what's attached:"]
        if not deps["users_ok"]:
            lines.append("- ⚠️ **Assigned users:** couldn't be read just now — please double-check in Administration.")
        elif users:
            names = ", ".join(u["name"] for u in users[:8]) + ("…" if len(users) > 8 else "")
            lines.append(f"- 👥 **{len(users)} user(s)** currently hold this role and **will lose it**: {names}")
        else:
            lines.append("- 👥 **No users** are assigned this role.")
        if privs:
            lines.append(f"- 🔑 **{privs} privilege row(s)** on this role will be removed.")
        if role.get("menuName"):
            lines.append(f"- 📄 Attached menu **{role['menuName']}** stays — only the role is deleted.")
        lines.append("\n**This hard delete can't be undone.** Shall I go ahead?")
        summary = "\n".join(lines)
        fr = FlowResult(message=summary,
                        pending={"tool_name": "deleteApplicationRoleById_delete",
                                 "tool_args": {"applicationRoleId": str(role["id"])},
                                 "summary": summary})
        fr.progress = _remove_progress(flow)
        return fr

    return FlowResult(message="Please use **Confirm** or **Cancel** above to finish removing the role.")


async def _start_menu_subflow(session: Any, role_flow: dict[str, Any],
                              headers: dict[str, str] | None) -> FlowResult:
    """Dependency chain: the role needs a menu that doesn't exist yet — suspend the role flow,
    run a menu-create flow first (inheriting the role's application), and mark it to RESUME the
    role at its confirm once the menu is created."""
    app_id = (role_flow.get("data") or {}).get("applicationId")
    app_label = (role_flow.get("labels") or {}).get("applicationId", "")
    menu_flow = {"name": "entity_create", "entity": "menu", "stage": "collect", "idx": 0,
                 "data": {"applicationId": app_id}, "labels": {"applicationId": app_label},
                 "_resume_to": role_flow}
    session.metadata["flow"] = menu_flow
    # applicationId is inherited from the role, so skip the app picker and prompt the next field.
    return await _advance_entity(menu_flow, headers,
                                 prefix="Let's set up the **menu** for this role first. ")


async def resume_role_after_menu(session: Any, tool_args: dict[str, Any],
                                 headers: dict[str, str] | None) -> FlowResult | None:
    """After a menu is created INSIDE a role flow (the _resume_to marker), attach the new menu
    to the role and resume it at its confirm gate. Returns None for a standalone menu create."""
    flow = session.metadata.get("flow")
    if not flow or flow.get("name") != "entity_create" or not flow.get("_resume_to"):
        return None
    role_flow = flow["_resume_to"]
    app_id = (role_flow.get("data") or {}).get("applicationId")
    menu_name = str((tool_args or {}).get("menuName") or "").strip()
    # addMenu returns no id → look the new menu up by name in its application.
    new_id = None
    for m in await _menus(headers, app_id):
        if str(m["name"]).strip().lower() == menu_name.lower():
            new_id = m["id"]
            break
    session.metadata["flow"] = role_flow
    role_flow.setdefault("data", {})
    role_flow.setdefault("labels", {})
    if new_id is None:                       # couldn't resolve → let them pick it from the list
        role_flow["_menu_stage"] = "pick"
        fld = _ENTITY_CREATE["role"]["fields"][role_flow["idx"]]
        return await _efield_step(role_flow, fld, headers,
                                  prefix=f"Menu **{menu_name}** created. Now attach it to the role — ")
    role_flow["data"]["menuId"] = new_id
    role_flow["labels"]["menuId"] = menu_name
    fr = await _advance_entity(role_flow, headers,
                               prefix=f"Menu **{menu_name}** created and attached. ")
    fr = _ep(fr, role_flow)
    if fr is not None:
        fr.ack = f"Prerequisite handled — menu **{menu_name}** created and attached to the role."
    return fr


# Tools whose confirmed mutation ends an entity_create flow (no continuation) — used by
# chat_service._post_confirm_flow to clear the flow after the single write succeeds.
ENTITY_CREATE_TOOLS = frozenset(spec["tool"] for spec in _ENTITY_CREATE.values())


# ── Onboarding authorization ────────────────────────────────────────────────────
# Onboarding is an ADMINISTRATION action: the caller must be assigned the Administration
# application AND hold an admin app-role in it (an ADMINISTRATION role whose definition has
# isAdmin=Y — SYSTEM_ADMIN / GC_ADMIN / BUDGET_ADMIN / SUPER_ADMIN). Verified before a flow
# starts so an unauthorized caller is refused up front, never after collecting details.
_ADMIN_APP_CODE = "ADMINISTRATION"
_ADMIN_ROLE_FALLBACK = frozenset({"SUPER_ADMIN", "SYSTEM_ADMIN", "GC_ADMIN", "BUDGET_ADMIN"})
_ADMIN_ROLES_CACHE: dict[str, Any] = {"codes": None, "ts": 0.0}
_ONBOARDING_FLOWS = frozenset({"create_user", "entity_create", "onboard", "remove_role"})


def is_onboarding_flow(session: Any) -> bool:
    f = session.metadata.get("flow")
    return bool(f) and f.get("name") in _ONBOARDING_FLOWS


async def _admin_role_codes(headers: dict[str, str] | None) -> set[str]:
    """Admin role codes in ADMINISTRATION (isAdmin=Y) — the roles that authorize onboarding.
    Cached ~10 min; falls back to the known set if the definitions can't be read."""
    import time as _t
    c = _ADMIN_ROLES_CACHE
    if c["codes"] and (_t.monotonic() - c["ts"]) < 600:
        return c["codes"]
    codes: set[str] = set()
    try:
        res = await tool_registry.execute("getAllApplicationRoles_get", {}, headers)
        data = res.output.get("data") if isinstance(res.output, dict) else None
        for r in (data or []):
            if isinstance(r, dict) and str(r.get("applicationCode", "")).upper() == _ADMIN_APP_CODE \
               and str(r.get("isAdmin", "")).upper() in ("Y", "TRUE", "1"):
                codes.add(str(r.get("role", "")).upper())
    except Exception:  # noqa: BLE001
        pass
    codes = codes or set(_ADMIN_ROLE_FALLBACK)
    c["codes"] = codes
    c["ts"] = _t.monotonic()
    return codes


async def authorize_onboarding(headers: dict[str, str] | None) -> tuple[bool, str]:
    """(allowed, refusal_message). The caller must hold an admin role in ADMINISTRATION.
    Fail CLOSED: if their access can't be read, deny — never onboard without proven authority."""
    admin_codes = await _admin_role_codes(headers)
    try:
        res = await tool_registry.execute("getActiveUserAppRolesByUserId_post", {}, headers)
        rows = res.output.get("data") if isinstance(res.output, dict) else None
    except Exception:  # noqa: BLE001
        rows = None
    if rows is None:
        return False, ("I couldn't verify your administrator access just now, so I can't run an "
                       "onboarding action. Please try again, or use the Administration app.")
    for r in rows:
        if isinstance(r, dict) and str(r.get("applicationCode", "")).upper() == _ADMIN_APP_CODE \
           and str(r.get("role", "")).upper() in admin_codes:
            return True, ""
    return False, ("Onboarding is an administrator action, and your access doesn't include an "
                   "admin role in the **Administration** application — so I can't do that on "
                   "your behalf. Please ask a system administrator, or use the Administration app.")


# Access-governing mutations — creating users/apps/roles/menus/privileges AND assigning /
# removing / editing a user's application access. Every one of these requires the caller to
# hold an admin role in Administration (authorize_onboarding). Gated at execution (the confirm
# executors + the workflow tool runner) so NO path — skill, flow, or workflow — can run one
# without proven authority, and up front at skill resolution for a clean refusal.
ACCESS_MUTATION_TOOLS = frozenset(ENTITY_CREATE_TOOLS) | {
    "addUser_post",
    "addUserApplicationAndRole_post", "addUserApplicationRole_post",
    "addUsersWithRoleToApp_post", "assignApplicationRolesToUser_post",
    "deleteUserApplicationRoleById_delete", "deleteUserApplicationRolesById_delete",
    "deleteUserApplicationById_delete", "updateUserApplicationRoles_put",
    "deleteApplicationRoleById_delete",   # remove a role definition (guided remove_role flow)
}


def is_access_mutation(tool_name: str) -> bool:
    return tool_name in ACCESS_MUTATION_TOOLS
