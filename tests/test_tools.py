"""
tests/test_tools.py
────────────────────
Tool registry endpoint tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.models.mcp import Tool, ToolParameter


def _sample_tools():
    return [
        Tool(
            name="search",
            description="Search the database. Risk level: LOW",
            risk_level="LOW",
            requires_confirmation=False,
            parameters=[ToolParameter(name="query", type="string", required=True)],
        ),
        Tool(
            name="delete_record",
            description="Delete a record. Risk level: HIGH",
            risk_level="HIGH",
            requires_confirmation=True,
        ),
    ]


def test_list_tools(client):
    with patch(
        "app.mcp.tool_registry.ToolRegistry.get_tools",
        new_callable=AsyncMock,
        return_value=_sample_tools(),
    ):
        response = client.get("/api/tools")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    names = [t["name"] for t in data["tools"]]
    assert "search" in names
    assert "delete_record" in names


def test_get_tool_detail(client):
    from app.mcp.tool_registry import tool_registry as _registry
    tools = _sample_tools()
    with patch.object(_registry, "get_tool", AsyncMock(return_value=tools[0])):
        response = client.get("/api/tools/search")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "search"
    assert data["risk_level"] == "LOW"


def test_get_tool_not_found(client):
    from app.mcp.tool_registry import tool_registry as _registry
    with patch.object(_registry, "get_tool", AsyncMock(return_value=None)):
        response = client.get("/api/tools/nonexistent")
    assert response.status_code == 404


def test_tool_risk_parsing():
    """Unit test for risk extraction logic."""
    from app.models.mcp import _extract_risk, _needs_confirmation

    assert _extract_risk("Do something. Risk level: HIGH") == "HIGH"
    assert _extract_risk("Safe operation. Risk level: LOW") == "LOW"
    assert _extract_risk("No risk mentioned") == "MEDIUM"
    assert _needs_confirmation({"name": "delete_user", "description": ""}, "MEDIUM") is True
    assert _needs_confirmation({"name": "list_users", "description": ""}, "MEDIUM") is False
    assert _needs_confirmation({"name": "anything", "description": ""}, "HIGH") is True
    assert _needs_confirmation({"name": "read_data", "description": ""}, "LOW") is False
