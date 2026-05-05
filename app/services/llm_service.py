"""
services/llm_service.py
────────────────────────
LLM provider abstraction.

Supported providers:
  - anthropic  (default) — uses claude-3-5-sonnet via the Anthropic Python SDK
  - ollama               — uses local OLLAMA REST API for open-source models

The service returns a structured LLMResponse that includes tool_calls,
the assistant text, finish_reason, and token usage.
Both streaming and non-streaming modes are supported.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
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


# ── Abstract base ─────────────────────────────────────────────────────────────

class BaseLLMProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[str]:
        ...


# ── Anthropic provider ────────────────────────────────────────────────────────

class AnthropicProvider(BaseLLMProvider):
    def __init__(self) -> None:
        try:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)
            self._model = cfg.LLM_MODEL
            log.bind(func="AnthropicProvider").info(f"Anthropic provider initialised: {self._model}")
        except ImportError:
            raise RuntimeError(
                "anthropic package not installed. Run: pip install anthropic"
            )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": cfg.LLM_MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},  # Prompt caching
                }
            ],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        if hasattr(response.usage, "cache_read_input_tokens"):
            usage["cache_read_input_tokens"] = response.usage.cache_read_input_tokens
        if hasattr(response.usage, "cache_creation_input_tokens"):
            usage["cache_creation_input_tokens"] = response.usage.cache_creation_input_tokens

        return LLMResponse(
            text=" ".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=response.stop_reason or "stop",
            model=response.model,
            usage=usage,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[str]:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": cfg.LLM_MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text


# ── OLLAMA provider ───────────────────────────────────────────────────────────

class OllamaProvider(BaseLLMProvider):
    """
    OLLAMA REST API adapter for local open-source models.
    Supports tool-use via the /api/chat endpoint with Ollama ≥0.3.
    """

    def __init__(self) -> None:
        self._base_url = cfg.OLLAMA_BASE_URL
        self._model = cfg.OLLAMA_MODEL
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=120.0)
        log.bind(func="OllamaProvider").info(f"OLLAMA provider initialised: {self._model}")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse:
        # Prepend system as a system message for Ollama
        ollama_messages = [{"role": "system", "content": system}] + messages

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": ollama_messages,
            "stream": False,
            "options": {"temperature": cfg.LLM_TEMPERATURE},
        }
        if tools:
            # Ollama ≥0.3 supports OpenAI-style tool_calls
            payload["tools"] = [_anthropic_to_openai_tool(t) for t in tools]

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        msg = data.get("message", {})
        text = msg.get("content", "")
        tool_calls: list[ToolCall] = []

        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", f"tc_{fn.get('name', 'unknown')}"),
                    name=fn.get("name", ""),
                    input=args,
                )
            )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=data.get("done_reason", "stop"),
            model=self._model,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[str]:
        ollama_messages = [{"role": "system", "content": system}] + messages
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": ollama_messages,
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


def _anthropic_to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert Anthropic tool schema to OpenAI function-call format for Ollama."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        },
    }


# ── Factory ───────────────────────────────────────────────────────────────────

def get_llm_provider() -> BaseLLMProvider:
    provider = cfg.LLM_PROVIDER.lower()
    if provider == "anthropic":
        return AnthropicProvider()
    if provider == "ollama":
        return OllamaProvider()
    raise ValueError(f"Unknown LLM provider: '{provider}'. Choose 'anthropic' or 'ollama'.")


# Module-level singleton (lazy)
_llm: BaseLLMProvider | None = None


def llm() -> BaseLLMProvider:
    global _llm
    if _llm is None:
        _llm = get_llm_provider()
    return _llm
