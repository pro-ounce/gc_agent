"""
mcp/tool_index.py
─────────────────
Tool-RAG: index the MCP tool catalog in OpenSearch (kNN vector + BM25 text) and
retrieve the top-k relevant tools for a query, so the LLM only ever sees a small,
relevant subset instead of every tool. Reuses the estate's OpenSearch + the
nomic-embed-text embedding model.

Everything is FAIL-OPEN: any error (OpenSearch down, embed fails, kNN unsupported)
returns None/no-op so the caller falls back to sending all tools. Gated by
flags.tool_rag_enabled — a no-op when the flag is off.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ..commons.config import cfg
from ..commons.flags import flags
from ..commons.logger import get_logger
from ..services.embeddings import embed
from ..services import runtime_config

log = get_logger(__name__)


# Bump when _tool_text changes — folded into the digest so a deploy forces a re-embed
# instead of the persisted-digest skip serving stale vectors.
_EMBED_VERSION = "v2-humanized"


def _humanize(name: str) -> str:
    """getUserByUserName_get → 'get User By User Name get'. Splits camelCase and
    underscores so both BM25 and the embedding see real words ('user', 'name') instead
    of one opaque camelCase token — the reason a user-by-name lookup ranked out of range."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return s.replace("_", " ")


def _tool_text(tool: Any) -> str:
    params = ", ".join(p.name for p in getattr(tool, "parameters", []) or [])
    # Include BOTH the raw name (exact matches) and the humanized form (word matches).
    return f"{tool.name}. {_humanize(tool.name)}. {tool.description or ''} params: {params}".strip()


def _rrf(ranked_lists: list[list[str]], k: int, c: int = 60) -> list[str]:
    """Reciprocal-rank fusion: combine several ranked name lists into one.
    score(name) = Σ 1/(c + rank). The constant c=60 (standard) damps the top so a
    single list can't dominate. Returns the top-k fused names, order-preserving on ties.
    """
    scores: dict[str, float] = {}
    order: list[str] = []
    for names in ranked_lists:
        for rank, name in enumerate(names):
            if name not in scores:
                scores[name] = 0.0
                order.append(name)
            scores[name] += 1.0 / (c + rank)
    order.sort(key=lambda n: scores[n], reverse=True)
    return order[:k]


