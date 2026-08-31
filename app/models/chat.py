"""
models/chat.py
──────────────
Request / Response Pydantic models for the chat API.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models.mcp import PendingAction


# ── Inbound ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Unique session identifier")
    message: str = Field(..., min_length=1, description="User message")
    stream: bool = Field(False, description="Enable SSE streaming")
    # Optional: override system prompt per-request
    system_prompt: str | None = Field(None, description="Override default system prompt")
    # Output verbosity: concise | standard | detailed
    detail: str | None = Field(None, description="Output level: concise|standard|detailed")


class ConfirmRequest(BaseModel):
    session_id: str
    action_id: str
    confirmed: bool = True


class PromptExecuteRequest(BaseModel):
    session_id: str
    prompt_name: str
    arguments: dict[str, str] = Field(default_factory=dict)


# ── Outbound ──────────────────────────────────────────────────────────────────

class UIBlock(BaseModel):
    """A typed, render-ready piece of an assistant reply. Built deterministically from
    tool results so the client renders structured data (cards/tables) instead of prose —
    faster (near-zero LLM generation for data) and accurate (from source, not the model).
    Clients that don't understand blocks fall back to `ChatResponse.assistant_message`."""
    type: Literal["text", "fields", "table", "list", "code", "notice"]
    title: str | None = None
    text: str | None = None                       # text / notice / code body
    items: list[Any] | None = None                # fields: [{label,value}] · list: [str]
    columns: list[str] | None = None              # table header
    rows: list[list[Any]] | None = None           # table rows
    level: str | None = None                      # notice: info|success|warn|error
    language: str | None = None                   # code
    source_tool: str | None = None                # provenance


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_results: list[dict[str, Any]] | None = None


class Suggestion(BaseModel):
    """A chip the widget renders under the assistant's message. On click the widget
    prefills the input with `send` (defaults to `label`) so the user can edit + send."""
    label: str
    send: str = ""            # message to prefill on click; falls back to label when blank
    icon: str | None = None   # optional icon key the widget maps (app, role, check, list, …)


class ChatResponse(BaseModel):
    session_id: str
    message_id: str
    assistant_message: str
    pending_action: PendingAction | None = None
    tool_calls_made: list[str] = Field(default_factory=list)
    blocks: list[UIBlock] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)  # per-turn chips (guided flows)
    task: dict[str, Any] | None = None   # background task descriptor (id/status/title) to poll
    model: str = ""
    usage: dict[str, int] | None = None
    finish_reason: str = "stop"


class StreamChunk(BaseModel):
    type: Literal["delta", "tool_use", "confirm_required", "done", "error"]
    session_id: str
    content: str = ""
    blocks: list[UIBlock] = Field(default_factory=list)  # populated on the final "done" chunk
    suggestions: list[Suggestion] = Field(default_factory=list)  # per-turn chips (guided flows)
    task: dict[str, Any] | None = None   # background task descriptor (id/status/title) to poll
    pending_action: PendingAction | None = None
    finish_reason: str | None = None
    error: str | None = None


# ── Session summary ───────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    session_id: str
    message_count: int
    created_at: str
    updated_at: str
    user_id: str | None = None
