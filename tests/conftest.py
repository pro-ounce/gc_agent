"""
tests/conftest.py
─────────────────
Pytest configuration — sets test environment BEFORE any app imports.
"""
from __future__ import annotations

import os

# ── Set test environment variables BEFORE importing app ───────────────────────
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RBAC_ENABLED", "false")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("MCP_BASE_URL", "http://localhost:19999")   # Never actually called
os.environ.setdefault("LLM_PROVIDER", "ollama")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("JWT_SECRET", "test-secret-key")
os.environ.setdefault("TOOL_RISK_CONFIRMATION", "false")

import pytest
import httpx
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture(scope="session")
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    """JWT header for test user."""
    from app.rbac.jwt_handler import create_access_token
    from app.rbac.models import User
    user = User(id="test-user", username="tester", roles=["admin"])
    token = create_access_token({"sub": user.id, "roles": user.roles})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api_key_headers():
    return {"X-API-KEY": "test-api-key-placeholder"}


@pytest.fixture
def sample_session_id():
    return "test-session-001"