class ToolIndex:
    def __init__(self) -> None:
        self._client: Any = None
        self._tried = False
        self._indexed_hash: str | None = None

    # ── OpenSearch client (lazy, fail-open) ─────────────────────────────────────
    def _os(self) -> Any:
        if self._client is not None or self._tried:
            return self._client
        self._tried = True
        try:
            from opensearchpy import OpenSearch

            auth = (
                (cfg.OPENSEARCH_USERNAME, cfg.OPENSEARCH_PASSWORD or "")
                if cfg.OPENSEARCH_USERNAME
                else None
            )
            c = OpenSearch(
                hosts=[cfg.OPENSEARCH_URL],
                http_auth=auth,
                use_ssl=cfg.OPENSEARCH_URL.lower().startswith("https"),
                verify_certs=cfg.OPENSEARCH_VERIFY_CERTS,
                ssl_show_warn=False,
                timeout=cfg.OPENSEARCH_TIMEOUT,
                max_retries=0,
                retry_on_timeout=False,
            )
            if not c.ping():
                raise RuntimeError("ping False")
            self._client = c
            self._ensure_index(c)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"tool-RAG OpenSearch unavailable, falling back to all-tools: {exc}")
            self._client = None
        return self._client

    def _ensure_index(self, c: Any) -> None:
        if c.indices.exists(index=cfg.TOOL_RAG_INDEX):
            return
        c.indices.create(
            index=cfg.TOOL_RAG_INDEX,
            body={
                "settings": {"index": {"knn": True}},
                "mappings": {
                    "properties": {
                        "name": {"type": "keyword"},
                        "text": {"type": "text"},
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": cfg.EMBED_DIM,
                            "method": {
                                "name": "hnsw",
                                "space_type": "cosinesimil",
                                "engine": "lucene",
                            },
                        },
                    }
                },
            },
        )
        log.info(f"tool-RAG index created: {cfg.TOOL_RAG_INDEX}")

    # ── Indexing ────────────────────────────────────────────────────────────────
    async def reindex(self, tools: list[Any]) -> None:
        """Embed + upsert tools. Skips work when the tool-name set is unchanged.
        Stale docs (removed tools) are harmless — select_tools filters to live tools."""
        if not runtime_config.get_bool("TOOL_RAG_ENABLED") or not tools:
            return
        # Stable across processes (Python's hash() is per-process salted, so it could
        # never match a persisted value). Lets us skip the ~24s re-embed after a restart.
        digest = hashlib.sha256(
            (_EMBED_VERSION + "\n" + "\n".join(sorted(t.name for t in tools))).encode()
        ).hexdigest()
        if digest == self._indexed_hash:
            return
        c = self._os()
        if c is None:
            return
        # The index survives restarts in OpenSearch — only our in-memory hash resets.
        # If a persisted marker shows the same tool set is already embedded, skip the work.
        try:
            marker = c.get(index=cfg.TOOL_RAG_INDEX, id="__digest__", _source=["digest"])
            if marker.get("_source", {}).get("digest") == digest:
                cnt = c.count(index=cfg.TOOL_RAG_INDEX).get("count", 0) - 1  # exclude marker
                if cnt >= len(tools):
                    self._indexed_hash = digest
                    log.info(f"tool-RAG index already current ({len(tools)} tools) — skipping reindex")
                    return
        except Exception:  # noqa: BLE001 — marker absent / index new → fall through and embed
            pass
        try:
            for t in tools:
                vec = await embed(_tool_text(t))
                if not vec:
                    continue
                c.index(
                    index=cfg.TOOL_RAG_INDEX,
                    id=t.name,
                    body={
                        "name": t.name,
                        "text": _tool_text(t),
                        "params": [p.name for p in getattr(t, "parameters", []) or []],
                        "embedding": vec,
                    },
                )
            # Persist the digest so the next restart can skip re-embedding. Stored in the
            # same index without an embedding — kNN never returns it, and search filters it
            # out (no "name" field), so it's inert to retrieval.
            c.index(index=cfg.TOOL_RAG_INDEX, id="__digest__", body={"digest": digest}, refresh=True)
            self._indexed_hash = digest
            log.info(f"tool-RAG reindexed {len(tools)} tools")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"tool-RAG reindex failed (fail-open): {exc}")

    # ── Retrieval ───────────────────────────────────────────────────────────────
    def _hits(self, c: Any, body: dict) -> list[str]:
        resp = c.search(index=cfg.TOOL_RAG_INDEX, body=body)
        hits = resp.get("hits", {}).get("hits", [])
        return [h["_source"]["name"] for h in hits if h.get("_source", {}).get("name")]

    async def search(self, query: str, k: int) -> list[str] | None:
        """Return up to k relevant tool names, or None to signal 'no selection'
        (→ caller sends all tools).

        HYBRID: fuse semantic kNN with BM25 keyword ranking via reciprocal-rank
        fusion. Pure kNN drifts on queries like "account details of GCADMIN" —
        it surfaced fund/profile-group tools while the actual user tools (which
        keyword-match 'user') ranked out of range. BM25 catches the literal terms
        the embedding misses; kNN catches paraphrases BM25 misses. Over-fetch each,
        then fuse. Fail-open to None everywhere.
        """
        if not runtime_config.get_bool("TOOL_RAG_ENABLED"):
            return None
        c = self._os()
        if c is None:
            return None
        # Over-fetch each ranker so fusion has material to work with.
        pool = max(k * 2, 20)
        try:
            bm25 = self._hits(c, {
                "size": pool,
                "_source": ["name"],
                "query": {"multi_match": {"query": query, "fields": ["name^3", "text"]}},
            })
            knn: list[str] = []
            vec = await embed(query)
            if vec:
                knn = self._hits(c, {
                    "size": pool,
                    "_source": ["name"],
                    "query": {"knn": {"embedding": {"vector": vec, "k": pool}}},
                })
            fused = _rrf([knn, bm25], k)
            return fused or None
        except Exception as exc:  # noqa: BLE001
            log.warning(f"tool-RAG search failed (fail-open): {exc}")
            return None


tool_index = ToolIndex()
