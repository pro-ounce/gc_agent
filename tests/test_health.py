"""
tests/test_health.py
────────────────────
Health endpoint tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch


def test_health_returns_200(client):
    with patch(
        "app.mcp.client.MCPClient.health",
        new_callable=AsyncMock,
        return_value={"status": "UP"},
    ):
        response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("UP", "DEGRADED")
    assert "app" in data
    assert "version" in data


def test_health_degraded_when_mcp_down(client):
    with patch(
        "app.mcp.client.MCPClient.health",
        new_callable=AsyncMock,
        return_value={"status": "DOWN", "error": "connection refused"},
    ):
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "DEGRADED"


def test_api_health_alias(client):
    with patch(
        "app.mcp.client.MCPClient.health",
        new_callable=AsyncMock,
        return_value={"status": "UP"},
    ):
        response = client.get("/api/health")
    assert response.status_code == 200
