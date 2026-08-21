"""
services/runtime_config.py
──────────────────────────
Runtime-tunable agent parameters, editable from the /admin UI WITHOUT a restart.

Mirrors the RAG app's admin_config pattern: overrides persist in the store (OpenSearch,
via the same redis_* helpers the rest of the agent uses), a short-TTL in-memory cache makes
a change propagate to every worker within a few seconds, and each param falls back to its
.env/cfg default when unset. Hot-path code calls runtime_config.get_*(KEY) instead of
reading cfg/flags directly, so a saved change takes effect on the next turn.
"""
from __future__ import annotations

import time
from typing import Any

from ..commons.config import cfg
from ..commons.flags import flags
from ..commons.logger import get_logger
from ..connections import redis_get_json, redis_set_json

log = get_logger(__name__)

_STORE_KEY = "admin:runtime_config"
_CACHE_TTL = 15.0  # seconds — how fast a saved change reaches every worker


def _spec(key: str, label: str, group: str, type_: str, default: Any, desc: str, **extra) -> dict:
    return {"key": key, "label": label, "group": group, "type": type_,
            "default": default, "description": desc, **extra}


def _param_specs() -> list[dict]:
    """The tunable surface. Defaults are the current .env/cfg values (the fallback when a
    param has no override). type: int | float | bool | string | select."""
    return [
        # ── Model ──
        _spec("LLM_MODEL", "Model", "Model", "select", cfg.LLM_MODEL,
              "Ollama model for chat + tool-calling. Larger = better tool-calls, slower.",
              options=["qwen2.5:32b", "qwen2.5:14b", "qwen2.5:7b", "llama3.1:latest"]),
        _spec("LLM_TEMPERATURE", "Temperature", "Model", "float", cfg.LLM_TEMPERATURE,
              "Sampling temperature. 0 = deterministic; higher = more varied.", min=0.0, max=2.0, step=0.1),
        _spec("LLM_MAX_TOKENS", "Max output tokens", "Model", "int", cfg.LLM_MAX_TOKENS,
              "Cap on generated tokens per call. Lower = shorter answers, faster.", min=64, max=8192),
        _spec("LLM_NUM_CTX", "Context window", "Model", "int", cfg.LLM_NUM_CTX,
              "Ollama context size (prompt + output tokens).", min=2048, max=65536),
        # ── Retrieval ──
        _spec("TOOL_RAG_ENABLED", "Tool-RAG", "Retrieval", "bool", flags.tool_rag_enabled,
              "Retrieve only the top-K relevant tools per query (vs sending all)."),
        _spec("TOOL_RAG_TOP_K", "Tools per query (top-K)", "Retrieval", "int", cfg.TOOL_RAG_TOP_K,
              "How many tools RAG hands the model. Fewer = smaller prompt, faster, cleaner tool-calls.",
              min=3, max=60),
        # ── Agent loop ──
        _spec("LLM_MAX_ITERATIONS", "Max tool iterations", "Agent loop", "int", cfg.LLM_MAX_ITERATIONS,
              "Max LLM↔tool rounds before the agent must answer.", min=1, max=15),
        _spec("MAX_HISTORY_CHARS", "History budget (chars)", "Agent loop", "int", cfg.MAX_HISTORY_CHARS,
              "Conversation history sent to the model (~4 chars/token). 0 = unlimited.", min=0, max=80000),
        # ── Toggles ──
        _spec("CHATBOT_READ_ONLY", "Read-only chatbot", "Toggles", "bool", flags.chatbot_read_only,
              "Never offer mutating tools (create/update/delete/activate) to the model."),
        _spec("TOOL_RISK_CONFIRMATION", "Risk confirmation", "Toggles", "bool", flags.tool_risk_confirmation,
              "Pause for user confirmation before HIGH/CRITICAL tools run."),
        _spec("AGENT_STRICT_GROUNDING", "Strict grounding", "Toggles", "bool", flags.strict_grounding,
              "Report only facts the tools returned — no interpretation or embellishment."),
        _spec("LOG_USER_PROMPTS", "Log user prompts", "Toggles", "bool", flags.log_user_prompts,
              "Log each question + the tools RAG retrieved (chat_prompt line)."),
    ]


_SPECS_BY_KEY = {s["key"]: s for s in _param_specs()}

# ── override cache ────────────────────────────────────────────────────────────
_cache: dict[str, Any] | None = None
_cache_exp: float = 0.0


def _now() -> float:
    try:
        return time.monotonic()
    except Exception:  # noqa: BLE001
        return time.time()


def _overrides() -> dict[str, Any]:
    global _cache, _cache_exp
    if _cache is not None and _now() < _cache_exp:
        return _cache
    try:
        data = redis_get_json(_STORE_KEY)
        data = data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"runtime_config read failed (using defaults): {exc}")
        data = {}
    _cache = data
    _cache_exp = _now() + _CACHE_TTL
    return data


def _persist(ov: dict[str, Any]) -> None:
    global _cache, _cache_exp
    try:
        redis_set_json(_STORE_KEY, ov)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"runtime_config persist failed: {exc}")
    _cache = ov
    _cache_exp = _now() + _CACHE_TTL


def _coerce(spec: dict, val: Any) -> Any:
    t = spec["type"]
    try:
        if t == "int":
            return int(val)
        if t == "float":
            return float(val)
        if t == "bool":
            return val if isinstance(val, bool) else str(val).strip().lower() in ("1", "true", "yes", "on")
        return str(val)
    except Exception:  # noqa: BLE001
        return spec["default"]


def _clamp(spec: dict, val: Any) -> Any:
    if spec["type"] in ("int", "float"):
        if "min" in spec:
            val = max(spec["min"], val)
        if "max" in spec:
            val = min(spec["max"], val)
    return val


# ── public read API (hot path) ────────────────────────────────────────────────
def get(key: str) -> Any:
    spec = _SPECS_BY_KEY.get(key)
    if spec is None:
        return None
    ov = _overrides()
    if key in ov and ov[key] is not None:
        return _coerce(spec, ov[key])
    return spec["default"]


def get_int(key: str) -> int:
    return int(get(key))


def get_float(key: str) -> float:
    return float(get(key))


def get_bool(key: str) -> bool:
    return bool(get(key))


def get_str(key: str) -> str:
    return str(get(key))


# ── admin API (UI) ────────────────────────────────────────────────────────────
def get_all() -> list[dict]:
    """Every param with its live value + default + whether it's overridden — for the UI."""
    ov = _overrides()
    out = []
    for s in _param_specs():
        k = s["key"]
        out.append({**s, "value": get(k), "overridden": bool(k in ov and ov[k] is not None)})
    return out


def set_many(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist overrides. A value of None/'' clears that param (back to default).
    Returns the applied (coerced/clamped) values."""
    ov = dict(_overrides())
    applied: dict[str, Any] = {}
    for k, v in (updates or {}).items():
        spec = _SPECS_BY_KEY.get(k)
        if spec is None:
            continue
        if v is None or (isinstance(v, str) and v.strip() == ""):
            ov.pop(k, None)
            continue
        val = _clamp(spec, _coerce(spec, v))
        if spec["type"] == "select" and spec.get("options") and val not in spec["options"]:
            continue  # ignore an out-of-range select
        ov[k] = val
        applied[k] = val
    _persist(ov)
    log.bind(func="runtime_config").info(f"runtime config updated: {applied}")
    return applied


def reset() -> None:
    _persist({})
    log.bind(func="runtime_config").info("runtime config reset to defaults")
