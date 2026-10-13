"""
services/meta.py
────────────────
Read-only INTROSPECTION handled deterministically (no LLM, no MCP mutation): list the
agent's skills, search/list the platform tools, and show full detail for one tool. Lets a
functional consultant discover what the agent can do and what a tool is, by name alone.

Runs before flow-start / skill routing when no guided flow is active.
"""
from __future__ import annotations

import re
from typing import Any

from . import skill_store
from .flows import FlowResult
from ..models.chat import UIBlock

# ── intent patterns ────────────────────────────────────────────────────────────
_LIST_SKILLS = re.compile(r"\b(list|show|see|which|what)\b[^.?]{0,25}\bskills?\b", re.I)
_WHAT_CAN = re.compile(r"\bwhat can you (do|help (me )?with)\b|\byour (capabilities|skills)\b", re.I)
_TOOLS_FOR = re.compile(r"\btools?\b[^.?]{0,10}\b(?:for|about|related to|to)\b\s+(.+)$", re.I)
_LIST_TOOLS = re.compile(r"\b(list|show|see|which|what|how many)\b[^.?]{0,25}\btools?\b", re.I)
_TOOL_DETAIL = re.compile(
    r"\b(?:about|info(?:rmation)?(?: on| about)?|details?(?: of| on| about)?|what does|explain|describe|tell me about)\b"
    r"[^.?]{0,12}\b([A-Za-z][A-Za-z0-9_]*_(?:get|post|put|delete))\b", re.I)


async def handle(message: str, headers: dict[str, str] | None) -> FlowResult | None:
    """Return a read-only introspection answer, or None to fall through to normal routing."""
    msg = (message or "").strip()
    if not msg:
        return None

    m = _TOOL_DETAIL.search(msg)                     # "about addUser_post" / "what does X do"
    if m:
        return await _tool_detail(m.group(1), headers)

    m = _TOOLS_FOR.search(msg)                       # "tools for licenses"
    if m:
        return await _tools_for(m.group(1).strip(" ?.").strip())

    if _LIST_SKILLS.search(msg) or _WHAT_CAN.search(msg):
        return _skills_list()

    if _LIST_TOOLS.search(msg):                      # "list tools" (too many to dump → guide)
        return _tools_hint()

    return None


def _skills_list() -> FlowResult:
    """A proper capabilities MENU — an actionable card (tap a row to start) rather than a plain
    bullet dump. Each item carries the REAL launch phrase in `send`, so the widget fires that flow
    on tap; `icon` is a key the widget maps to a glyph. Curated top capabilities first, then any
    other registered skill not already covered so the menu stays complete."""
    from .skills import SKILLS
    # Curated, well-captioned primary capabilities, in the order a budget admin reaches for them.
    items: list[dict[str, Any]] = [
        {"label": "Create a user", "send": "create a user", "icon": "user",
         "desc": "Guided intake — name, email, account type — then assign apps & roles."},
        {"label": "Onboard access", "send": "assign an application", "icon": "app",
         "desc": "Assign applications and roles to a user, one at a time."},
        {"label": "Look up a user", "send": "look up user", "icon": "search",
         "desc": "A user's account details, by username."},
        {"label": "Generate an access report", "send": "generate report", "icon": "report",
         "desc": "A user's application-access report."},
        {"label": "Generate a baseline", "send": "generate a formulation baseline", "icon": "baseline",
         "desc": "A formulation baseline for an application across fiscal years."},
        {"label": "Set up a data call", "send": "set up a data call", "icon": "datacall",
         "desc": "A data call with attendees and reminders."},
        {"label": "Edit an assignment", "send": "make default", "icon": "edit",
         "desc": "Set a default app, or enable / disable / favourite a role."},
        {"label": "Remove access", "send": "remove role", "icon": "remove",
         "desc": "Un-assign a role, or remove a whole application from a user."},
        {"label": "Create a new skill", "send": "create a skill", "icon": "skill",
         "desc": "Teach me a new action from your own input."},
        {"label": "Explore tools", "send": "list tools", "icon": "tools",
         "desc": "Browse the platform's tools by topic, or get detail on one."},
    ]
    seen = {"create_user", "assign_access", "user_lookup", "generate_report",
            "formulation_baseline", "create_data_call", "edit_access", "remove_access",
            "remove_application", "create_skill"}
    for s in SKILLS:
        if s.name in seen or not s.keywords:
            continue
        seen.add(s.name)
        items.append({"label": (s.summary or s.name).capitalize(), "send": s.keywords[0],
                      "icon": "spark", "desc": ""})
    block = UIBlock(type="menu", title="What I can help with", items=items)
    return FlowResult(
        message="Here's what I can help you with — tap any one to get started:",
        blocks=[block])


def _tools_hint() -> FlowResult:
    return FlowResult(message=(
        "I have hundreds of platform tools — too many to list at once. Tell me a topic and I'll "
        "show the relevant ones, e.g. **tools for licenses**, **tools for users**, or "
        "**tools for applications**. For one tool, ask **about <tool_name>**."))


async def _tools_for(topic: str) -> FlowResult:
    if not topic:
        return _tools_hint()
    sugg = await skill_store.suggest_tools(topic, 8)
    if not sugg:
        return FlowResult(message=f"I couldn't find tools related to “{topic}”. Try another topic.")
    lines = "\n".join(
        f"• **{s['name']}** — {s['desc'] or 'No description available.'}"
        + (f"  \n  _needs: {', '.join(s['required'])}_" if s.get("required") else "")
        for s in sugg)
    return FlowResult(message=(f"Tools related to **{topic}**:\n\n{lines}\n\n"
                               "Ask **about <tool>** for full detail."))


async def _tool_detail(name: str, headers: dict[str, str] | None) -> FlowResult:
    picked = await skill_store.find_tool(name)
    det = await skill_store.tool_detail(picked) if picked else None
    if not det:
        return FlowResult(message=f"I couldn't find a tool called “{name}”.")
    needs = ", ".join(det["required"]) or "nothing extra"
    allp = ", ".join(det["params"]) or "—"
    return FlowResult(message=(f"**{det['name']}**\n{det['desc'] or 'No description available.'}\n\n"
                               f"_Required inputs: {needs}_\n_All inputs: {allp}_"))
