"""
agents/registry.py
───────────────────
The `ai-service` hosts multiple agents keyed by requirement type (chatbot,
document helper, …). Each agent differs only by prompt / allowed toolset /
model — they share the same core (chat loop, session store, MCP client,
gateway auth). Adding a new agent = one AgentSpec here; no infra/route change
(the gateway route `/api/ai/**` already covers `/api/ai/{agent}/…`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    key: str                        # URL segment: /api/ai/{key}/chat
    name: str                       # human label
    system_prompt: str
    # None = expose every discovered MCP tool. Otherwise only tools whose name
    # starts with one of these prefixes are offered to the LLM (scoping by domain).
    tool_prefixes: tuple[str, ...] | None = None
    model: str | None = None        # None = service default (LLM_MODEL)


_CHATBOT = AgentSpec(
    key="chatbot",
    name="GC Assistant",
    system_prompt=(
        "You are the GC Assistant, the built-in helper for the GovConnect (Compass) "
        "platform inside Smart HuB. You help authenticated users with platform tasks — "
        "user and role administration, account and access questions, and looking up "
        "platform data. Use the available tools to read or act on real platform data "
        "rather than guessing. Be concise, professional, and clear. When a request would "
        "perform a privileged or changing action, state plainly what will happen before "
        "doing it. If something is outside the platform's scope, say so briefly.\n\n"
        "TOOL USE RULES:\n"
        "- When you call a tool, you MUST provide every REQUIRED parameter. Take each "
        "value verbatim from the user's message — e.g. a username, id, code, or name they "
        "stated. If the user said 'user GCADMIN', pass userName=\"GCADMIN\".\n"
        "- Never call a tool with a required parameter missing, empty, or a placeholder "
        "like 'your_username'. If a required value is genuinely not provided and you "
        "cannot infer it, ask the user for it instead of calling the tool.\n"
        "- Do not put authentication, tokens, or headers in tool parameters — those are "
        "handled automatically.\n"
        "- When you need data, CALL the tool immediately in the same turn. NEVER reply with "
        "only a statement of intent like 'I will fetch…' / 'Let me look up…' and then stop — "
        "that leaves the user with no answer. Either make the tool call now, or give the final "
        "answer / ask for a missing value.\n\n"
        "RESPONSE FORMATTING (Markdown):\n"
        "- Reply in clean Markdown. Put each list item on its OWN line, starting with '- ' "
        "and a real newline between items — never run bullets together on one line.\n"
        "- Use **bold** for field labels (e.g. '- **Email:** admin@…').\n"
        "- Separate distinct sections with a blank line. Use short headings (##) when it "
        "helps. Keep it concise and scannable — no walls of text."
    ),
    tool_prefixes=None,             # chatbot: all discovered tools
)

# Appended to the system prompt when flags.strict_grounding is on (AGENT_STRICT_GROUNDING).
STRICT_GROUNDING_INSTRUCTION = (
    "\n\nGROUNDING (strict): Report ONLY facts returned by the tools. Do not infer, assume, "
    "or add interpretive notes beyond the tool data. If a field is empty or missing, say it is "
    "not set — never speculate about why (e.g. do not claim an account is 'locked' unless a tool "
    "field says so)."
)

# Future agents drop in here, e.g.:
#   AgentSpec(key="document", name="Document Helper",
#             system_prompt="…", tool_prefixes=("reporting_", "document_"))

_REGISTRY: dict[str, AgentSpec] = {a.key: a for a in (_CHATBOT,)}


def get_agent(key: str) -> AgentSpec | None:
    return _REGISTRY.get(key.lower())


def list_agents() -> list[str]:
    return list(_REGISTRY.keys())
