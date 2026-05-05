"""
tests/test_chat.py
──────────────────
Chat endpoint tests — mock LLM and MCP tool execution.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm_service import LLMResponse


def _mock_llm_response(text: str = "Hello!", tool_calls=None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls or [],
        finish_reason="stop",
        model="llama3.1",
        usage={"input_tokens": 10, "output_tokens": 5},
    )


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Replace llm() singleton with a mock for all chat tests."""
    mock = MagicMock()
    mock.complete = AsyncMock(return_value=_mock_llm_response("I'm a test response."))
    monkeypatch.setattr("app.services.chat_service.llm", lambda: mock)
    monkeypatch.setattr("app.services.llm_service._llm", mock)
    return mock


@pytest.fixture(autouse=True)
def mock_tool_registry(monkeypatch):
    """Empty tool registry for most tests."""
    monkeypatch.setattr(
        "app.services.chat_service.tool_registry.as_tools",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.chat_service.tool_registry.get_tool",
        AsyncMock(return_value=None),
    )


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_chat_basic(client, sample_session_id):
    response = client.post(
        "/api/chat",
        json={"session_id": sample_session_id, "message": "Hello"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "assistant_message" in data
    assert data["session_id"] == sample_session_id
    assert data["assistant_message"] == "I'm a test response."


def test_chat_missing_message(client, sample_session_id):
    response = client.post(
        "/api/chat",
        json={"session_id": sample_session_id, "message": ""},
    )
    assert response.status_code == 422  # Pydantic validation


def test_chat_missing_session(client):
    response = client.post(
        "/api/chat",
        json={"message": "Hello"},
    )
    assert response.status_code == 422


def test_chat_persists_session(client, sample_session_id):
    """Second message in same session should have history."""
    session_id = "persist-test-001"

    client.post("/api/chat", json={"session_id": session_id, "message": "First message"})
    client.post("/api/chat", json={"session_id": session_id, "message": "Second message"})

    from app.services.session_service import session_service
    session = session_service.get(session_id)
    assert session is not None
    assert len(session.messages) >= 2


def test_chat_with_tool_calls(client, monkeypatch, sample_session_id):
    """LLM returns tool call → execute → return result."""
    from app.services.llm_service import ToolCall
    from app.models.mcp import Tool, ToolResult

    # First response: tool call
    tc = ToolCall(id="tc_1", name="get_info", input={"query": "test"})
    first_response = _mock_llm_response("", tool_calls=[tc])
    # Second response: final answer after tool execution
    second_response = _mock_llm_response("Here is the info: test result")

    mock_complete = AsyncMock(side_effect=[first_response, second_response])
    mock_llm = MagicMock()
    mock_llm.complete = mock_complete
    monkeypatch.setattr("app.services.chat_service.llm", lambda: mock_llm)

    # Tool has LOW risk (no confirmation needed)
    low_risk_tool = Tool(name="get_info", risk_level="LOW", requires_confirmation=False)
    monkeypatch.setattr(
        "app.services.chat_service.tool_registry.get_tool",
        AsyncMock(return_value=low_risk_tool),
    )
    monkeypatch.setattr(
        "app.services.chat_service.tool_registry.execute",
        AsyncMock(return_value=ToolResult(tool_name="get_info", success=True, output="test result")),
    )
    monkeypatch.setattr(
        "app.services.chat_service.tool_registry.as_tools",
        AsyncMock(return_value=[{
            "type": "function",
            "function": {"name": "get_info", "description": "", "parameters": {}},
        }]),
    )

    response = client.post(
        "/api/chat",
        json={"session_id": "tool-test-001", "message": "Get info for test"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "get_info" in data["tool_calls_made"]


def test_confirm_action_no_pending(client, sample_session_id):
    response = client.post(
        "/api/chat/confirm",
        json={
            "session_id": "no-pending-session",
            "action_id": "fake-action-id",
            "confirmed": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "pending action" in data["assistant_message"].lower()


def test_chat_returns_model_field(client, sample_session_id):
    response = client.post(
        "/api/chat",
        json={"session_id": sample_session_id, "message": "What model are you?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "model" in data
