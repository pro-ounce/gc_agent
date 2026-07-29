"""
tests/test_health.py
────────────────────
Health endpoint tests (Spring-actuator semantics: aggregate DOWN -> 503).
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch


@contextmanager
def _deps(mcp_status: str, llm_status: str = "UP"):
    """Mock the MCP + LLM health probes (store is in-memory UP in tests)."""
    with patch("app.mcp.client.MCPClient.health", new_callable=AsyncMock,
               return_value={"status": mcp_status}), \
         patch("app.services.llm_service.OllamaProvider.health", new_callable=AsyncMock,
               return_value={"status": llm_status, "provider": "ollama"}):
        yield


def test_health_up_returns_200(client):
    with _deps("UP", "UP"):
        r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "UP"
    assert "app" in data and "version" in data
    assert "components" in data


def test_health_down_when_mcp_down(client):
    with _deps("DOWN", "UP"):
        r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "DOWN"


def test_api_health_alias(client):
    with _deps("UP", "UP"):
        r = client.get("/api/health")
    assert r.status_code == 200
    assert "components" in r.json()


def test_actuator_health_up(client):
    with _deps("UP", "UP"):
        r = client.get("/actuator/health")
    assert r.status_code == 200
    assert r.json()["status"] == "UP"
