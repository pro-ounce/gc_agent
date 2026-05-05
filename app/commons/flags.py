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

    # Storage
    redis_enabled: bool = field(default_factory=lambda: env_bool("REDIS_ENABLED", True))

    # Observability
    request_logging_enabled: bool = field(
        default_factory=lambda: env_bool("REQUEST_LOGGING_ENABLED", True)
    )
    audit_logging_enabled: bool = field(
        default_factory=lambda: env_bool("AUDIT_LOGGING_ENABLED", True)
    )

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
