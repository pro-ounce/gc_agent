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

    # list ops (used by the capped audit log)
    def lpush(self, key: str, value: Any) -> int:
        arr, _ = self._store.get(key, ([], None))
        if not isinstance(arr, list):
            arr = []
        arr.insert(0, value)
        self._store[key] = (arr, None)
        return len(arr)

    def ltrim(self, key: str, start: int, stop: int) -> bool:
        arr, exp = self._store.get(key, ([], None))
        if isinstance(arr, list):
            end = None if stop == -1 else stop + 1
            self._store[key] = (arr[start:end], exp)
        return True

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


# ── OpenSearch store (Redis-compatible KV/list backend) ────────────────────────

class _OpenSearchStore:
    """
    Redis-compatible KV+list store backed by OpenSearch.

    Each key is a document (_id = key) in one index:
        { "value": <str>, "expires_at": <epoch int|None>, "kind": "kv"|"list" }

    GET-by-id is realtime (works immediately after a write); keys()/list uses search,
    which is near-real-time (~1s refresh) — fine for the agent. TTL is enforced lazily
    (expired docs are skipped and deleted on access); add an ISM policy for hard cleanup.
    """

    def __init__(self, client: Any, index: str) -> None:
        self._c = client
        self._index = index
        self._ensure_index()

    def _ensure_index(self) -> None:
        try:
            if not self._c.indices.exists(index=self._index):
                self._c.indices.create(index=self._index, body={
                    "mappings": {"properties": {
                        "value": {"type": "text", "index": False},
                        "expires_at": {"type": "long"},
                        "kind": {"type": "keyword"},
                    }}
                })
        except Exception as exc:  # racing create / perms — log and continue
            log.warning(f"OpenSearch ensure-index '{self._index}' failed: {exc}")

    @staticmethod
    def _now() -> float:
        return time.time()

    def _get_src(self, key: str) -> dict | None:
        try:
            resp = self._c.get(index=self._index, id=key)  # realtime
        except Exception:
            return None
        src = resp.get("_source") if isinstance(resp, dict) else None
        if not src:
            return None
        exp = src.get("expires_at")
        if exp is not None and self._now() > exp:
            self.delete(key)
            return None
        return src

    def get(self, key: str) -> bytes | None:
        src = self._get_src(key)
        if src is None:
            return None
        val = src.get("value")
        if val is None:
            return None
        return val.encode() if isinstance(val, str) else val

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        body = {
            "value": value if isinstance(value, str) else str(value),
            "expires_at": int(self._now() + ex) if ex else None,
            "kind": "kv",
        }
        self._c.index(index=self._index, id=key, body=body)

    def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            try:
                self._c.delete(index=self._index, id=k)
                removed += 1
            except Exception:
                pass
        return removed

    def exists(self, key: str) -> int:
        return 1 if self._get_src(key) is not None else 0

    def expire(self, key: str, seconds: int) -> bool:
        try:
            self._c.update(index=self._index, id=key,
                           body={"doc": {"expires_at": int(self._now() + seconds)}})
            return True
        except Exception:
            return False

    def keys(self, pattern: str = "*") -> list[bytes]:
        import fnmatch
        try:
            resp = self._c.search(index=self._index, body={
                "query": {"match_all": {}}, "_source": ["expires_at"], "size": 10000,
            })
        except Exception:
            return []
        now = self._now()
        out: list[bytes] = []
        for hit in resp.get("hits", {}).get("hits", []):
            kid = hit.get("_id", "")
            exp = (hit.get("_source") or {}).get("expires_at")
            if exp is not None and now > exp:
                continue
            if fnmatch.fnmatch(kid, pattern):
                out.append(kid.encode())
        return out

    # ── list ops (audit log) — stored as a JSON array under the key ────────────
    def _get_list(self, key: str) -> list:
        src = self._get_src(key)
        if not src:
            return []
        try:
            arr = json.loads(src.get("value") or "[]")
            return arr if isinstance(arr, list) else []
        except Exception:
            return []

    def lpush(self, key: str, value: Any) -> int:
        arr = self._get_list(key)
        arr.insert(0, value)
        self._c.index(index=self._index, id=key,
                      body={"value": json.dumps(arr), "expires_at": None, "kind": "list"})
        return len(arr)

    def ltrim(self, key: str, start: int, stop: int) -> bool:
        arr = self._get_list(key)
        end = None if stop == -1 else stop + 1
        arr = arr[start:end]
        self._c.index(index=self._index, id=key,
                      body={"value": json.dumps(arr), "expires_at": None, "kind": "list"})
        return True

    def ping(self) -> bool:
        try:
            return bool(self._c.ping())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass


