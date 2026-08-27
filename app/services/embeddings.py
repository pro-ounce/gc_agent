"""
services/embeddings.py
──────────────────────
Thin embedding client over OLLAMA (nomic-embed-text by default), used by tool-RAG
to embed tool descriptions and user queries. Fail-open: any error returns None so
the caller falls back to sending all tools.
"""
from __future__ import annotations

import httpx

from ..commons.config import cfg
from ..commons.logger import get_logger

log = get_logger(__name__)

# Reused across calls; lazily created.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=cfg.OLLAMA_BASE_URL, timeout=15.0)
    return _client


async def embed(text: str) -> list[float] | None:
    """Return the embedding vector for `text`, or None on any failure."""
    if not text:
        return None
    try:
        resp = await _http().post(
            # keep_alive=-1 keeps the small embed model resident too, so per-query tool-RAG
            # retrieval never pays a reload after idle.
            "/api/embeddings",
            json={"model": cfg.EMBED_MODEL, "prompt": text, "keep_alive": -1},
        )
        resp.raise_for_status()
        vec = resp.json().get("embedding")
        return vec if isinstance(vec, list) and vec else None
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.warning(f"embed failed (model={cfg.EMBED_MODEL}): {exc}")
        return None
