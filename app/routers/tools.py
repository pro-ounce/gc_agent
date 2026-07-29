"""
routers/tools.py
────────────────
Endpoints for browsing MCP tools.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.mcp.tool_registry import tool_registry
from app.models.mcp import Tool
from app.rbac.middleware import require_permission
from app.rbac.permissions import Permissions

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", summary="List all available MCP tools")
async def list_tools(
    force_refresh: bool = Query(False, alias="refresh"),
    _: object = Depends(require_permission(Permissions.TOOL_LIST)),
) -> dict:
    from app.commons.config import cfg
    from app.mcp.client import MCPClientError

    error: str | None = None
    try:
        tools = await tool_registry.get_tools(force_refresh=force_refresh)
    except MCPClientError as exc:
        tools = []
        error = str(exc)

    return {
        "total": len(tools),
        "mcp_url": cfg.MCP_BASE_URL,
        "error": error,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "risk_level": t.risk_level,
                "requires_confirmation": t.requires_confirmation,
                "parameters": [p.model_dump() for p in t.parameters],
            }
            for t in tools
        ],
    }


@router.get("/{tool_name}", summary="Get details for a single tool")
async def get_tool(
    tool_name: str,
    _: object = Depends(require_permission(Permissions.TOOL_LIST)),
) -> dict:
    from fastapi import HTTPException

    tool = await tool_registry.get_tool(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return {
        "name": tool.name,
        "description": tool.description,
        "risk_level": tool.risk_level,
        "requires_confirmation": tool.requires_confirmation,
        "parameters": [p.model_dump() for p in tool.parameters],
        "input_schema": tool.input_schema,
    }
