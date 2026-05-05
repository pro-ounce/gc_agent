"""
models/session.py
─────────────────
Session data model — persisted to Redis as JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Message(BaseModel):
    role: str          # "user" | "assistant" | "tool" | "tool_result"
    content: str | list[dict[str, Any]]
    tool_use_id: str | None = None
    tool_name: str | None = None
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

    def add_message(self, role: str, content: str | list[dict[str, Any]], **kw: Any) -> None:
        self.messages.append(Message(role=role, content=content, **kw))
        self.touch()

    def to_llm_messages(self) -> list[dict[str, Any]]:
        """Convert session history to the format expected by the LLM."""
        import json as _json

        result: list[dict[str, Any]] = []
        for msg in self.messages:
            if msg.role in ("user", "assistant"):
                result.append({"role": msg.role, "content": msg.content})
            elif msg.role == "tool_use":
                # tool input stored as JSON string; deserialise back to dict for LLM
                raw_input = msg.content
                if isinstance(raw_input, str):
                    try:
                        tool_input: Any = _json.loads(raw_input)
                    except (ValueError, TypeError):
                        tool_input = raw_input
                else:
                    tool_input = raw_input

                # Append tool_use block into the last assistant message if possible
                if result and result[-1]["role"] == "assistant":
                    if isinstance(result[-1]["content"], list):
                        result[-1]["content"].append(
                            {"type": "tool_use", "id": msg.tool_use_id, "name": msg.tool_name, "input": tool_input}
                        )
                    else:
                        result[-1]["content"] = [
                            {"type": "text", "text": result[-1]["content"]},
                            {"type": "tool_use", "id": msg.tool_use_id, "name": msg.tool_name, "input": tool_input},
                        ]
                else:
                    result.append(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": msg.tool_use_id, "name": msg.tool_name, "input": tool_input}
                            ],
                        }
                    )
            elif msg.role == "tool_result":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_use_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
        return result
