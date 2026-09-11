"""
services/workflow.py
────────────────────
A declarative, n8n-style workflow engine for the GC agent's guided flows.

Where flows.py hand-codes one handler per flow (and PickerStep gives a *linear* picker
list), this models a workflow as a GRAPH of typed nodes wired by edges — the n8n mental
model, adapted to a conversational agent:

    Ask      → prompt the user for a value (static or fetched options; single or multi)
    Fetch    → call a read tool; stash its data for later nodes  (data passes node→node)
    Branch   → choose the next node from a collected value        (the If node)
    Confirm  → show a summary of everything collected, gate the mutation
    Mutate   → call the write tool with the collected data        (the terminal action)
    Say      → a terminal message, ends the run

A workflow is data, not code: `Workflow(id, trigger, start, nodes={...})`. New workflows
are composed by declaring nodes and edges — no bespoke handler — which is what makes the
existing flows migratable and, later, a visual builder (option 3) possible: the builder
just reads/writes these node/edge definitions.

State is a small dict carried on the session: {wf, node, data} — `data` is the shared bag
that every node reads and writes (n8n's "items" passed between nodes). The runner walks the
graph, executing Fetch/Branch nodes silently until it reaches a node that needs the user
(Ask/Confirm) or ends the run (Say), and returns a RunStep the caller renders.

This module is standalone and side-effect-free to import; wiring it into chat_service is a
follow-up once the pattern is signed off. `python -m app.services.workflow --demo` walks a
sample workflow with a stub executor so the graph logic can be seen with no backend.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _dcfield
from typing import Any, Awaitable, Callable, Optional

# A tool executor: (tool_name, args, headers) -> {"data": [...], ...}. Injected so the engine
# never imports the MCP layer directly (keeps it unit-testable with a stub).
ToolExec = Callable[[str, dict, Optional[dict]], Awaitable[dict]]


# ── node types ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Node:
    id: str
    next: str = ""            # default outgoing edge (unused by Branch/Say)


@dataclass(frozen=True)
class Ask(Node):
    field: str = ""
    prompt: str = ""
    options_from: str = ""    # a data key (list of dicts) supplying options; else static/free-text
    label_key: str = ""       # option display key
    value_key: str = ""       # option value key stored into data[field]
    static: tuple[str, ...] = ()
    multi: bool = False
    optional: bool = False


@dataclass(frozen=True)
class Fetch(Node):
    tool: str = ""
    args: dict = _dcfield(default_factory=dict)   # literal args; "$field" values pull from data
    as_key: str = ""          # store the tool's data list under data[as_key]


@dataclass(frozen=True)
class Filter(Node):
    source: str = ""          # data key (a list) to filter
    as_key: str = ""          # where to store the filtered list (+ "<as_key>_empty" = Y/N)
    keep: dict = _dcfield(default_factory=dict)   # {field: literal-or-"$ref"} — all must match
    exclude_source: str = ""  # data key (a list) whose `on` values are removed from source
    on: str = ""              # match field for exclusion (drop row if row[on] ∈ exclude set)


@dataclass(frozen=True)
class Branch(Node):
    field: str = ""
    cases: dict = _dcfield(default_factory=dict)  # {collected value → next node id}
    default: str = ""


@dataclass(frozen=True)
class Confirm(Node):
    summary: tuple[tuple[str, str], ...] = ()  # (label, data-field) rows shown before mutate


@dataclass(frozen=True)
class Mutate(Node):
    tool: str = ""
    arg_map: dict = _dcfield(default_factory=dict)  # {tool arg → data field}


@dataclass(frozen=True)
class Say(Node):
    text: str = ""


@dataclass(frozen=True)
class Workflow:
    id: str
    trigger: str              # regex that opens the workflow
    start: str                # id of the first node
    title: str = ""
    nodes: dict = _dcfield(default_factory=dict)  # id → Node


# What the runner hands back for the caller to render / act on.
@dataclass
class RunStep:
    message: str = ""
    options: list[str] = _dcfield(default_factory=list)   # chips to offer, if any
    done: bool = False                                  # run finished (clear state)
    trace: list[str] = _dcfield(default_factory=list)     # node ids executed this turn (run trace)


# ── helpers ─────────────────────────────────────────────────────────────────────
_DONE = re.compile(r"\b(done|that'?s all|finish(ed)?|proceed|next|complete)\b", re.I)
_SKIP = re.compile(r"\b(skip|none|not now|later|n/?a)\b", re.I)
_YES = re.compile(r"\b(y|yes|yeah|yep|confirm|go|proceed|do it)\b", re.I)


def _resolve_args(args: dict, data: dict) -> dict:
    """Substitute "$field" arg values from the collected data (n8n expression, simplified)."""
    out = {}
    for k, v in args.items():
        out[k] = data.get(v[1:], "") if isinstance(v, str) and v.startswith("$") else v
    return out


_EDIT = re.compile(r"\b(change|edit|modif(y|ies)|update|revise|redo|go ?back|back to)\b", re.I)
_EDIT_STOP = {"the", "a", "an", "which", "in", "that", "to", "your", "only", "they", "don",
              "have", "already", "name", "should", "access", "grant", "run", "this"}


def _edit_target(message: str, wf: "Workflow") -> str:
    """If the user asks to change/edit a step, return the Ask node id whose field/prompt best
    matches (so 'change the application' jumps back to the application step). '' if none."""
    if not _EDIT.search(message or ""):
        return ""
    words = set(re.findall(r"[a-z]+", (message or "").lower())) - _EDIT_STOP
    for nid, node in wf.nodes.items():
        if not isinstance(node, Ask):
            continue
        kw = set(re.findall(r"[a-z]+", (node.field + " " + node.prompt).lower())) - _EDIT_STOP
        if words & kw:
            return nid
    return ""


def _match_option(msg: str, opts: list[dict], label_key: str, value_key: str) -> Any:
    """Resolve a user's line to an option's value (exact, then case-insensitive contains)."""
    low = msg.strip().lower()
    for o in opts:
        if str(o.get(label_key, "")).strip().lower() == low:
            return o.get(value_key)
    for o in opts:
        if low and low in str(o.get(label_key, "")).strip().lower():
            return o.get(value_key)
    return None


# ── the runner ────────────────────────────────────────────────────────────────
async def advance(wf: Workflow, state: dict, message: str,
                  execute: ToolExec, headers: dict | None = None) -> RunStep:
    """Advance the workflow one user turn. `state` = {"node": id, "data": {}}; mutated in place.
    Applies the user's `message` to the node awaiting input, then walks silent nodes
    (Fetch/Branch/Mutate) until the next node that needs the user or ends the run."""
    data = state.setdefault("data", {})
    node = wf.nodes[state["node"]]
    trace: list[str] = []

    # Walk-back edit: at ANY user-facing step, "change <step>" jumps back to that step (its
    # field is cleared and re-asked; dependent steps downstream re-run). Checked before we
    # treat the message as an answer, so it works mid-flow, not just at Confirm.
    if isinstance(node, (Ask, Confirm)):
        tgt = _edit_target(message, wf)
        if tgt:
            fld = wf.nodes[tgt].field
            data.pop(fld, None)
            data.pop(fld + "_label", None)
            state["node"] = tgt
            return await _walk(wf, state, execute, headers, trace)

    # 1) apply the user's answer to the node that was awaiting it
    if isinstance(node, Ask):
        opts = data.get(node.options_from, []) if node.options_from else []
        if node.optional and (_SKIP.search(message) or _DONE.search(message)):
            data.setdefault(node.field, [] if node.multi else "")
        elif opts:
            val = _match_option(message, opts, node.label_key, node.value_key)
            if val is None:
                return _render(node, data, note="I didn't catch that — pick one of the options.")
            if node.multi:
                data.setdefault(node.field, []).append(val)
                if not _DONE.search(message):                 # keep collecting until "done"
                    return _render(node, data, note="Got it — add more, or say **done**.")
            else:
                data[node.field] = val
                for o in opts:                                # stash the label for summaries
                    if o.get(node.value_key) == val:
                        data[node.field + "_label"] = str(o.get(node.label_key, ""))
                        break
        else:
            data[node.field] = message.strip()               # free text
        state["node"] = node.next
    elif isinstance(node, Confirm):
        if not _YES.search(message):
            return RunStep(message="Okay — say **confirm** to run it, **cancel** to stop, or "
                           "**change <step>** to edit.", options=["confirm", "cancel"])
        state["node"] = node.next

    # 2) walk silent nodes until we need the user again or finish
    return await _walk(wf, state, execute, headers, trace)


async def start(wf: Workflow, state: dict, execute: ToolExec,
                headers: dict | None = None) -> RunStep:
    """Open a workflow at its start node and run to the first user-facing node."""
    state["wf"] = wf.id
    state["node"] = wf.start
    state["data"] = {}
    return await _walk(wf, state, execute, headers, [])


async def _walk(wf: Workflow, state: dict, execute: ToolExec,
                headers: dict | None, trace: list[str]) -> RunStep:
    data = state["data"]
    while True:
        nid = state["node"]
        if not nid:
            return RunStep(message="Done.", done=True, trace=trace)
        node = wf.nodes[nid]
        trace.append(nid)

        if isinstance(node, Fetch):
            res = await execute(node.tool, _resolve_args(node.args, data), headers)
            rows = res.get("data") if isinstance(res, dict) else None
            data[node.as_key] = [r for r in (rows or []) if isinstance(r, dict)]
            state["node"] = node.next
            continue
        if isinstance(node, Filter):
            rows = [r for r in data.get(node.source, []) if isinstance(r, dict)]
            for f, vref in node.keep.items():
                want = data.get(vref[1:], "") if isinstance(vref, str) and vref.startswith("$") else vref
                rows = [r for r in rows if str(r.get(f)) == str(want)]
            if node.exclude_source:
                excl = {str(e.get(node.on)) for e in data.get(node.exclude_source, []) if isinstance(e, dict)}
                rows = [r for r in rows if str(r.get(node.on)) not in excl]
            data[node.as_key] = rows
            data[node.as_key + "_empty"] = "Y" if not rows else "N"
            state["node"] = node.next
            continue
        if isinstance(node, Branch):
            val = str(data.get(node.field, ""))
            state["node"] = node.cases.get(val, node.default)
            continue
        if isinstance(node, Mutate):
            args = {arg: data.get(src) for arg, src in node.arg_map.items()}
            await execute(node.tool, args, headers)
            state["node"] = node.next
            continue
        if isinstance(node, Ask):
            return _render(node, data, trace=trace)
        if isinstance(node, Confirm):
            lines = "\n".join(f"• **{lbl}:** {data.get(f, '—')}" for lbl, f in node.summary)
            edits = [f"change {lbl.lower()}" for lbl, _ in node.summary]  # walk-back chips
            return RunStep(
                message="Here's what I'll submit — **confirm** to run it, or **change <step>** "
                        "to edit before it runs:\n\n" + lines,
                options=["confirm", "cancel", *edits], trace=trace)
        if isinstance(node, Say):
            return RunStep(message=node.text, done=True, trace=trace)
        return RunStep(message=f"(unknown node {nid})", done=True, trace=trace)


def _render(node: Ask, data: dict, note: str = "", trace: list[str] | None = None) -> RunStep:
    opts = data.get(node.options_from, []) if node.options_from else []
    chips = [str(o.get(node.label_key, "")) for o in opts][:8] if opts else list(node.static)
    if node.optional:
        chips = chips + (["done"] if node.multi else ["skip"])
    msg = node.prompt + (f"\n\n_{note}_" if note else "")
    return RunStep(message=msg, options=[c for c in chips if c], trace=trace or [])


# ── a sample workflow: grant a user a role in an application ───────────────────
# Trigger → Ask user → Fetch that user's apps? (kept simple: Fetch all apps) → Ask app →
# Fetch that app's roles → Ask role → Confirm → Mutate → Say. Shows every node type, incl.
# data passed node→node ($applicationId into the roles fetch and the mutation).
GRANT_ACCESS = Workflow(
    id="grant_access",
    title="Grant application access",
    trigger=r"\bgrant .*access\b|\bgive .* access to\b",
    start="ask_user",
    nodes={
        "ask_user": Ask("ask_user", next="fetch_apps", field="userName",
                        prompt="Which **user** should I grant access to? (their username)"),
        "fetch_apps": Fetch("fetch_apps", next="ask_app",
                            tool="getAllApplications_get", as_key="apps"),
        "ask_app": Ask("ask_app", next="fetch_all_roles", field="applicationId",
                       prompt="Which **application**?", options_from="apps",
                       label_key="applicationName", value_key="applicationId"),
        # Grantable = every role in the app MINUS the roles this user already holds there.
        "fetch_all_roles": Fetch("fetch_all_roles", next="fetch_user_roles",
                                 tool="getApplicationRolesByAppId_get",
                                 args={"applicationId": "$applicationId"}, as_key="all_roles"),
        "fetch_user_roles": Fetch("fetch_user_roles", next="filter_has",
                                  tool="getAllUserApplicationRoles_post",
                                  args={"userName": "$userName"}, as_key="user_roles"),
        "filter_has": Filter("filter_has", next="filter_grantable", source="user_roles",
                             as_key="has_roles",
                             keep={"userName": "$userName", "applicationId": "$applicationId"}),
        "filter_grantable": Filter("filter_grantable", next="check_grantable", source="all_roles",
                                   as_key="grantable", exclude_source="has_roles",
                                   on="applicationRoleId"),
        "check_grantable": Branch("check_grantable", field="grantable_empty",
                                  cases={"Y": "say_all", "N": "ask_role"}),
        "say_all": Say("say_all",
                       text="This user already holds every role in that application — nothing to grant."),
        "ask_role": Ask("ask_role", next="confirm", field="roleName",
                        prompt="Which **role** to grant? _(only roles they don't already have)_",
                        options_from="grantable", label_key="roleName", value_key="roleName"),
        # The registry resolves userName→userId and applicationRoleId(name)→id at execute time,
        # so we hand it names + the numeric applicationId (mirrors the existing assign flow).
        "confirm": Confirm("confirm", next="do_grant",
                           summary=(("User", "userName"), ("Application", "applicationId_label"),
                                    ("Role", "roleName"))),
        "do_grant": Mutate("do_grant", next="done",
                           tool="addUserApplicationAndRole_post",
                           arg_map={"userId": "userName", "applicationId": "applicationId",
                                    "applicationRoleId": "roleName"}),
        "done": Say("done", text="✅ Access granted."),
    },
)

REGISTRY: dict[str, Workflow] = {GRANT_ACCESS.id: GRANT_ACCESS}


# ── offline demo: walk the graph with a stub executor (no backend) ─────────────
if __name__ == "__main__":  # pragma: no cover
    import asyncio

    async def _stub(tool, args, headers):
        canned = {
            "getAllApplications_get": [{"applicationName": "Formulation Planner", "applicationId": "3"},
                                       {"applicationName": "Execution Planner", "applicationId": "61"}],
            "getApplicationRolesByAppId_get": [{"roleName": "Budget Administrator", "applicationRoleId": "12"},
                                               {"roleName": "Planning User", "applicationRoleId": "34"}],
        }
        return {"data": canned.get(tool, [])}

    async def _demo():
        wf = GRANT_ACCESS
        st: dict = {}
        step = await start(wf, st, _stub)
        script = ["JSMITH", "Formulation Planner", "Budget Administrator", "confirm"]
        print("BOT:", step.message, "| opts:", step.options, "| trace:", step.trace)
        for ans in script:
            print("\nUSER:", ans)
            step = await advance(wf, st, ans, _stub)
            print("BOT:", step.message, "| opts:", step.options, "| trace:", step.trace,
                  "| done:", step.done)
            if step.done:
                break
        print("\nfinal data:", st.get("data"))

    asyncio.run(_demo())
