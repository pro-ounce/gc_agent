"""
mcp/prompt_registry.py
──────────────────────
Registry for MCP prompts defined on the Spring Boot server.
Prompts can be listed, inspected, and executed (rendered with arguments).
"""
from __future__ import annotations

import time
from typing import Any

from ..commons.config import cfg
from ..commons.logger import get_logger
from ..mcp.client import MCPClientError, mcp_client
from ..models.mcp import Prompt, PromptArgument

log = get_logger(__name__)

_CACHE_TTL = cfg.MCP_TOOLS_CACHE_TTL  # Reuse the same TTL setting


class PromptRegistry:
    def __init__(self) -> None:
        self._cache: list[Prompt] = []
        self._loaded_at: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_prompts(self, force_refresh: bool = False) -> list[Prompt]:
        if force_refresh or self._is_stale():
            await self._refresh()
        return self._cache

    async def get_prompt(self, name: str) -> Prompt | None:
        prompts = await self.get_prompts()
        return next((p for p in prompts if p.name == name), None)

    async def execute_prompt(self, name: str, arguments: dict[str, str]) -> str:
        """Render a named prompt with given arguments; returns the text."""
        try:
            return await mcp_client.execute_prompt(name, arguments)
        except MCPClientError as exc:
            log.error(f"Failed to execute prompt '{name}': {exc}")
            raise

    def invalidate(self) -> None:
        self._loaded_at = 0.0
        self._cache = []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _is_stale(self) -> bool:
        return (time.time() - self._loaded_at) > _CACHE_TTL

    async def _refresh(self) -> None:
        try:
            raw_prompts = await mcp_client.list_prompts()
            self._cache = [_parse_prompt(p) for p in raw_prompts]
            self._loaded_at = time.time()
            log.bind(func="prompt_registry").info(
                f"Prompt registry refreshed: {len(self._cache)} prompts loaded"
            )
        except MCPClientError as exc:
            log.error(f"Failed to refresh prompt registry: {exc}")
            if not self._cache:
                self._cache = []


def _parse_prompt(payload: dict[str, Any]) -> Prompt:
    args_raw = payload.get("arguments", [])
    args = [
        PromptArgument(
            name=a.get("name", ""),
            description=a.get("description", ""),
            required=a.get("required", False),
        )
        for a in args_raw
    ]
    return Prompt(
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        arguments=args,
        raw=payload,
    )


# Module-level singleton
prompt_registry = PromptRegistry()
