"""
mcp/client.py
─────────────
Async HTTP client that speaks to the Spring Boot MCP server.

Expected Spring Boot MCP REST API:
  GET  /mcp/tools                    → list all tools
  POST /mcp/tools/{name}/execute     → execute a tool
  GET  /mcp/prompts                  → list all prompts
  GET  /mcp/prompts/{name}           → get prompt definition
  POST /mcp/prompts/{name}/execute   → render a prompt with arguments
  GET  /mcp/health                   → health probe

All endpoints may be prefixed by MCP_BASE_PATH (default "").
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.commons.config import cfg
from app.commons.logger import get_logger
from app.connections import get_mcp_http_client

log = get_logger(__name__)

_BASE_PATH = ""  # Override if Spring Boot context-path is set


class MCPClientError(Exception):
    """Raised when the MCP server returns an error."""
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MCPClient:
    """Thin async wrapper around the Spring Boot MCP REST API."""

    @property
    def _http(self) -> httpx.AsyncClient:
        return get_mcp_http_client()

    # ── Tool endpoints ────────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return raw tool definitions from the MCP server."""
        resp = await self._get("/mcp/tools")
        data = resp.json()
        # Support both {"tools": [...]} and plain [...]
        if isinstance(data, list):
            return data
        return data.get("tools", [])

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a named tool and return the raw result payload."""
        start = time.perf_counter()
        log.bind(func="execute_tool", tool=tool_name).info(
            f"Executing tool '{tool_name}' with args={list(arguments)}"
        )
        resp = await self._post(
            f"/mcp/tools/{tool_name}/execute",
            body={"arguments": arguments},
            extra_headers=request_headers,
        )
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        log.bind(func="execute_tool", tool=tool_name, duration_ms=duration_ms).info(
            f"Tool '{tool_name}' completed in {duration_ms}ms"
        )
        return resp.json()

    # ── Prompt endpoints ──────────────────────────────────────────────────────

    async def list_prompts(self) -> list[dict[str, Any]]:
        resp = await self._get("/mcp/prompts")
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("prompts", [])

    async def get_prompt(self, name: str) -> dict[str, Any]:
        resp = await self._get(f"/mcp/prompts/{name}")
        return resp.json()

    async def execute_prompt(
        self, name: str, arguments: dict[str, str]
    ) -> str:
        """Render a prompt server-side and return the resulting text."""
        resp = await self._post(
            f"/mcp/prompts/{name}/execute",
            body={"arguments": arguments},
        )
        data = resp.json()
        # Support {"text": "..."} or {"content": "..."}
        return data.get("text") or data.get("content") or str(data)

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        try:
            resp = await self._get("/mcp/health")
            return resp.json()
        except Exception as exc:
            return {"status": "DOWN", "error": str(exc)}

    # ── Private HTTP helpers ──────────────────────────────────────────────────

    async def _get(self, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = await self._http.get(_BASE_PATH + path, **kw)
            self._raise_for_status(resp)
            return resp
        except httpx.TimeoutException as exc:
            raise MCPClientError(f"MCP server timeout on GET {path}: {exc}")
        except httpx.RequestError as exc:
            raise MCPClientError(f"MCP server connection error on GET {path}: {exc}")

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if extra_headers:
            # Only pass through safe headers (authorization, etc.)
            for h in ("Authorization", "X-API-KEY", "X-Forwarded-For"):
                if h in extra_headers:
                    headers[h] = extra_headers[h]
        try:
            resp = await self._http.post(
                _BASE_PATH + path,
                json=body or {},
                headers=headers,
            )
            self._raise_for_status(resp)
            return resp
        except httpx.TimeoutException as exc:
            raise MCPClientError(f"MCP server timeout on POST {path}: {exc}")
        except httpx.RequestError as exc:
            raise MCPClientError(f"MCP server connection error on POST {path}: {exc}")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or resp.text
            except Exception:
                detail = resp.text
            raise MCPClientError(
                f"MCP server error {resp.status_code}: {detail}",
                status_code=resp.status_code,
            )


# Module-level singleton
mcp_client = MCPClient()
