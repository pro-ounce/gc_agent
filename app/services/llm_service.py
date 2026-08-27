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
import re
from typing import Any, AsyncIterator

import httpx

import time

from ..commons import metrics as M
from ..commons.config import cfg
from ..services import runtime_config
from ..commons.logger import get_logger

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


_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _extract_text_tool_calls(text: str) -> tuple[list["ToolCall"], str]:
    """
    Recover tool calls a model emitted in the *content* instead of as a structured
    tool_calls entry. Handles two shapes:
      1. qwen2.5 / Hermes:  <tool_call>{"name": "...", "arguments": {...}}</tool_call>
         (Ollama doesn't always lift these into message.tool_calls → they arrive as text)
      2. bare JSON:         {"name": "getUser_get", "parameters": {...}}
    Returns (calls, cleaned_text) with the matched call(s) removed from the text.
    Conservative: only objects with a "name" plus "parameters"/"arguments" qualify.
    """
    calls: list[ToolCall] = []

    # (1) Qwen/Hermes <tool_call> tags first — the format qwen2.5 actually emits.
    def _take_tag(m: "re.Match[str]") -> str:
        try:
            obj = json.loads(m.group(1))
        except Exception:
            return m.group(0)  # leave malformed tags in place
        args = obj.get("arguments") or obj.get("parameters") if isinstance(obj, dict) else None
        if isinstance(obj, dict) and obj.get("name") and isinstance(args, dict):
            calls.append(ToolCall(id=f"tc_{obj['name']}", name=str(obj["name"]), input=args))
            return ""
        return m.group(0)

    text = _TOOL_CALL_TAG.sub(_take_tag, text).strip()
    if calls:
        return calls, text

    # (2) Bare JSON object fallback.
    if '"name"' not in text:
        return [], text
    out = text
    i = 0
    while True:
        start = out.find("{", i)
        if start == -1:
            break
        depth, end, in_str, esc = 0, -1, False, False
        for j in range(start, len(out)):
            c = out[j]
            if in_str:
                esc = (c == "\\") and not esc
                if c == '"' and not esc:
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            break
        try:
            obj = json.loads(out[start : end + 1])
        except Exception:
            i = start + 1
            continue
        args = obj.get("parameters") or obj.get("arguments") if isinstance(obj, dict) else None
        if isinstance(obj, dict) and obj.get("name") and isinstance(args, dict):
            calls.append(ToolCall(id=f"tc_{obj['name']}", name=str(obj["name"]), input=args))
            out = (out[:start] + out[end + 1 :]).strip()
            i = start
        else:
            i = end + 1
    return calls, out


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
            timeout=cfg.LLM_TIMEOUT_SECONDS,
        )
        log.bind(func="OllamaProvider").info(
            f"OLLAMA provider ready: {self._base_url}  model={self._model}"
        )

    @property
    def model(self) -> str:
        """Live model — runtime_config override (from /admin) or the configured default."""
        return runtime_config.get_str("LLM_MODEL") or self._model

    def _options(self) -> dict[str, Any]:
        """Sampling/context options — shared by every call path so streaming and
        non-streaming can't drift. Read from runtime_config so /admin edits apply live."""
        return {
            "temperature": runtime_config.get_float("LLM_TEMPERATURE"),
            "num_predict": runtime_config.get_int("LLM_MAX_TOKENS"),
            "num_ctx": runtime_config.get_int("LLM_NUM_CTX"),
        }

    def _keep_alive(self):
        """How long Ollama keeps the model resident after a call. '-1' = never unload, so the
        model stays warm and users never pay a cold-reload on the first chat after idle.
        Ollama wants a NUMBER for -1/seconds (a bare '-1' string fails its duration parse); a
        unit'd duration like '30m' stays a string."""
        v = (runtime_config.get_str("LLM_KEEP_ALIVE") or "-1").strip()
        return int(v) if v.lstrip("-").isdigit() else v

    async def warm(self) -> bool:
        """Pre-load the model into the GPU (called on startup) so the first real chat is fast.
        A 1-token generate with keep_alive=-1 loads + pins the weights. Best-effort."""
        try:
            resp = await self._client.post("/api/generate", json={
                "model": self.model, "prompt": "ok", "stream": False,
                "options": {"num_predict": 1}, "keep_alive": self._keep_alive(),
            })
            resp.raise_for_status()
            log.bind(func="warm").info(f"model warmed: {self.model}")
            return True
        except Exception as exc:  # noqa: BLE001 — warm-up must never break startup
            log.bind(func="warm").warning(f"model warm-up failed (non-fatal): {exc}")
            return False

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse:
        # Prepend system as a system-role message
        full_messages = [{"role": "system", "content": system}] + messages

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "stream": False,
            "options": self._options(),
            "keep_alive": self._keep_alive(),
        }
        if tools:
            payload["tools"] = tools  # Already in OpenAI format from tool_registry

        start = time.perf_counter()
        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            M.llm_calls_total.labels("ollama", self._model, "error").inc()
            # OLLAMA puts the real reason in the response body — surface it.
            log.error(
                f"OLLAMA {exc.response.status_code} on /api/chat: {exc.response.text[:800]} | "
                f"last msg roles={[m.get('role') for m in full_messages[-4:]]}"
            )
            raise
        except Exception:
            M.llm_calls_total.labels("ollama", self._model, "error").inc()
            raise
        finally:
            M.llm_duration.labels("ollama", self._model).observe(time.perf_counter() - start)
        M.llm_calls_total.labels("ollama", self._model, "success").inc()
        if data.get("prompt_eval_count"):
            M.llm_tokens_total.labels("ollama", self._model, "input").inc(data["prompt_eval_count"])
        if data.get("eval_count"):
            M.llm_tokens_total.labels("ollama", self._model, "output").inc(data["eval_count"])

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

        # Fallback: smaller models (llama3.1 8B) sometimes emit the tool call as JSON in
        # the content instead of a structured tool_calls entry. Recover it and strip it
        # from the visible answer so raw JSON never leaks to the user.
        if not tool_calls and text:
            recovered, text = _extract_text_tool_calls(text)
            tool_calls.extend(recovered)

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

    async def health(self) -> dict[str, Any]:
        """Probe the Ollama runtime (lists local models). UP if it responds."""
        try:
            resp = await self._client.get("/api/tags", timeout=5.0)
            resp.raise_for_status()
            models = [m.get("name") for m in resp.json().get("models", [])]
            return {"status": "UP", "provider": "ollama", "model": self._model,
                    "model_available": self._model in models or any(str(self._model).split(":")[0] in str(x) for x in models)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "DOWN", "provider": "ollama", "model": self._model, "error": str(exc)}

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[str]:
        full_messages = [{"role": "system", "content": system}] + messages
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "stream": True,
            "options": self._options(),
            "keep_alive": self._keep_alive(),
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

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[tuple[str, Any]]:
        """
        Streaming chat that also surfaces tool calls, for the agentic loop.
        Yields ("delta", text) for content tokens, then ("tool_calls", [ToolCall-dict, …])
        if the model called tools, and ("done", finish_reason) at the end.
        """
        full_messages = [{"role": "system", "content": system}] + messages
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "stream": True,
            "options": self._options(),
            "keep_alive": self._keep_alive(),
        }
        if tools:
            payload["tools"] = tools

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message", {})
                if msg.get("content"):
                    yield ("delta", msg["content"])
                if msg.get("tool_calls"):
                    calls = []
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        calls.append(
                            {"id": tc.get("id") or f"tc_{fn.get('name', '')}",
                             "name": fn.get("name", ""), "input": args or {}}
                        )
                    yield ("tool_calls", calls)
                if chunk.get("done"):
                    # Ollama reports token counts + durations (ns) on the final chunk.
                    yield (
                        "usage",
                        {
                            "prompt_tokens": chunk.get("prompt_eval_count", 0),
                            "completion_tokens": chunk.get("eval_count", 0),
                            "llm_seconds": (chunk.get("total_duration", 0) or 0) / 1e9,
                        },
                    )
                    yield ("done", chunk.get("done_reason", "stop"))


# ── Singleton ─────────────────────────────────────────────────────────────────

_llm: OllamaProvider | None = None


def llm() -> OllamaProvider:
    global _llm
    if _llm is None:
        _llm = OllamaProvider()
    return _llm
