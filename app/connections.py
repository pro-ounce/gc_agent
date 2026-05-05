"""
connections.py
──────────────
Initialises shared external clients:
  - Redis (session storage) with automatic in-memory fallback for tests
  - httpx AsyncClient for MCP Spring Boot backend

Call `close_connections()` in app lifespan teardown.
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis as _redis
import httpx

from app.commons.config import cfg
from app.commons.logger import get_logger

log = get_logger(__name__)


# ── In-memory Redis fallback (for testing / no-Redis environments) ─────────────

class _InMemoryRedis:
    """Minimal Redis-compatible shim backed by a plain dict."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float | None]] = {}

    def _is_expired(self, key: str) -> bool:
        _, exp = self._store.get(key, (None, None))
        return exp is not None and time.time() > exp

    def get(self, key: str) -> bytes | None:
        if key not in self._store or self._is_expired(key):
            return None
        value, _ = self._store[key]
        return value.encode() if isinstance(value, str) else value

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        expiry = time.time() + ex if ex else None
        self._store[key] = (value, expiry)

    def delete(self, *keys: str) -> int:
        removed = sum(1 for k in keys if self._store.pop(k, None) is not None)
        return removed

    def exists(self, key: str) -> int:
        return 0 if (key not in self._store or self._is_expired(key)) else 1

    def expire(self, key: str, seconds: int) -> bool:
        if key not in self._store:
            return False
        value, _ = self._store[key]
        self._store[key] = (value, time.time() + seconds)
        return True

    def keys(self, pattern: str = "*") -> list[bytes]:
        import fnmatch
        return [
            k.encode()
            for k in self._store
            if not self._is_expired(k) and fnmatch.fnmatch(k, pattern)
        ]

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


# ── Redis client ──────────────────────────────────────────────────────────────

def _make_redis() -> _redis.Redis | _InMemoryRedis:
    if cfg.is_testing() or not cfg.REDIS_URL:
        log.info("Using in-memory Redis (test/fallback mode)")
        return _InMemoryRedis()  # type: ignore[return-value]
    try:
        client = _redis.from_url(
            cfg.REDIS_URL,
            socket_connect_timeout=cfg.REDIS_TIMEOUT,
            decode_responses=False,
        )
        client.ping()
        log.bind(func="connections").info(f"Redis connected: {cfg.REDIS_URL}")
        return client
    except Exception as exc:
        log.warning(f"Redis unavailable ({exc}), falling back to in-memory store")
        return _InMemoryRedis()  # type: ignore[return-value]


redis_client: _redis.Redis | _InMemoryRedis = _make_redis()


# ── Typed Redis helpers ───────────────────────────────────────────────────────

def redis_set_json(key: str, data: Any, ex: int | None = None) -> None:
    redis_client.set(key, json.dumps(data), ex=ex)


def redis_get_json(key: str) -> Any | None:
    raw = redis_client.get(key)
    if raw is None:
        return None
    return json.loads(raw)


def redis_delete(key: str) -> None:
    redis_client.delete(key)


# ── httpx async client for MCP backend ───────────────────────────────────────

_mcp_http_client: httpx.AsyncClient | None = None


def get_mcp_http_client() -> httpx.AsyncClient:
    global _mcp_http_client
    if _mcp_http_client is None or _mcp_http_client.is_closed:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if cfg.MCP_API_KEY:
            headers["X-API-KEY"] = cfg.MCP_API_KEY
        _mcp_http_client = httpx.AsyncClient(
            base_url=cfg.MCP_BASE_URL,
            headers=headers,
            timeout=cfg.MCP_TIMEOUT_SECONDS,
        )
        log.bind(func="connections").info(f"MCP HTTP client created: {cfg.MCP_BASE_URL}")
    return _mcp_http_client


async def close_connections() -> None:
    """Call from app lifespan on shutdown."""
    global _mcp_http_client
    if _mcp_http_client and not _mcp_http_client.is_closed:
        await _mcp_http_client.aclose()
        log.info("MCP HTTP client closed")
    if hasattr(redis_client, "close"):
        redis_client.close()
        log.info("Redis client closed")
