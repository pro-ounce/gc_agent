"""
services/llm_service.py
────────────────────────
LLM provider — OLLAMA (open-source, self-hosted).

Uses the OLLAMA REST API (/api/chat) with OpenAI-compatible tool calling
supported since OLLAMA ≥ 0.3.

Tool schema format: OpenAI function-calling style
  {
    "type": "function",
    "function": {
      "name": "...",
      "description": "...",
      "parameters": { "type": "object", "properties": {...}, "required": [...] }
    }
  }
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from app.commons.config import cfg
from app.commons.logger import get_logger

log = get_logger(__name__)


# ── Response models ───────────────────────────────────────────────────────────

class ToolCall:
    def __init__(self, id: str, name: str, input: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.input = input

    def __repr__(self) -> str:
        return f"ToolCall(id={self.id!r}, name={self.name!r})"


class LLMResponse:
    def __init__(
        self,
        text: str,
        tool_calls: list[ToolCall],
        finish_reason: str,
        model: str,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.model = model
        self.usage = usage or {}

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


# ── OLLAMA provider ───────────────────────────────────────────────────────────

class OllamaProvider:
    """
    OLLAMA REST API — open-source, self-hosted LLM.
    Supports tool-use via /api/chat with OLLAMA ≥ 0.3.

    Tools are passed in OpenAI function-calling format.
    """

    def __init__(self) -> None:
        self._base_url = cfg.OLLAMA_BASE_URL
        self._model = cfg.LLM_MODEL
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=120.0,
        )
        log.bind(func="OllamaProvider").info(
            f"OLLAMA provider ready: {self._base_url}  model={self._model}"
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse:
        # Prepend system as a system-role message
        full_messages = [{"role": "system", "content": system}] + messages

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "stream": False,
            "options": {
                "temperature": cfg.LLM_TEMPERATURE,
                "num_predict": cfg.LLM_MAX_TOKENS,
            },
        }
        if tools:
            payload["tools"] = tools  # Already in OpenAI format from tool_registry

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        msg = data.get("message", {})
        text = msg.get("content") or ""
        tool_calls: list[ToolCall] = []

        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"tc_{fn.get('name', 'unknown')}",
                    name=fn.get("name", ""),
                    input=args,
                )
            )

        usage: dict[str, int] = {}
        if "prompt_eval_count" in data:
            usage["input_tokens"] = data["prompt_eval_count"]
        if "eval_count" in data:
            usage["output_tokens"] = data["eval_count"]

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=data.get("done_reason", "stop"),
            model=self._model,
            usage=usage,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[str]:
        full_messages = [{"role": "system", "content": system}] + messages
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "stream": True,
        }

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                except json.JSONDecodeError:
                    pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_llm: OllamaProvider | None = None


def llm() -> OllamaProvider:
    global _llm
    if _llm is None:
        _llm = OllamaProvider()
    return _llm
