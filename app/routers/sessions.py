"""
routers/sessions.py
────────────────────
Session management endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.chat import SessionSummary
from app.rbac.middleware import get_current_user, require_permission
from app.rbac.models import User
from app.rbac.permissions import Permissions
from app.services.session_service import session_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", summary="List sessions for current user")
async def list_sessions(
    user: User = Depends(require_permission(Permissions.SESSION_LIST)),
) -> dict:
    summaries = session_service.list_sessions(user_id=user.id)
    return {"total": len(summaries), "sessions": summaries}


@router.get("/{session_id}", summary="Get session details")
async def get_session(
    session_id: str,
    user: User = Depends(require_permission(Permissions.SESSION_READ)),
) -> dict:
    session = session_service.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "message_count": len(session.messages),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "messages": [m.model_dump() for m in session.messages],
    }


@router.delete("/{session_id}", summary="Delete a session")
async def delete_session(
    session_id: str,
    user: User = Depends(require_permission(Permissions.SESSION_DELETE)),
) -> dict:
    deleted = session_service.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": True, "session_id": session_id}


@router.post("/{session_id}/clear", summary="Clear messages but keep session")
async def clear_session(
    session_id: str,
    _: User = Depends(require_permission(Permissions.SESSION_DELETE)),
) -> dict:
    cleared = session_service.clear_messages(session_id)
    if not cleared:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"cleared": True, "session_id": session_id}
