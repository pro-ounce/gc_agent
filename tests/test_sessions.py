"""
tests/test_sessions.py
──────────────────────
Session CRUD endpoint tests.
"""
from __future__ import annotations

from app.services.session_service import session_service


def test_get_nonexistent_session(client):
    response = client.get("/api/sessions/does-not-exist")
    assert response.status_code == 404


def test_delete_nonexistent_session(client):
    response = client.delete("/api/sessions/does-not-exist")
    assert response.status_code == 404


def test_session_lifecycle(client):
    from unittest.mock import AsyncMock, MagicMock
    from app.services.llm_service import LLMResponse

    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(
        return_value=LLMResponse(
            text="Session response",
            tool_calls=[],
            finish_reason="stop",
            model="test",
        )
    )

    import app.services.chat_service as cs
    original = cs.llm

    cs.llm = lambda: mock_llm
    cs.tool_registry.as_tools = AsyncMock(return_value=[])

    try:
        # Create session via chat
        session_id = "lifecycle-test-123"
        client.post("/api/chat", json={"session_id": session_id, "message": "Hello"})

        # Get session
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session_id
        assert data["message_count"] >= 1

        # Clear session
        resp = client.post(f"/api/sessions/{session_id}/clear")
        assert resp.status_code == 200

        # Delete session
        resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200

        # Verify deleted
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 404
    finally:
        cs.llm = original
