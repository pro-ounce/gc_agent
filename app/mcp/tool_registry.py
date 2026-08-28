"""
mcp/tool_registry.py
────────────────────
In-memory TTL cache for MCP tools with OpenAI-compatible tool schema.

Usage:
    registry = ToolRegistry()
    tools = await registry.get_tools()   # list[Tool]
    schema = await registry.as_tools()   # OpenAI function-calling format
    result = await registry.execute(name, args)  # ToolResult
"""
from __future__ import annotations

import time
from typing import Any

from ..commons import metrics as M
from ..commons.config import cfg
from ..commons.flags import flags
from ..commons.logger import get_logger
from ..connections import redis_get_json, redis_set_json
from ..services import runtime_config
from ..mcp.client import MCPClientError, mcp_client
from ..mcp.tool_index import tool_index
from ..models.mcp import Tool, ToolResult

log = get_logger(__name__)

# Store key (OpenSearch agent-kv) for the last /mcp/tools catalog. Persisting it lets a
# restart rebuild the registry WITHOUT a live MCP fetch — the tokenless startup/health
# fetch 401s (MCP guards /mcp/tools), so without this the cache would be empty until the
# first authenticated chat. The raw MCP payloads round-trip via Tool.from_mcp_payload.
_CATALOG_KEY = "mcp:tools:catalog"

# Cache for deterministic application code/name → id resolution (see _resolve_application_id).
_APP_MAP_CACHE: dict[str, Any] = {"map": None, "ts": 0.0}
_APP_MAP_TTL = 300.0  # seconds

# Name prefixes that read state (safe for a read-only chatbot). Everything else —
# _put/_delete/_patch, and _post whose verb isn't a read (activate/deactivate/delete/
# create/update/add/remove/reset/restore/upload/bulk…) — is treated as a mutation.
_READ_VERBS = (
    "get", "list", "fetch", "search", "find", "view", "read", "count",
    "download", "export", "validate", "check", "preview", "lookup",
)


def _is_read_only(name: str) -> bool:
    n = name.lower()
    if n.endswith(("_put", "_delete", "_patch")):
        return False
    if n.endswith("_get"):
        return True
    # _post (or anything else): allow only when the verb prefix reads (search-style POST).
    return any(n.startswith(v) for v in _READ_VERBS)


def is_mutation(name: str) -> bool:
    """A tool that changes state (create/update/delete/activate/…). These must be
    confirmed by the user before execution — the inverse of the read-only set."""
    return not _is_read_only(name)


