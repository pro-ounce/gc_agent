"""
commons/config.py
─────────────────
Environment-driven configuration with type-safe helpers and automatic secret masking.
Follows the same pattern as delivery/app/commons/config.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env.local first, then .env as fallback
_ROOT = Path(__file__).resolve().parents[2]  # agent/ (app/commons/config.py → up 2)
load_dotenv(_ROOT / ".env.local", override=False)
load_dotenv(_ROOT / ".env", override=False)


# ── Primitive helpers ──────────────────────────────────────────────────────────

def env_str(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    if value is not None:
        return value.strip()
    return default


def env_str_required(key: str) -> str:
    value = env_str(key)
    if value is None:
        raise RuntimeError(f"Required environment variable '{key}' is not set.")
    return value


def env_int(key: str, default: int | None = None) -> int | None:
    raw = env_str(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(key: str, default: float | None = None) -> float | None:
    raw = env_str(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = env_str(key)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def env_list(key: str, separator: str = ",") -> list[str]:
    raw = env_str(key)
    if not raw:
        return []
    return [item.strip() for item in raw.split(separator) if item.strip()]


# ── Secret masking ─────────────────────────────────────────────────────────────

_SECRET_KEYS: frozenset[str] = frozenset(
    {"password", "secret", "token", "key", "credential", "api_key", "jwt_secret"}
)


def _mask(key: str, value: Any) -> Any:
    lower = key.lower()
    if any(s in lower for s in _SECRET_KEYS):
        if isinstance(value, str) and len(value) > 4:
            return value[:4] + "****"
        return "****"
    return value


# ── App configuration ──────────────────────────────────────────────────────────

class AppConfig:
    """Central configuration — all env vars in one place."""

    # Application
    APP_NAME: str = env_str("APP_NAME", "MCP Agent") or "MCP Agent"
    APP_VERSION: str = env_str("APP_VERSION", "1.0.0") or "1.0.0"
    ENV: str = env_str("ENV", "development") or "development"
    DEBUG: bool = env_bool("DEBUG", False)
    LOG_LEVEL: str = env_str("LOG_LEVEL", "INFO") or "INFO"

    # Server
    HOST: str = env_str("HOST", "0.0.0.0") or "0.0.0.0"
    PORT: int = env_int("PORT", 8080) or 8080
    RELOAD: bool = env_bool("RELOAD", False)
    WORKERS: int = env_int("WORKERS", 1) or 1

    # CORS
    CORS_ORIGINS: list[str] = env_list("CORS_ORIGINS") or ["*"]

    # MCP / Spring Boot backend
    # JAVA_MCP_BASE_URL is the primary env var (matches Spring Boot deploy convention);
    # MCP_BASE_URL is accepted as an alias.  The value should be the full MCP API root
    # including any context path, e.g. http://host:19170/mcp-service/mcp
    MCP_BASE_URL: str = (
        env_str("JAVA_MCP_BASE_URL")
        or env_str("MCP_BASE_URL", "http://localhost:19170/mcp")
        or "http://localhost:19170/mcp"
    )
    MCP_TIMEOUT_SECONDS: float = env_float("MCP_TIMEOUT_SECONDS", 30.0) or 30.0
    MCP_TOOLS_CACHE_TTL: int = env_int("MCP_TOOLS_CACHE_TTL", 600) or 600
    MCP_API_KEY: str | None = env_str("MCP_API_KEY")
    # Bearer token forwarded to MCP server on every request
    MCP_BEARER_TOKEN: str | None = env_str("MCP_BEARER_TOKEN")

    # LLM — OLLAMA (open-source, self-hosted)
    LLM_PROVIDER: str = env_str("LLM_PROVIDER", "ollama") or "ollama"
    LLM_MODEL: str = env_str("LLM_MODEL", "llama3.1") or "llama3.1"
    LLM_MAX_TOKENS: int = env_int("LLM_MAX_TOKENS", 4096) or 4096
    LLM_TEMPERATURE: float = env_float("LLM_TEMPERATURE", 0.0) or 0.0
    LLM_MAX_ITERATIONS: int = env_int("LLM_MAX_ITERATIONS", 10) or 10

    # OLLAMA
    OLLAMA_BASE_URL: str = env_str("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434"
    OLLAMA_MODEL: str = env_str("OLLAMA_MODEL", "llama3.1") or "llama3.1"

    # Redis (session storage)
    REDIS_URL: str = env_str("REDIS_URL", "redis://localhost:6379/0") or "redis://localhost:6379/0"
    REDIS_TIMEOUT: int = env_int("REDIS_TIMEOUT", 5) or 5
    SESSION_TTL_SECONDS: int = env_int("SESSION_TTL_SECONDS", 86400) or 86400  # 24 h

    # Auth / RBAC
    JWT_SECRET: str = env_str("JWT_SECRET", "change-me-in-production") or "change-me-in-production"
    JWT_ALGORITHM: str = env_str("JWT_ALGORITHM", "HS256") or "HS256"
    JWT_EXPIRE_MINUTES: int = env_int("JWT_EXPIRE_MINUTES", 60) or 60
    JWT_REFRESH_EXPIRE_MINUTES: int = env_int("JWT_REFRESH_EXPIRE_MINUTES", 10080) or 10080  # 7d

    # System prompt injected into every conversation
    AGENT_SYSTEM_PROMPT: str = (
        env_str("AGENT_SYSTEM_PROMPT")
        or (
            "You are an intelligent enterprise assistant with access to a set of tools. "
            "Use them to answer user queries accurately. "
            "Always confirm before executing HIGH or MEDIUM risk operations."
        )
    )

    @classmethod
    def to_dict(cls, mask_secrets: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attr in dir(cls):
            if attr.startswith("_") or callable(getattr(cls, attr)):
                continue
            value = getattr(cls, attr)
            result[attr] = _mask(attr, value) if mask_secrets else value
        return result

    @classmethod
    def is_production(cls) -> bool:
        return cls.ENV.lower() in {"production", "prod"}

    @classmethod
    def is_testing(cls) -> bool:
        return env_bool("TESTING", False)


cfg = AppConfig()
