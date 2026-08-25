"""
services/skills.py
──────────────────
Data-driven "skills" — guided application ACTIONS (create user, assign role, …), as
opposed to plain reads. A skill makes a small model reliably drive a real mutation:

  • pins its backing MCP tool so it's always offered (RAG may not surface an action tool),
  • grounds the model on the ESSENTIAL fields (ask the user for any that are missing),
  • fills safe DEFAULTS for the remaining fields deterministically in the executor,
  • then rides the existing risk-confirmation flow (pending_action → confirm → execute),
    so nothing state-changing runs without the user's explicit confirmation.

Add a new action by appending a Skill below — no control-flow changes needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .ui_blocks import _humanize


@dataclass(frozen=True)
class Step:
    """One link in a dependency chain that runs AFTER the confirmed entry tool.
    args values reference the shared context with '$name' (a collected field or a value
    captured by an earlier step); anything else is a literal. capture pulls values out of
    this step's output (dotted path into the GC envelope's data) into the context for later
    steps. render=True makes this step's output the final result shown to the user."""
    tool: str
    args: dict[str, str] = field(default_factory=dict)
    capture: dict[str, str] = field(default_factory=dict)
    render: bool = False
    optional: bool = False             # on failure: skip and continue (don't abort the chain)
    label: str = ""                    # human status shown while this step runs (no method names)


@dataclass(frozen=True)
class Skill:
    name: str
    keywords: tuple[str, ...]          # lower-cased intent substrings
    tool: str                          # backing MCP tool (pinned while the skill is active)
    required: tuple[str, ...] = ()     # fields the model must collect (ask if missing)
    defaults: dict[str, Any] = field(default_factory=dict)  # static fills if omitted
    derived: dict[str, tuple[str, ...]] = field(default_factory=dict)  # target ← join(sources)
    lookups: dict[str, str] = field(default_factory=dict)   # field → admin lookup code (name→code)
    validate: dict[str, str] = field(default_factory=dict)  # field → regex (format check)
    unique: dict[str, str] = field(default_factory=dict)    # field → check tool (must NOT already exist)
    then: tuple[Step, ...] = ()        # dependency chain run after the entry tool (on confirm)
    async_task: bool = False           # run in the background (long job) and poll for completion
    summary: str = ""                  # short verb phrase used in grounding


