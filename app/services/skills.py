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

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Skill:
    name: str
    keywords: tuple[str, ...]          # lower-cased intent substrings
    tool: str                          # backing MCP tool (pinned while the skill is active)
    required: tuple[str, ...] = ()     # fields the model must collect (ask if missing)
    defaults: dict[str, Any] = field(default_factory=dict)  # static fills if omitted
    derived: dict[str, tuple[str, ...]] = field(default_factory=dict)  # target ← join(sources)
    lookups: dict[str, str] = field(default_factory=dict)   # field → admin lookup code (name→code)
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
        # Master-data fields: the user may name a type/category/status; resolved to its
        # code via the ADMINISTRATION-app lookups (USER_ACCOUNT_*). Defaults above win if
        # the user says nothing.
        lookups={
            "accountType": "USER_ACCOUNT_TYPES",
            "accountCategory": "USER_ACCOUNT_CATEGORIES",
            "accountStatus": "USER_ACCOUNT_STATUS",
        },
        summary="create a new user account",
    ),
]


def match(query: str) -> Skill | None:
    """First skill whose intent keywords appear in the query, else None."""
    q = (query or "").lower()
    for s in SKILLS:
        if any(k in q for k in s.keywords):
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
