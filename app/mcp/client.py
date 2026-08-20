"""
mcp/client.py
─────────────
Async HTTP client for the Spring Boot MCP server (port 19170).

Actual API (discovered from GenericMcpController.java):
  GET  /mcp/tools   Authorization: Bearer ...
       → { "jsonrpc": "2.0", "id": "1",
           "result": [ { "type":"function", "name":"opId_method",
                         "description":"...", "parameters":{...} }, ... ] }

  POST /mcp         Authorization: Bearer ...
       body: { "jsonrpc":"2.0", "id":"1", "method":"opId_method",
               "params": { ...args... } }
       → { "jsonrpc": "2.0", "id":"1", "result": <response payload> }

  (No /mcp/health endpoint — we probe with GET /mcp/tools)
  (No /mcp/prompts endpoint)
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from app.commons import metrics as M
from app.commons.config import cfg
from app.commons.logger import get_logger
from app.rbac.jwt_handler import mint_service_token
from app.connections import get_mcp_http_client

log = get_logger(__name__)


class MCPClientError(Exception):
    """Raised when the MCP server returns an error or is unreachable."""
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MCPClient:
    """Thin async wrapper around the Spring Boot MCP REST/JSON-RPC API.

    URL layout (MCP_BASE_URL = full API root, e.g. http://host:19170/mcp-service/mcp):
      GET  {base}/tools   → list tools (JSON-RPC envelope)
      POST {base}/tools   → execute tool (JSON-RPC body)

    Both list and execute share the same /tools path; method discriminates.
    """

    @property
    def _http(self) -> httpx.AsyncClient:
        return get_mcp_http_client()

    def _base(self) -> str:
        """Return MCP_BASE_URL with no trailing slash."""
        return cfg.MCP_BASE_URL.rstrip("/")

    def _url(self, path: str) -> str:
        """Build a full URL: base + path (path should start with '/')."""
        return self._base() + path

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """
        Build headers for MCP requests.

        Forwards the caller's auth context to the MCP server so it can call the real
        downstream service on behalf of the user:
          - X-INT-TKN  : the gc gateway's internal token (gateway mode). MCP validates it
                         (caller stays gateway-service → no allow-list change) and re-issues
                         a fresh internal token to the downstream service.
          - Authorization : the user JWT (standalone / off-platform mode).
          - X-AR-KEY / X-TRACE-ID / X-Time-Zone / X-Date-Time : role + tracing context.
        Fallback: MCP_BEARER_TOKEN only when no user/gateway token is present (e.g. startup
        tool listing). X-API-KEY is already a default header on the httpx client.
        """
        forward = (cfg.GC_INTERNAL_HEADER, "Authorization", cfg.GC_ROLE_HEADER,
                   "X-TRACE-ID", "X-Time-Zone", "X-Date-Time")
        headers: dict[str, str] = {}
        if extra:
            lower = {k.lower(): v for k, v in extra.items()}
            for name in forward:
                val = lower.get(name.lower())
                if val:
                    headers[name] = val
        # MCP's JwtSecurityContextFilter reads the caller token from the Authorization
        # header and only treats it as internal when the Bearer token's X_TP claim is an
        # internal type (e.g. SVC_PER_USER). The gateway delivers the internal token in the
        # X-INT-TKN header, so mirror it into Authorization: Bearer — otherwise MCP falls to
        # the user path and validates with the (short) jwtSecret instead of internalJwtSecret.
        int_tkn = headers.get(cfg.GC_INTERNAL_HEADER)
        if int_tkn and "Authorization" not in headers:
            raw = int_tkn.split(" ", 1)[1] if int_tkn.lower().startswith("bearer ") else int_tkn
            headers["Authorization"] = f"Bearer {raw}"

        # Fallback credential only when the caller supplied none — i.e. the agent's own
        # tokenless calls (startup/health tool discovery). Prefer an explicit
        # MCP_BEARER_TOKEN override; otherwise mint a short-lived iss=GC360 discovery token
        # (the shape MCP accepts from chats). Execution during a chat always carries the
        # caller's token, so this never authenticates a state-changing call.
        has_cred = cfg.GC_INTERNAL_HEADER in headers or "Authorization" in headers
        if not has_cred:
            if cfg.MCP_BEARER_TOKEN:
                headers["Authorization"] = f"Bearer {cfg.MCP_BEARER_TOKEN}"
            else:
                minted = mint_service_token()
                if minted:
                    headers["Authorization"] = f"Bearer {minted}"
        return headers

    # ── Tool endpoints ────────────────────────────────────────────────────────

    async def list_tools(
        self, extra_headers: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """
        Return raw tool definitions from GET {base}/tools.
        Response: { "jsonrpc":"2.0", "id":"1", "result": [ tool, ... ] }
        Each tool is already in near-OpenAI format:
          { "type":"function", "name":"...", "description":"...", "parameters":{...} }

        `extra_headers` forwards the caller's X-INT-TKN so discovery authenticates to
        MCP the same way execution does.
        """
        resp = await self._get("/tools", extra_headers=extra_headers)
        data = resp.json()
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        return result

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a named tool via POST {base} (JSON-RPC). The MCP execute handler is
        @PostMapping on the base (/mcp-service/mcp); /tools is GET-only (discovery).

        Request:
          { "jsonrpc":"2.0", "id":"<uuid>", "method":"tool_name", "params":{...args...} }

        Response:
          { "jsonrpc":"2.0", "id":"<uuid>", "result": <payload> }
        """
        start = time.perf_counter()
        rpc_id = str(uuid.uuid4())

        log.bind(func="execute_tool", tool=tool_name).info(
            f"Executing tool '{tool_name}' with args={list(arguments)}"
        )

        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": tool_name,
            "params": arguments,
        }

        resp = await self._post("", body=body, extra_headers=request_headers)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        data = resp.json()

        # Surface JSON-RPC application-level errors
        if "error" in data:
            err = data["error"]
            raise MCPClientError(
                f"MCP tool error: {err}",
                status_code=None,
            )

        log.bind(func="execute_tool", tool=tool_name, duration_ms=duration_ms).info(
            f"Tool '{tool_name}' completed in {duration_ms}ms"
        )
        return data  # caller reads data["result"]

    # ── Prompts ───────────────────────────────────────────────────────────────
    # The Spring Boot MCP server has no prompts endpoint.
    # These return empty / raise gracefully so the rest of the app still works.

    async def list_prompts(self) -> list[dict[str, Any]]:
        return []

    async def get_prompt(self, name: str) -> dict[str, Any]:
        return {}

    async def execute_prompt(self, name: str, arguments: dict[str, str]) -> str:
        raise MCPClientError("Prompts are not supported by this MCP server.")

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """Probe by calling GET {base}/tools — UP if it responds."""
        try:
            resp = await self._get("/tools")
            data = resp.json()
            tool_count = len(data.get("result", []))
            return {"status": "UP", "tools": tool_count}
        except Exception as exc:
            return {"status": "DOWN", "error": str(exc)}

    # ── Private HTTP helpers ──────────────────────────────────────────────────

    async def _get(self, path: str, extra_headers: dict[str, str] | None = None) -> httpx.Response:
        url = self._url(path)
        headers = self._auth_headers(extra_headers)
        op = f"GET {path}"
        with M.timed(M.mcp_duration, op):
            try:
                resp = await self._http.get(url, headers=headers)
                self._raise_for_status(resp, url)
                M.mcp_requests_total.labels(op, "success").inc()
                return resp
            except httpx.TimeoutException as exc:
                M.mcp_requests_total.labels(op, "timeout").inc()
                raise MCPClientError(f"MCP server timeout on GET {url}: {exc}")
            except httpx.RequestError as exc:
                M.mcp_requests_total.labels(op, "error").inc()
                raise MCPClientError(f"MCP server connection error on GET {url}: {exc}")
            except MCPClientError:
                M.mcp_requests_total.labels(op, "error").inc()
                raise

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = self._url(path)
        headers = self._auth_headers(extra_headers)
        op = f"POST {path}"
        with M.timed(M.mcp_duration, op):
            try:
                resp = await self._http.post(url, json=body, headers=headers)
                self._raise_for_status(resp, url)
                M.mcp_requests_total.labels(op, "success").inc()
                return resp
            except httpx.TimeoutException as exc:
                M.mcp_requests_total.labels(op, "timeout").inc()
                raise MCPClientError(f"MCP server timeout on POST {url}: {exc}")
            except httpx.RequestError as exc:
                M.mcp_requests_total.labels(op, "error").inc()
                raise MCPClientError(f"MCP server connection error on POST {url}: {exc}")
            except MCPClientError:
                M.mcp_requests_total.labels(op, "error").inc()
                raise

    @staticmethod
    def _raise_for_status(resp: httpx.Response, path: str) -> None:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or resp.text
            except Exception:
                detail = resp.text
            raise MCPClientError(
                f"MCP server {resp.status_code} on {path}: {detail}",
                status_code=resp.status_code,
            )


# Module-level singleton
mcp_client = MCPClient()
