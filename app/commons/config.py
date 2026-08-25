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


# ── Jasypt secrets ─────────────────────────────────────────────────────────────
# Secrets may be provided as Jasypt ENC(...) values (matching the platform's
# jasypt-spring-boot config), so the cleartext key is never stored at rest — only
# the ENC(...) value + the one master JASYPT_ENCRYPTOR_PASSWORD. `_secret()` reads
# an env var and transparently decrypts it when wrapped in ENC(...).
from ..commons.jasypt import resolve as _jasypt_resolve  # noqa: E402

_JASYPT_PASSWORD: str | None = env_str("JASYPT_ENCRYPTOR_PASSWORD")
_JASYPT_ITERATIONS: int = env_int("JASYPT_KEY_ITERATIONS", 1000) or 1000


def _secret(key: str) -> str | None:
    """Read an env var; decrypt transparently if it is a Jasypt ENC(...) value."""
    return _jasypt_resolve(env_str(key), _JASYPT_PASSWORD, _JASYPT_ITERATIONS)


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
    # Ollama's default num_ctx is 4096. Our system prompt + MCP tool schemas alone run ~4k
    # tokens, which fills the window and makes Ollama "context shift" — silently discarding
    # the middle of the prompt (tools included) and truncating the reply. Must exceed
    # prompt + num_predict. Lower it only if the box is memory-starved (bigger ctx = bigger KV cache).
    LLM_NUM_CTX: int = env_int("LLM_NUM_CTX", 8192) or 8192
    LLM_MAX_ITERATIONS: int = env_int("LLM_MAX_ITERATIONS", 10) or 10
    # Cap on conversation history sent to the LLM (approx chars; ~4 chars/token). Older turns
    # beyond this are dropped so a long-lived session can't bloat the prompt and slow every
    # turn. The window always starts at a user turn so no tool_call/tool_result pair is
    # orphaned. 0 = unlimited. 12000 chars ≈ 3000 tokens of recent context.
    MAX_HISTORY_CHARS: int = env_int("MAX_HISTORY_CHARS", 12000) or 12000
    # HTTP timeout for LLM calls. CPU inference of a large prompt can take minutes, so
    # this must exceed the slowest expected turn (raise it, or move Ollama to a GPU).
    LLM_TIMEOUT_SECONDS: float = env_float("LLM_TIMEOUT_SECONDS", 300.0) or 300.0

    # OLLAMA
    OLLAMA_BASE_URL: str = env_str("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434"
    OLLAMA_MODEL: str = env_str("OLLAMA_MODEL", "llama3.1") or "llama3.1"

    # Tool-RAG (dynamic tool selection). Gated by flags.tool_rag_enabled.
    EMBED_MODEL: str = env_str("EMBED_MODEL", "nomic-embed-text") or "nomic-embed-text"
    EMBED_DIM: int = env_int("EMBED_DIM", 768) or 768               # nomic-embed-text = 768
    # Tools offered to the LLM per query. 5 is far too tight against a 710-tool catalog —
    # the relevant tool often ranks outside the top-5 (e.g. a user-by-name lookup loses to
    # profile-/fund-group tools on "account details"). 15 stays tiny in the prompt
    # (~1.1k tokens) while giving the right tool room to surface. Tune via env.
    TOOL_RAG_TOP_K: int = env_int("TOOL_RAG_TOP_K", 15) or 15
    TOOL_RAG_MIN_TOOLS: int = env_int("TOOL_RAG_MIN_TOOLS", 8) or 8  # below this, send all (no RAG)
    TOOL_RAG_INDEX: str = env_str("TOOL_RAG_INDEX", "agent-tools") or "agent-tools"
    # Resolver tools always offered alongside the RAG hits — small utility lookups the model
    # needs to turn a name/code into an id (getAllApplications_get → applicationId). Without
    # pinning, a "roles for app X" query may not surface the app lookup, so the chain breaks.
    TOOL_RAG_PINNED: str = env_str("TOOL_RAG_PINNED", "getUserAppsByUserId_get") or ""
    # Structured-response mode: when read-only tool results already render as UIBlocks,
    # skip the final LLM synthesis call and emit a one-line lead-in + the blocks. Kills the
    # stream-prose-then-table double render and the generation latency for data lookups.
    SKIP_SYNTHESIS_WITH_BLOCKS: bool = env_bool("SKIP_SYNTHESIS_WITH_BLOCKS", True)
    # Personal-assistant default: a user-scoped tool (has a userId param) called with no user
    # identifier is scoped to the caller — so "my apps/roles" means mine, not everyone's.
    DEFAULT_USER_SCOPE: bool = env_bool("DEFAULT_USER_SCOPE", True)

    # Session / KV store backend: "opensearch" (default) | "redis" | "memory".
    # We run on OpenSearch (already in the estate); Redis can be added later by flipping this.
    STORE_BACKEND: str = env_str("STORE_BACKEND", "opensearch") or "opensearch"
    SESSION_TTL_SECONDS: int = env_int("SESSION_TTL_SECONDS", 86400) or 86400  # 24 h

    # OpenSearch (KV/session store)
    OPENSEARCH_URL: str = env_str("OPENSEARCH_URL", "https://localhost:9200") or "https://localhost:9200"
    OPENSEARCH_USERNAME: str | None = env_str("OPENSEARCH_USERNAME")
    OPENSEARCH_PASSWORD: str | None = _secret("OPENSEARCH_PASSWORD")
    OPENSEARCH_VERIFY_CERTS: bool = env_bool("OPENSEARCH_VERIFY_CERTS", True)
    OPENSEARCH_INDEX: str = env_str("OPENSEARCH_INDEX", "agent-kv") or "agent-kv"
    # Manual/scheduled snapshot target — the registered repo + which indices to snapshot.
    # The scheduled SM policy uses the same repo; the /admin "Backups" button and the deploy
    # script take an on-demand snapshot into it (e.g. before a major push).
    OPENSEARCH_SNAPSHOT_REPO: str = env_str("OPENSEARCH_SNAPSHOT_REPO", "agent-s3") or "agent-s3"
    OPENSEARCH_SNAPSHOT_INDICES: str = (
        env_str("OPENSEARCH_SNAPSHOT_INDICES", "agent-kv,agent-tools,admin_configs")
        or "agent-kv,agent-tools,admin_configs"
    )
    # Snapshot Management (SM) policy the /admin Backups tab views/edits for the schedule.
    OPENSEARCH_SM_POLICY: str = env_str("OPENSEARCH_SM_POLICY", "agent-daily") or "agent-daily"
    OPENSEARCH_SM_TIMEZONE: str = env_str("OPENSEARCH_SM_TIMEZONE", "America/New_York") or "America/New_York"
    # Health flags the backup as DEGRADED if the newest SUCCESS snapshot is older than this
    # (hours). 0 disables the check. Surfaces a silently-broken backup in monitoring.
    OPENSEARCH_SNAPSHOT_MAX_AGE_HOURS: int = env_int("OPENSEARCH_SNAPSHOT_MAX_AGE_HOURS", 48) or 48
    OPENSEARCH_TIMEOUT: int = env_int("OPENSEARCH_TIMEOUT", 10) or 10

    # Redis (only used when STORE_BACKEND=redis)
    REDIS_URL: str = env_str("REDIS_URL", "redis://localhost:6379/0") or "redis://localhost:6379/0"
    REDIS_TIMEOUT: int = env_int("REDIS_TIMEOUT", 5) or 5

    # Auth / RBAC (standalone mode — the agent's own login)
    JWT_SECRET: str = env_str("JWT_SECRET", "change-me-in-production") or "change-me-in-production"
    JWT_ALGORITHM: str = env_str("JWT_ALGORITHM", "HS256") or "HS256"
    JWT_EXPIRE_MINUTES: int = env_int("JWT_EXPIRE_MINUTES", 60) or 60
    JWT_REFRESH_EXPIRE_MINUTES: int = env_int("JWT_REFRESH_EXPIRE_MINUTES", 10080) or 10080  # 7d

    # ── Platform integration (gc gateway-vouched auth) ──────────────────────────
    # PLATFORM_AUTH_MODE:
    #   "standalone" (default) — use the agent's own HS256 login / API keys (dev / off-platform).
    #   "gateway"              — trust the gc gateway: verify the forwarded internal token
    #                            (X-INT-TKN, HS512, signed with the gc DB key). Deployment-agnostic:
    #                            the signed short-lived token proves the request came from the gateway,
    #                            so it is safe even when the agent runs on a different server.
    PLATFORM_AUTH_MODE: str = env_str("PLATFORM_AUTH_MODE", "standalone") or "standalone"
    # Shared gc HMAC signing key — verifies X-INT-TKN. Accepts a Jasypt ENC(...) value
    # (the same encrypted JWT_INTERNAL_SECRET the Java services use) — decrypted in-memory
    # at startup via JASYPT_ENCRYPTOR_PASSWORD; the cleartext key is never stored on disk.
    GC_JWT_SECRET: str | None = _secret("GC_JWT_SECRET")
    GC_JWT_ALGORITHM: str = env_str("GC_JWT_ALGORITHM", "HS512") or "HS512"
    # Platform USER JWT signing key (the DB JWT_SECRET, category G / code JWT). The gateway
    # forwards the user's JWT (iss=GC360) in Authorization; this key verifies it. Separate
    # from the internal key above. Accepts ENC(...) (same jasypt password).
    GC_USER_JWT_SECRET: str | None = _secret("GC_USER_JWT_SECRET")
    GC_USER_JWT_ISSUER: str = env_str("GC_USER_JWT_ISSUER", "GC360") or "GC360"
    # MCP's gc-jwt-filter validates a user/access token by audience (aud) + a token-type claim
    # (X_TP == ACCESS). The discovery token must carry both or MCP 401s ("invalid audience" /
    # "token type is not ACCESS"). Values match the platform's GC360 access token.
    GC_USER_JWT_AUDIENCE: str = env_str("GC_USER_JWT_AUDIENCE", "GC360_API") or "GC360_API"
    GC_TOKEN_TYPE_CLAIM: str = env_str("GC_TOKEN_TYPE_CLAIM", "X_TP") or "X_TP"
    GC_ACCESS_TOKEN_TYPE: str = env_str("GC_ACCESS_TOKEN_TYPE", "ACCESS") or "ACCESS"
    # The agent signs its OWN short-lived token (same iss=GC360 user-JWT shape MCP accepts
    # from the gateway) for tokenless MCP calls — startup/health tool discovery, which carry
    # no caller token. Real chats forward the caller's token instead. Signed with
    # GC_USER_JWT_SECRET. Disable to fall back to MCP_BEARER_TOKEN / no credential.
    GC_MINT_DISCOVERY_TOKEN: bool = env_bool("GC_MINT_DISCOVERY_TOKEN", True)
    GC_SERVICE_USER_ID: str = env_str("GC_SERVICE_USER_ID", "1") or "1"
    # Mint as a REAL platform user so MCP accepts the discovery token (a synthetic uname can
    # be rejected if MCP resolves the user). userId 1 = GCADMIN. Used only for the agent's
    # own tokenless discovery/health calls — never for tool execution (chats forward the caller).
    GC_SERVICE_USERNAME: str = env_str("GC_SERVICE_USERNAME", "GCADMIN") or "GCADMIN"
    GC_INTERNAL_ISSUER: str = env_str("GC_INTERNAL_ISSUER", "GC_INTERNAL") or "GC_INTERNAL"
    GC_INTERNAL_AUDIENCE: str = env_str("GC_INTERNAL_AUDIENCE", "GC_INTERNAL_API") or "GC_INTERNAL_API"
    # Verify issuer/audience on the internal token? Default OFF — the gateway signs with the
    # shared secret (the real trust anchor) but does not consistently set aud, so requiring it
    # rejects valid tokens. Enable once the gateway's claims are confirmed.
    GC_VERIFY_ISSUER: bool = env_bool("GC_VERIFY_ISSUER", False)
    GC_VERIFY_AUDIENCE: bool = env_bool("GC_VERIFY_AUDIENCE", False)
    GC_INTERNAL_HEADER: str = env_str("GC_INTERNAL_HEADER", "X-INT-TKN") or "X-INT-TKN"
    GC_ROLE_HEADER: str = env_str("GC_ROLE_HEADER", "X-AR-KEY") or "X-AR-KEY"
    GC_JWT_LEEWAY: int = env_int("GC_JWT_LEEWAY", 30) or 30

    # Path prefix when served behind the gateway rewrite (e.g. /ai-agent-service). Empty = root.
    ROOT_PATH: str = env_str("ROOT_PATH", "") or ""

    # ── Actuator / observability (mirrors the gc Spring services) ───────────────
    # Endpoints served under this base path: <base>/health, /health/liveness, /health/readiness,
    # /info, /prometheus  — same shape as spring-boot-actuator across the fleet.
    MANAGEMENT_BASE_PATH: str = env_str("MANAGEMENT_BASE_PATH", "/actuator") or "/actuator"
    # Optional loopback/allow-list for actuator (matches app.security.actuator.allowed-ips).
    # Empty = allow all (rely on gateway/firewall). Set e.g. "127.0.0.1,::1" for loopback-only.
    ACTUATOR_ALLOWED_IPS: list[str] = env_list("ACTUATOR_ALLOWED_IPS")

    # System prompt injected into every conversation
    AGENT_SYSTEM_PROMPT: str = (
        env_str("AGENT_SYSTEM_PROMPT")
        or (
            "You are an intelligent enterprise assistant with access to a set of tools. "
            "Use them to answer user queries accurately. "
            "Always confirm before executing HIGH or MEDIUM risk operations. "
            "Answer concisely and directly: no preamble, no restating the question, no filler. "
            "Return only the facts the user asked for, as a short sentence or compact list. "
            # ID resolution is handled by the system: the model may pass an application code or
            # name directly and it is resolved to the numeric id before the call.
            "When a query is about a named application (e.g. 'FORMULATION'), call the "
            "application-scoped tool directly and pass the application code or name as "
            "applicationId — the system resolves it to the numeric id automatically. "
            # Self-scope: 'my …' means the current user.
            "When the user asks about their OWN data ('my applications', 'do I have access'), "
            "prefer a tool that takes a userId (e.g. getUserAppsByUserId) over an all-users "
            "tool; leave userId blank — the system fills in the current user. Only fall back "
            "to an all-users tool if no user-scoped option is offered."
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