# ── Store selection (opensearch | redis | memory) ──────────────────────────────

def _make_store():
    if cfg.is_testing():
        log.info("Using in-memory store (test mode)")
        return _InMemoryRedis()

    backend = (cfg.STORE_BACKEND or "opensearch").lower()

    if backend == "memory":
        log.info("Using in-memory store (STORE_BACKEND=memory)")
        return _InMemoryRedis()

    if backend == "redis":
        try:
            client = _redis.from_url(cfg.REDIS_URL, socket_connect_timeout=cfg.REDIS_TIMEOUT,
                                     decode_responses=False)
            client.ping()
            log.bind(func="connections").info(f"Redis connected: {cfg.REDIS_URL}")
            return client
        except Exception as exc:
            log.warning(f"Redis unavailable ({exc}), falling back to in-memory store")
            return _InMemoryRedis()

    # default: OpenSearch
    try:
        from opensearchpy import OpenSearch
        auth = None
        if cfg.OPENSEARCH_USERNAME:
            auth = (cfg.OPENSEARCH_USERNAME, cfg.OPENSEARCH_PASSWORD or "")
        client = OpenSearch(
            hosts=[cfg.OPENSEARCH_URL],
            http_auth=auth,
            use_ssl=cfg.OPENSEARCH_URL.lower().startswith("https"),
            verify_certs=cfg.OPENSEARCH_VERIFY_CERTS,
            ssl_show_warn=False,
            timeout=cfg.OPENSEARCH_TIMEOUT,
            max_retries=0,          # fail fast if the cluster is unreachable
            retry_on_timeout=False,
        )
        import logging as _logging
        _logging.getLogger("opensearch").setLevel(_logging.ERROR)  # quiet retry tracebacks
        if not client.ping():
            raise RuntimeError("ping returned False")
        store = _OpenSearchStore(client, cfg.OPENSEARCH_INDEX)
        log.bind(func="connections").info(
            f"OpenSearch store connected: {cfg.OPENSEARCH_URL} index={cfg.OPENSEARCH_INDEX}"
        )
        return store
    except Exception as exc:
        log.warning(f"OpenSearch unavailable ({exc}), falling back to in-memory store")
        return _InMemoryRedis()


# `redis_client` name kept for call-site compatibility — it is the selected store.
redis_client = _make_store()


def store_backend_name() -> str:
    cls = type(redis_client).__name__
    return {
        "_OpenSearchStore": "opensearch",
        "_InMemoryRedis": "memory",
        "Redis": "redis",
    }.get(cls, cls)


def store_health() -> dict[str, Any]:
    """Backend name + reachability, for health/readiness probes."""
    try:
        up = bool(redis_client.ping())
    except Exception as exc:  # noqa: BLE001
        return {"backend": store_backend_name(), "status": "DOWN", "error": str(exc)}
    return {"backend": store_backend_name(), "status": "UP" if up else "DOWN"}


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
        # No base_url — client.py builds full URLs from cfg.MCP_BASE_URL so that
        # the path prefix (e.g. /mcp-service/mcp) is preserved correctly regardless
        # of whether the URL has a trailing slash.
        _mcp_http_client = httpx.AsyncClient(
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
