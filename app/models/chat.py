"""
models/chat.py
──────────────
Request / Response Pydantic models for the chat API.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.mcp import PendingAction


# ── Inbound ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Unique session identifier")
    message: str = Field(..., min_length=1, description="User message")
    stream: bool = Field(False, description="Enable SSE streaming")
    # Optional: override system prompt per-request
    system_prompt: str | None = Field(None, description="Override default system prompt")


class ConfirmRequest(BaseModel):
    session_id: str
    action_id: str
    confirmed: bool = True


class PromptExecuteRequest(BaseModel):
    session_id: str
    prompt_name: str
    arguments: dict[str, str] = Field(default_factory=dict)


# ── Outbound ──────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_results: list[dict[str, Any]] | None = None


class ChatResponse(BaseModel):
    session_id: str
    message_id: str
    assistant_message: str
    pending_action: PendingAction | None = None
    tool_calls_made: list[str] = Field(default_factory=list)
    model: str = ""
    usage: dict[str, int] | None = None
    finish_reason: str = "stop"


class StreamChunk(BaseModel):
    type: Literal["delta", "tool_use", "done", "error"]
    session_id: str
    content: str = ""
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
