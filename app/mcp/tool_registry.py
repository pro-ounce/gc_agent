"""
mcp/tool_registry.py
────────────────────
In-memory TTL cache for MCP tools, with Anthropic tool-use format conversion.

Usage:
    registry = ToolRegistry()
    tools = await registry.get_tools()           # list[Tool]
    schema = await registry.as_anthropic_tools() # Anthropic SDK format
    result = await registry.execute(name, args)  # ToolResult
"""
from __future__ import annotations

import time
from typing import Any

from app.commons.config import cfg
from app.commons.flags import flags
from app.commons.logger import get_logger
from app.mcp.client import MCPClientError, mcp_client
from app.models.mcp import Tool, ToolResult

log = get_logger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._cache: list[Tool] = []
        self._loaded_at: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_tools(self, force_refresh: bool = False) -> list[Tool]:
        if force_refresh or self._is_stale():
            await self._refresh()
        return self._cache

    async def get_tool(self, name: str) -> Tool | None:
        tools = await self.get_tools()
        return next((t for t in tools if t.name == name), None)

    async def as_anthropic_tools(self) -> list[dict[str, Any]]:
        """Convert tool list to Anthropic tool_use schema."""
        tools = await self.get_tools()
        return [_to_anthropic_schema(t) for t in tools]

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_headers: dict[str, str] | None = None,
    ) -> ToolResult:
        import time as _time
        start = _time.perf_counter()
        try:
            raw = await mcp_client.execute_tool(tool_name, arguments, request_headers)
            duration_ms = round((_time.perf_counter() - start) * 1000, 1)

            # Normalise output — Spring Boot may return {"result": ...} or {"content": [...]}
            output = (
                raw.get("result")
                or raw.get("content")
                or raw.get("output")
                or raw
            )
            # If content is a list of {type, text} blocks, join text parts
            if isinstance(output, list):
                texts = [
                    block.get("text", "")
                    for block in output
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                output = "\n".join(texts) if texts else str(output)

            return ToolResult(
                tool_name=tool_name,
                success=True,
                output=output,
                duration_ms=duration_ms,
            )
        except MCPClientError as exc:
            duration_ms = round((_time.perf_counter() - start) * 1000, 1)
            log.bind(func="execute_tool", tool=tool_name).error(
                f"Tool execution failed: {exc}"
            )
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
            )

    def invalidate(self) -> None:
        self._loaded_at = 0.0
        self._cache = []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _is_stale(self) -> bool:
        if not flags.tool_caching_enabled:
            return True
        return (time.time() - self._loaded_at) > cfg.MCP_TOOLS_CACHE_TTL

    async def _refresh(self) -> None:
        try:
            raw_tools = await mcp_client.list_tools()
            self._cache = [Tool.from_mcp_payload(t) for t in raw_tools]
            self._loaded_at = time.time()
            log.bind(func="tool_registry").info(
                f"Tool registry refreshed: {len(self._cache)} tools loaded"
            )
        except MCPClientError as exc:
            log.error(f"Failed to refresh tool registry: {exc}")
            if not self._cache:
                self._cache = []


def _to_anthropic_schema(tool: Tool) -> dict[str, Any]:
    """Convert Tool → Anthropic tool_use dict."""
    schema: dict[str, Any] = tool.input_schema or {}
    if not schema:
        # Build minimal schema from parsed parameters
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in tool.parameters:
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            props[p.name] = prop
            if p.required:
                required.append(p.name)
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required

    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": schema,
    }


# Module-level singleton
tool_registry = ToolRegistry()
