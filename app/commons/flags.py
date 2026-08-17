"""
commons/flags.py
────────────────
Feature flags — toggle subsystems without code changes.
All flags read from environment so they can be overridden in CI/test/prod.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from app.commons.config import env_bool


@dataclass
class FeatureFlags:
    # Auth
    auth_enabled: bool = field(default_factory=lambda: env_bool("AUTH_ENABLED", True))
    rbac_enabled: bool = field(default_factory=lambda: env_bool("RBAC_ENABLED", True))

    # MCP features
    tool_risk_confirmation: bool = field(
        default_factory=lambda: env_bool("TOOL_RISK_CONFIRMATION", True)
    )
    tool_caching_enabled: bool = field(
        default_factory=lambda: env_bool("TOOL_CACHING_ENABLED", True)
    )
    # Tool-RAG: retrieve only the top-k relevant MCP tools per query (vs sending all).
    # Default OFF — only worth enabling once the tool catalog is large. Fail-open.
    tool_rag_enabled: bool = field(default_factory=lambda: env_bool("TOOL_RAG_ENABLED", False))
    # Read-only chatbot: never OFFER mutating tools (create/update/delete/activate/…) to the
    # model. Default ON — a Q&A chatbot must not be able to change state from a casual query
    # (a "look up user" prompt once fired activateUserByUsername_put). Turn OFF only once
    # write actions are gated behind explicit confirmation (see tool_risk_confirmation, which
    # the STREAMING path does not yet enforce).
    chatbot_read_only: bool = field(
        default_factory=lambda: env_bool("CHATBOT_READ_ONLY", True)
    )
    # Strict grounding: when ON, the agent reports ONLY facts the tools returned and adds
    # no interpretation/embellishment (e.g. no "locked due to failed logins" notes). Default
    # OFF — light interpretation is useful in some cases; flip on when accuracy must be exact.
    strict_grounding: bool = field(
        default_factory=lambda: env_bool("AGENT_STRICT_GROUNDING", False)
    )

    # Storage
    redis_enabled: bool = field(default_factory=lambda: env_bool("REDIS_ENABLED", True))

    # Observability
    request_logging_enabled: bool = field(
        default_factory=lambda: env_bool("REQUEST_LOGGING_ENABLED", True)
    )
    audit_logging_enabled: bool = field(
        default_factory=lambda: env_bool("AUDIT_LOGGING_ENABLED", True)
    )
    metrics_enabled: bool = field(default_factory=lambda: env_bool("METRICS_ENABLED", True))

    # API behaviour
    streaming_enabled: bool = field(default_factory=lambda: env_bool("STREAMING_ENABLED", True))
    rate_limit_enabled: bool = field(default_factory=lambda: env_bool("RATE_LIMIT_ENABLED", False))

    # Security headers
    csp_enabled: bool = field(default_factory=lambda: env_bool("CSP_ENABLED", True))

    # Dev helpers
    debug_tools_enabled: bool = field(
        default_factory=lambda: env_bool("DEBUG_TOOLS_ENABLED", False)
    )


flags = FeatureFlags()
