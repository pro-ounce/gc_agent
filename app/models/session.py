"""
models/session.py
─────────────────
Session data model — persisted to Redis as JSON.
Message history is stored in OpenAI-compatible format (used by OLLAMA).
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.commons.config import cfg
from app.services import runtime_config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Message(BaseModel):
    role: str          # "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None   # for role="tool" results
    tool_name: str | None = None
    # Serialised JSON list of OpenAI tool_call objects (on assistant messages)
    tool_calls_json: str | None = None
    timestamp: str = Field(default_factory=_now_iso)


class Session(BaseModel):
    session_id: str
    user_id: str | None = None
    messages: list[Message] = Field(default_factory=list)
    pending_action_id: str | None = None
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def add_user(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))
        self.touch()

    def add_assistant(self, content: str, tool_calls: list[dict[str, Any]] | None = None) -> None:
        msg = Message(role="assistant", content=content)
        if tool_calls:
            msg.tool_calls_json = _json.dumps(tool_calls)
        self.messages.append(msg)
        self.touch()

    def add_tool_result(self, tool_call_id: str, tool_name: str, content: str) -> None:
        self.messages.append(
            Message(role="tool", content=content, tool_call_id=tool_call_id, tool_name=tool_name)
        )
        self.touch()

    # Convenience shim so old call sites still work
    def add_message(self, role: str, content: Any, **kw: Any) -> None:
        if isinstance(content, dict):
            content = _json.dumps(content)
        elif not isinstance(content, str):
            content = str(content)
        self.messages.append(Message(role=role, content=content, **kw))
        self.touch()

    def _windowed_messages(self) -> list["Message"]:
        """Most-recent messages within MAX_HISTORY_CHARS, so a long session can't bloat the
        prompt. Walks back accumulating content length, then trims the front to the first
        `user` turn — a safe conversation boundary that never orphans a tool_call/tool_result
        pair. 0 = unlimited."""
        budget = runtime_config.get_int("MAX_HISTORY_CHARS")
        msgs = self.messages
        if budget <= 0 or len(msgs) <= 1:
            return msgs
        total = 0
        start = 0
        for i in range(len(msgs) - 1, -1, -1):
            total += len(msgs[i].content or "")
            if total > budget:
                start = i + 1
                break
        window = msgs[start:]
        for i, m in enumerate(window):
            if m.role == "user":
                return window[i:]
        return window  # no user turn in window (unusual) — keep as-is

    def to_llm_messages(self) -> list[dict[str, Any]]:
        """
        Convert session history to OpenAI-compatible message list for OLLAMA.

        Roles used:
          user      → regular user message
          assistant → LLM response (may include tool_calls list)
          tool      → result of a tool call (tool_call_id required)
        """
        result: list[dict[str, Any]] = []
        for msg in self._windowed_messages():
            if msg.role == "user":
                result.append({"role": "user", "content": msg.content})

            elif msg.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": msg.content}
                if msg.tool_calls_json:
                    try:
                        entry["tool_calls"] = _json.loads(msg.tool_calls_json)
                    except (ValueError, TypeError):
                        pass
                result.append(entry)

            elif msg.role == "tool":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "",
                        "content": msg.content,
                    }
                )

            # Legacy roles written by earlier versions of chat_service
            elif msg.role == "tool_use":
                # Reconstruct as assistant message with tool_calls
                try:
                    args = _json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                except (ValueError, TypeError):
                    args = {}
                tool_calls = [
                    {
                        "id": msg.tool_call_id or "",
                        "type": "function",
                        "function": {"name": msg.tool_name or "", "arguments": _json.dumps(args)},
                    }
                ]
                if result and result[-1]["role"] == "assistant":
                    result[-1].setdefault("tool_calls", []).extend(tool_calls)
                else:
                    result.append({"role": "assistant", "content": "", "tool_calls": tool_calls})

            elif msg.role == "tool_result":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "",
                        "content": msg.content,
                    }
                )

        return result
