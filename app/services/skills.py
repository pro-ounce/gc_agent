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
    defaults: dict[str, Any] = field(default_factory=dict)  # filled by the executor if omitted
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
        required=("userName", "fullName", "emailAddress"),
        defaults={"enabled": "Y", "adminFlag": "N"},
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
    return (
        f"\n\nACTION — the user wants to {s.summary}. Use the '{s.tool}' tool. "
        f"Required fields: {req}. If any required field is missing from the request, ASK the "
        f"user for it in a short question and do NOT call the tool yet — never invent values. "
        f"Once you have the required fields, call the tool; remaining fields are set "
        f"automatically. The action pauses for the user to confirm before it runs."
    )


def apply_defaults(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    """Fill a skill tool's default fields the model omitted. Returns the keys filled."""
    s = by_tool(tool_name)
    if not s:
        return []
    filled: list[str] = []
    for k, v in s.defaults.items():
        if not str(arguments.get(k) or "").strip():
            arguments[k] = v
            filled.append(k)
    return filled