def _log_schema_cost(schemas: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    """Log what the tool schemas actually cost in prompt tokens, and name the worst
    offender. Prompt-processing dominates latency on CPU (~25 tok/s), and Ollama
    silently context-shifts when prompt+reply exceed num_ctx — both are invisible
    without this. ~4 chars/token is close enough to spot a 1000-token schema.
    """
    import json

    if not schemas:
        return schemas
    sizes = sorted(
        ((s.get("function", {}).get("name", "?"), len(json.dumps(s)) // 4) for s in schemas),
        key=lambda kv: kv[1],
        reverse=True,
    )
    approx = sum(n for _, n in sizes)
    worst = ", ".join(f"{name}={tok}tok" for name, tok in sizes[:3])
    # Log the FULL selected set — without it you can't tell whether RAG actually
    # surfaced the tool the query needs (the difference between "model won't call"
    # and "right tool never offered").
    selected = [s.get("function", {}).get("name", "?") for s in schemas]
    log.bind(
        func="select_tools", tools=len(schemas), approx_tool_tokens=approx, selected=selected
    ).info(
        f"tool schemas: {len(schemas)}/{total} tools ≈{approx} tokens (largest: {worst}) "
        f"| num_ctx={cfg.LLM_NUM_CTX} | selected={selected}"
    )
    return schemas


class ToolRegistry:
    def __init__(self) -> None:
        self._cache: list[Tool] = []
        self._loaded_at: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_tools(
        self, force_refresh: bool = False, request_headers: dict[str, str] | None = None
    ) -> list[Tool]:
        if force_refresh or self._is_stale():
            await self._refresh(request_headers)
        return self._cache

    async def get_tool(self, name: str) -> Tool | None:
        tools = await self.get_tools()
        return next((t for t in tools if t.name == name), None)

    async def as_tools(self, request_headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Convert tool list to OpenAI function-calling schema (used by OLLAMA).

        `request_headers` carries the caller's X-INT-TKN so the MCP tool-listing call
        is authenticated the same way execution is (discovery needs a valid token too).
        """
        tools = await self.get_tools(request_headers=request_headers)
        return [_to_tool_schema(t) for t in tools]

    async def select_tools(
        self, query: str, request_headers: dict[str, str] | None = None,
        extra: list[str] | None = None, only: list[str] | None = None,
        slim: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return OpenAI schemas for only the tools relevant to `query` (tool-RAG).
        Falls back to ALL tools when: the flag is off, the catalog is small
        (≤ TOOL_RAG_MIN_TOOLS), or retrieval fails — so it never breaks chat.

        `only` narrows the offered set to exactly those tool names (used when a mutation
        skill is active: hiding the read tools stops a weaker model from looking data up
        and then declining the action — the id lookups it needs still run server-side)."""
        tools = await self.get_tools(request_headers=request_headers)
        total_all = len(tools)
        if only:
            keep = set(only)
            picked = [t for t in tools if t.name in keep]
            if picked:
                schemas = [_to_tool_schema(t) for t in picked]
                _apply_slim(schemas, slim)
                return _log_schema_cost(schemas, len(tools))
        if runtime_config.get_bool("CHATBOT_READ_ONLY"):
            # Never OFFER mutations to the model — it can't call what it can't see.
            tools = [t for t in tools if _is_read_only(t.name)]
            if len(tools) != total_all:
                log.bind(func="select_tools", read_only=True, kept=len(tools), total=total_all).info(
                    f"read-only: offering {len(tools)}/{total_all} tools (mutations withheld)"
                )
        if not runtime_config.get_bool("TOOL_RAG_ENABLED") or len(tools) <= cfg.TOOL_RAG_MIN_TOOLS:
            return _log_schema_cost([_to_tool_schema(t) for t in tools], len(tools))
        names = await tool_index.search(query, runtime_config.get_int("TOOL_RAG_TOP_K"))
        if not names:
            return [_to_tool_schema(t) for t in tools]
        # Always offer the resolver tools (id lookups) + any per-call extras (an active
        # skill's backing tool) so name→id chains and actions never break for lack of the
        # tool being retrieved. De-duped, appended after the RAG hits.
        pinned = [p.strip() for p in (cfg.TOOL_RAG_PINNED or "").split(",") if p.strip()]
        for p in pinned + list(extra or []):
            if p not in names:
                names.append(p)
        by_name = {t.name: t for t in tools}
        picked = [by_name[n] for n in names if n in by_name] or tools
        return _log_schema_cost([_to_tool_schema(t) for t in picked], len(tools))

    async def _app_map(self, request_headers: dict[str, str] | None) -> dict[str, Any]:
        """Cached {code|name|shortCode (lower) → applicationId} from getAllApplications_get."""
        import time as _t
        now = _t.monotonic()
        cache = _APP_MAP_CACHE
        if cache["map"] is not None and (now - cache["ts"]) < _APP_MAP_TTL:
            return cache["map"]
        amap: dict[str, Any] = {}
        try:
            # Reuse execute() so the JSON-RPC/envelope normalization is shared. Safe from
            # recursion: getAllApplications_get carries no applicationId to resolve.
            res = await self.execute("getAllApplications_get", {}, request_headers)
            out = res.output if res.success else None
            data = out.get("data") if isinstance(out, dict) else out
            for a in (data or []):
                if not isinstance(a, dict):
                    continue
                aid = a.get("applicationId")
                if aid in (None, ""):
                    continue
                for key in (a.get("applicationCode"), a.get("applicationName"), a.get("applicationShortCode")):
                    if key:
                        amap[str(key).strip().lower()] = aid
        except Exception as exc:  # noqa: BLE001 — resolution is best-effort; never break the call
            log.bind(func="app_map").warning(f"app map load failed: {exc}")
        cache["map"], cache["ts"] = amap, now
        return amap

    async def _resolve_application_id(
        self, arguments: dict[str, Any], request_headers: dict[str, str] | None
    ) -> None:
        """If applicationId is a non-numeric code/name, replace it with the numeric id."""
        val = arguments.get("applicationId")
        if val is None or str(val).strip() == "" or str(val).strip().isdigit():
            return
        resolved = (await self._app_map(request_headers)).get(str(val).strip().lower())
        if resolved is not None:
            log.bind(func="resolve_app_id", frm=str(val), to=str(resolved)).info(
                f"resolved applicationId '{val}' → {resolved}"
            )
            arguments["applicationId"] = resolved

    async def _resolve_user_id(
        self, arguments: dict[str, Any], request_headers: dict[str, str] | None
    ) -> None:
        """Resolve a userId given as a username (or in a userName field) to the numeric id,
        so assignment tools can be driven by name."""
        val = arguments.get("userId")
        name = None
        if val is not None and str(val).strip() and not str(val).strip().isdigit():
            name = str(val).strip()
        elif not str(val or "").strip():
            for k in ("userName", "username"):
                if str(arguments.get(k) or "").strip():
                    name = str(arguments[k]).strip()
                    break
        if not name:
            return
        try:
            res = await self.execute("getUserByUserName_get", {"userName": name}, request_headers)
            data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
            if isinstance(data, list):
                data = data[0] if data else None
            uid = (data or {}).get("id") or (data or {}).get("userId")
            if uid is not None:
                arguments["userId"] = uid
                log.bind(func="resolve_user_id", frm=name, to=str(uid)).info(
                    f"resolved userId '{name}' → {uid}"
                )
        except Exception:  # noqa: BLE001 — resolution is best-effort
            pass

    async def _resolve_role_id(
        self, arguments: dict[str, Any], request_headers: dict[str, str] | None
    ) -> None:
        """Resolve an applicationRoleId given as a role NAME to its numeric id, using the
        already-resolved applicationId's role list."""
        val = arguments.get("applicationRoleId")
        if val is None or not str(val).strip() or str(val).strip().isdigit():
            return
        role_name = str(val).strip().lower()
        app_id = arguments.get("applicationId")
        if not str(app_id or "").strip() or not str(app_id).strip().isdigit():
            return
        try:
            res = await self.execute("getApplicationRolesByAppId_get", {"applicationId": app_id}, request_headers)
            data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
            if isinstance(data, list):
                for r in data:
                    nm = str(r.get("roleName") or r.get("role") or r.get("name") or "").strip().lower()
                    if nm and (nm == role_name or role_name in nm or nm in role_name):
                        rid = r.get("applicationRoleId") or r.get("id") or r.get("roleId")
                        if rid is not None:
                            arguments["applicationRoleId"] = rid
                            log.bind(func="resolve_role_id", frm=role_name, to=str(rid)).info(
                                f"resolved role '{role_name}' → {rid}"
                            )
                            return
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _caller_user_id(request_headers: dict[str, str] | None) -> str | None:
        """The current user's id from the caller/forwarded JWT (sub claim)."""
        if not request_headers:
            return None
        auth = None
        for k in ("Authorization", "authorization", cfg.GC_INTERNAL_HEADER):
            v = request_headers.get(k)
            if v:
                auth = v
                break
        if not auth:
            return None
        raw = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
        try:
            import jwt as _jwt
            claims = _jwt.decode(raw, options={"verify_signature": False})
            return claims.get("sub") or claims.get("userId") or claims.get("user_id")
        except Exception:  # noqa: BLE001
            return None

    async def _apply_default_user_scope(
        self, tool_name: str, arguments: dict[str, Any], request_headers: dict[str, str] | None
    ) -> None:
        """Personal-assistant default: a tool that filters by userId, called with no user
        identifier, is scoped to the caller — so 'my apps/roles' means mine, not everyone's."""
        if not cfg.DEFAULT_USER_SCOPE:
            return
        tool = await self.get_tool(tool_name)
        if not tool or not any(p.name.lower() == "userid" for p in tool.parameters):
            return
        if any(str(arguments.get(k) or "").strip() for k in ("userId", "userid", "username", "userName")):
            return  # the model already named a user — respect it
        uid = self._caller_user_id(request_headers)
        if uid:
            arguments["userId"] = uid
            log.bind(func="user_scope", tool=tool_name, userId=str(uid)).info(
                f"scoped {tool_name} to caller userId={uid}"
            )

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_headers: dict[str, str] | None = None,
    ) -> ToolResult:
        import time as _time
        # Drop any injected/header params the model may have hallucinated into the args
        # (auth/tracing are provided by request_headers → MCP injects the real values).
        arguments = {k: v for k, v in arguments.items() if k.lower() not in _INJECTED_PARAMS}
        # Deterministic id resolution: models reliably name an application by code/name but
        # not by numeric id. If applicationId is non-numeric, resolve it here so any
        # application-scoped tool works without depending on the model to chain a lookup.
        await self._resolve_application_id(arguments, request_headers)
        # Assignment tools are driven by name → resolve userName/roleName to ids (app id first).
        if tool_name in ("addUserApplicationAndRole_post", "addUserApplicationRole_post"):
            await self._resolve_user_id(arguments, request_headers)
            await self._resolve_role_id(arguments, request_headers)
        await self._apply_default_user_scope(tool_name, arguments, request_headers)
        # Skill defaults: fill safe defaults for an action tool's omitted fields.
        from ..services import skills as _skills
        _filled = _skills.apply_defaults(tool_name, arguments)
        if _filled:
            log.bind(func="skill_defaults", tool=tool_name, filled=",".join(_filled)).info(
                f"applied skill defaults on {tool_name}: {_filled}"
            )
        # Lookup QUERIES: a getLookup* call with a lookup NAME ("account category") → its code
        # (USER_ACCOUNT_CATEGORIES); default applicationId to the administration app.
        if tool_name.lower().startswith("getlookup"):
            _lkkey = "lookup" if "lookup" in arguments else ("lookUp" if "lookUp" in arguments else None)
            if _lkkey and arguments.get(_lkkey):
                from ..services import lookups as _lookups
                _lc = await _lookups.resolve_lookup_name(arguments[_lkkey], self.execute, request_headers)
                if _lc and str(_lc) != str(arguments[_lkkey]):
                    log.bind(func="lookup_name", frm=str(arguments[_lkkey]), to=str(_lc)).info(
                        f"resolved lookup '{arguments[_lkkey]}' → {_lc}")
                    arguments[_lkkey] = _lc
            _tool = await self.get_tool(tool_name)
            if _tool and any(p.name.lower() == "applicationid" for p in _tool.parameters) \
                    and not str(arguments.get("applicationId") or "").strip():
                arguments["applicationId"] = cfg.LOOKUP_ADMIN_APP_ID
        # Skill lookups: resolve master-data fields (type/category/status) named by the user
        # to their code, via the ADMINISTRATION-app lookups.
        _sk = _skills.by_tool(tool_name)
        if _sk and _sk.lookups:
            from ..services import lookups as _lookups
            for _fld, _code in _sk.lookups.items():
                _raw = arguments.get(_fld)
                if _raw in (None, ""):
                    continue
                _res = await _lookups.resolve(_code, str(_raw), self.execute, request_headers)
                if _res and str(_res) != str(_raw):
                    log.bind(func="skill_lookup", tool=tool_name, field=_fld,
                             frm=str(_raw), to=str(_res)).info(f"resolved {_fld} '{_raw}' → {_res}")
                    arguments[_fld] = _res
        start = _time.perf_counter()
        try:
            raw = await mcp_client.execute_tool(tool_name, arguments, request_headers)
            duration_ms = round((_time.perf_counter() - start) * 1000, 1)
            M.tool_exec_total.labels(tool_name, "success").inc()
            M.tool_exec_duration.labels(tool_name).observe(duration_ms / 1000.0)

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
            M.tool_exec_total.labels(tool_name, "error").inc()
            M.tool_exec_duration.labels(tool_name).observe(duration_ms / 1000.0)
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

    async def _refresh(self, request_headers: dict[str, str] | None = None) -> None:
        try:
            raw_tools = await mcp_client.list_tools(request_headers)
            self._cache = [Tool.from_mcp_payload(t) for t in raw_tools]
            self._loaded_at = time.time()
            self._persist_catalog(raw_tools)
            log.bind(func="tool_registry").info(
                f"Tool registry refreshed: {len(self._cache)} tools loaded"
            )
            # Tool-RAG index (no-op unless flags.tool_rag_enabled; fail-open).
            await tool_index.reindex(self._cache)
        except MCPClientError as exc:
            log.error(f"Failed to refresh tool registry: {exc}")
            # The tokenless startup/health fetch 401s against MCP's guarded /mcp/tools →
            # fall back to the last catalog persisted in the store (OpenSearch) so the
            # agent still has tools until a later authenticated request refreshes from MCP.
            if not self._cache:
                self._load_from_store()

    def _persist_catalog(self, raw_tools: list[dict]) -> None:
        """Save the raw /mcp/tools catalog to the store (OpenSearch). Best-effort /
        fail-open — a store hiccup must never break a successful MCP refresh."""
        try:
            redis_set_json(_CATALOG_KEY, raw_tools)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Tool catalog persist failed (fail-open): {exc}")

    def _load_from_store(self) -> None:
        """Rebuild the registry from the last catalog persisted in the store. Leaves
        _loaded_at at 0 so the next authenticated get_tools() still re-fetches from MCP
        and re-persists a fresh copy."""
        try:
            raw_tools = redis_get_json(_CATALOG_KEY)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Tool catalog store read failed: {exc}")
            return
        if not raw_tools:
            log.warning(
                "No persisted tool catalog in store — registry stays empty until MCP is reachable"
            )
            return
        self._cache = [Tool.from_mcp_payload(t) for t in raw_tools]
        log.bind(func="tool_registry").info(
            f"Tool registry loaded {len(self._cache)} tools from store (MCP unavailable)"
        )


# Params injected by the gateway/MCP (auth + tracing context) — the LLM must never
# see or fill these, or it hallucinates placeholder tokens (e.g. Authorization) and
# they get sent as bogus query params. MCP injects the real values on the proxied call.
_INJECTED_PARAMS: frozenset[str] = frozenset(
    {
        "authorization", "cookie", "x-int-tkn", "x-ar-key", "x-api-key",
        "x-trace-id", "x-time-zone", "x-date-time", "x-selected-app",
        "x-selected-role", "x-forwarded-for", "x-request-id",
    }
)


def _humanize(name: str) -> str:
    """camelCase / snake_case param name → a short description, so the model knows what to
    put there even when the swagger description is empty. e.g. userName → 'The user name.'"""
    import re as _re

    words = _re.sub(r"(?<!^)(?=[A-Z])", " ", name.replace("_", " ")).lower().strip()
    return f"The {words}." if words else name


def _strip_injected(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove injected header/auth params, and synthesize a description for any param that
    lacks one (agent-side, so MCP/swagger doesn't need updating)."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    new_props: dict[str, Any] = {}
    for k, v in props.items():
        if k.lower() in _INJECTED_PARAMS:
            continue
        if isinstance(v, dict) and not v.get("description"):
            v = {**v, "description": _humanize(k)}
        new_props[k] = v
    out = {**schema, "properties": new_props}
    req = schema.get("required")
    if isinstance(req, list):
        out["required"] = [r for r in req if r.lower() not in _INJECTED_PARAMS]
    return out


def _apply_slim(schemas: list[dict[str, Any]], slim: dict[str, dict[str, Any]] | None) -> None:
    """Replace a tool's exposed parameter schema with a skill-provided slim one (in place).
    Used for mutation skills: the real schema types ids as integers, which makes a weak model
    try to 'find the id' and stall. The slim schema exposes just the name fields as strings —
    the registry still fills the defaults and resolves names→ids at execute time."""
    if not slim:
        return
    for s in schemas:
        fn = s.get("function", {})
        override = slim.get(fn.get("name"))
        if override:
            fn["parameters"] = override


def _to_tool_schema(tool: Tool) -> dict[str, Any]:
    """Convert Tool → OpenAI function-calling dict (compatible with OLLAMA)."""
    # Build parameters schema from input_schema or parsed parameters
    schema: dict[str, Any] = tool.input_schema or {}
    if schema:
        schema = _strip_injected(schema)
    else:
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in tool.parameters:
            if p.name.lower() in _INJECTED_PARAMS:
                continue
            prop: dict[str, Any] = {"type": p.type, "description": p.description or _humanize(p.name)}
            if p.enum:
                prop["enum"] = p.enum
            props[p.name] = prop
            if p.required:
                required.append(p.name)
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": _short_desc(tool.description),
            "parameters": schema,
        },
    }


def _short_desc(desc: str | None, limit: int = 180) -> str:
    """Trim verbose swagger tool descriptions — on CPU, long tool descriptions bloat the
    prompt (thousands of tokens) and blow the prompt-processing time. Keep the first
    sentence, capped at `limit` chars."""
    if not desc:
        return ""
    text = " ".join(desc.split())
    first = text.split(". ", 1)[0]
    out = first if len(first) <= limit else text
    return (out[: limit - 1] + "…") if len(out) > limit else out


# Module-level singleton
tool_registry = ToolRegistry()