SKILLS: list[Skill] = [
    Skill(
        name="create_user",
        keywords=(
            "create user", "create a user", "create an user", "add user", "add a user",
            "new user", "register user", "register a user", "onboard user",
            "create account", "create an account", "set up a user",
        ),
        tool="addUser_post",
        # The user provides these; the model asks for any that are missing.
        required=("userName", "firstName", "lastName", "emailAddress"),
        # Backend-mandatory fields with safe platform defaults (verified against addUser).
        defaults={
            "emailFlag": "N", "accountType": "L", "accountStatus": "A",
            "accountCategory": "C", "loginType": "L", "enabled": "Y", "adminFlag": "N",
            "userProfileGroupId": 581,   # "Core Profile" — the default new-user profile group
        },
        # Computed from what the user gave, so the model needn't supply them.
        derived={"ssoUserName": ("userName",), "fullName": ("firstName", "lastName")},
        # Validate BEFORE creating: email format + username must not already exist.
        validate={"emailAddress": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        unique={"userName": "getUserByUserName_get"},
        # Master-data fields: the user may name a type/category/status; resolved to its
        # code via the ADMINISTRATION-app lookups (USER_ACCOUNT_*). Defaults above win if
        # the user says nothing.
        lookups={
            "accountType": "USER_ACCOUNT_TYPES",
            "accountCategory": "USER_ACCOUNT_CATEGORIES",
            "accountStatus": "USER_ACCOUNT_STATUS",
        },
        # Dependency chain: addUser returns no id, so fetch the created user by the username
        # we threaded in, and show that record as the result (proves output→input threading).
        then=(
            Step(tool="getUserByUserName_get", args={"userName": "$userName"},
                 capture={"userId": "id"}, render=True, label="Loading the new user"),
        ),
        summary="create a new user account",
    ),

    # ── Background/async example: a report that runs in the background ──────────
    Skill(
        name="generate_report",
        keywords=(
            "generate report", "generate a report", "user access report", "access report",
            "report on users", "run a report", "user application report",
        ),
        tool="getAllUserApps_get",     # aggregate read → rendered as a table "report"
        async_task=True,
        summary="generate a user-access report",
    ),
]


# Human status labels shown while a tool runs — keep method-level names OUT of the UI.
_TOOL_LABELS: dict[str, str] = {
    "addUser_post": "Creating the user",
    "getUserByUserName_get": "Loading the user",
    "getUserByUsernameAndUserId_get": "Loading the user",
    "getUserProfile_get": "Loading the profile",
    "getAllApplications_get": "Loading applications",
    "getUserAppsByUserId_get": "Loading applications",
    "getUserAppRoleByUserId_post": "Loading roles",
    "getApplicationRolesBy_post": "Loading roles",
    "getApplicationRolesByAppId_get": "Loading roles",
    "getAllApplicationRoles_get": "Loading roles",
    "getApplicationRoleByRole_post": "Loading the role",
    "getLookupValueByLookupAndApplicationId_get": "Loading options",
    "deactivateUserByUserName_put": "Deactivating the user",
}

# Generic verb fallback (by tool-name prefix) — clean, and still never a method name.
_VERB_PHRASES: dict[str, str] = {
    "add": "Creating", "create": "Creating", "register": "Creating", "save": "Saving",
    "update": "Updating", "edit": "Updating", "modify": "Updating", "assign": "Assigning",
    "delete": "Removing", "remove": "Removing", "deactivate": "Deactivating",
    "activate": "Activating", "get": "Fetching", "list": "Fetching", "search": "Searching",
    "find": "Finding", "check": "Checking", "fetch": "Fetching",
}


def status_label(tool_name: str, step: "Step | None" = None) -> str:
    """A human 'working on it' line for a tool/step — never exposes the method name.
    Priority: an explicit Step.label → a curated tool label → a generic verb phrase."""
    if step is not None and step.label:
        return f"{step.label}…"
    if tool_name in _TOOL_LABELS:
        return f"{_TOOL_LABELS[tool_name]}…"
    tl = (tool_name or "").lower()
    for prefix, phrase in _VERB_PHRASES.items():
        if tl.startswith(prefix):
            return f"{phrase}…"
    return "Working on it…"


def match(query: str) -> Skill | None:
    """First skill that matches the query. A keyword matches if it's a substring OR all of
    its words appear in the query — so 'create a test user' matches 'create user'."""
    ql = (query or "").lower()
    qwords = set(re.findall(r"[a-z0-9]+", ql))
    for s in SKILLS:
        for kw in s.keywords:
            if kw in ql:
                return s
            kwords = re.findall(r"[a-z0-9]+", kw.lower())
            if len(kwords) > 1 and all(w in qwords for w in kwords):
                return s
    return None


def by_tool(tool_name: str) -> Skill | None:
    for s in SKILLS:
        if s.tool == tool_name:
            return s
    return None


def grounding(s: Skill) -> str:
    """System-prompt snippet appended when a skill is active."""
    req = ", ".join(s.required) if s.required else "(none)"
    g = (
        f"\n\nACTION — the user wants to {s.summary}. Use the '{s.tool}' tool. "
        f"Required fields: {req}. Ask the user ONLY for these — if one is missing, ask a short "
        f"question and do NOT call the tool yet (never invent values). Do NOT ask for any other "
        f"field (password, account type, profile group, etc.); every non-required field is set "
        f"automatically — never request it. Once you have the required fields, call the tool "
        f"with just those; the rest are filled in. The action pauses for the user to confirm "
        f"before it runs."
    )
    if s.lookups:
        g += (
            f" If the user names a type, category, or status (e.g. 'local', 'service', "
            f"'external'), pass it as-is in the matching field ({', '.join(s.lookups)}) — it "
            f"is resolved to the correct code automatically."
        )
    return g


def apply_defaults(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    """Fill a skill tool's default + derived fields the model omitted. Returns keys filled."""
    s = by_tool(tool_name)
    if not s:
        return []
    filled: list[str] = []
    for k, v in s.defaults.items():
        if not str(arguments.get(k) or "").strip():
            arguments[k] = v
            filled.append(k)
    for target, sources in s.derived.items():
        if str(arguments.get(target) or "").strip():
            continue
        parts = [str(arguments.get(src) or "").strip() for src in sources]
        if all(parts):                       # only derive when every source is present
            arguments[target] = " ".join(parts)
            filled.append(target)
    return filled


# ── Pre-create validation ───────────────────────────────────────────────────────────────

async def validate_inputs(tool_name: str, args: dict[str, Any], execute, request_headers) -> list[str]:
    """Run a skill's format + uniqueness checks BEFORE the mutation. Returns error messages
    (empty = ok). Uniqueness calls a read tool; a non-empty result means it already exists."""
    s = by_tool(tool_name)
    if not s:
        return []
    errs: list[str] = []
    for fld, pattern in s.validate.items():
        val = str(args.get(fld) or "").strip()
        if val and not re.match(pattern, val):
            errs.append(f"'{val}' is not a valid {_humanize(fld).lower()}.")
    for fld, check_tool in s.unique.items():
        val = str(args.get(fld) or "").strip()
        if not val:
            continue
        try:
            res = await execute(check_tool, {fld: val}, request_headers)
            data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
            if data not in (None, "", [], {}):
                errs.append(f"A record with {_humanize(fld).lower()} '{val}' already exists.")
        except Exception:  # noqa: BLE001 — a failing pre-check must not block a legitimate create
            pass
    return errs


# ── Dependency-chain execution ──────────────────────────────────────────────────────────

def dig(output: Any, path: str) -> Any:
    """Pull a value out of a GC tool result by a lenient dotted path. The payload is under
    `data` (a dict, or a list → first row); 'id' and 'data.id' both work; 'data.0.id' indexes."""
    cur = output
    parts = [p for p in str(path).split(".") if p]
    if parts and parts[0] != "data" and isinstance(cur, dict) and "data" in cur:
        cur = cur.get("data")                # default to the payload
    for p in parts:
        if p == "data" and isinstance(cur, dict) and "data" in cur:
            cur = cur.get("data")
        elif isinstance(cur, list):
            cur = cur[int(p)] if p.isdigit() and int(p) < len(cur) else (cur[0] if cur else None)
            if not p.isdigit() and isinstance(cur, dict):
                cur = cur.get(p)
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def resolve_step_args(step: Step, ctx: dict[str, Any]) -> dict[str, Any]:
    """Build a step's args from the shared context: '$name' → ctx['name']; else literal."""
    out: dict[str, Any] = {}
    for k, v in step.args.items():
        if isinstance(v, str) and v.startswith("$"):
            val = ctx.get(v[1:])
            if val is not None and str(val).strip() != "":
                out[k] = val
        else:
            out[k] = v
    return out
