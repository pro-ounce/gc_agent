"""
services/session_service.py
────────────────────────────
CRUD operations for chat sessions backed by Redis.
"""
from __future__ import annotations

from app.commons.config import cfg
from app.commons.logger import get_logger
from app.connections import redis_delete, redis_get_json, redis_set_json
from app.models.session import Session

log = get_logger(__name__)

_PREFIX = "session:"


def _key(session_id: str) -> str:
    return f"{_PREFIX}{session_id}"


def count_sessions() -> int:
    """Approximate active-session count (for metrics)."""
    from app.connections import redis_client
    try:
        return len(redis_client.keys(f"{_PREFIX}*"))
    except Exception:
        return 0


class SessionService:

    def get(self, session_id: str) -> Session | None:
        data = redis_get_json(_key(session_id))
        if data is None:
            return None
        return Session(**data)

    def get_or_create(self, session_id: str, user_id: str | None = None) -> Session:
        existing = self.get(session_id)
        if existing:
            return existing
        session = Session(session_id=session_id, user_id=user_id)
        self.save(session)
        log.bind(func="get_or_create", session_id=session_id).info("New session created")
        return session

    def save(self, session: Session) -> None:
        session.touch()
        redis_set_json(_key(session.session_id), session.model_dump(), ex=cfg.SESSION_TTL_SECONDS)

    def delete(self, session_id: str) -> bool:
        exists = self.get(session_id) is not None
        redis_delete(_key(session_id))
        log.bind(func="delete", session_id=session_id).info("Session deleted")
        return exists

    def clear_messages(self, session_id: str) -> bool:
        session = self.get(session_id)
        if not session:
            return False
        session.messages = []
        session.pending_action_id = None
        self.save(session)
        return True

    def list_sessions(self, user_id: str | None = None) -> list[dict]:
        """List all active sessions (optionally filtered by user_id)."""
        from app.connections import redis_client

        keys = redis_client.keys(f"{_PREFIX}*")
        summaries = []
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            data = redis_get_json(key)
            if data is None:
                continue
            if user_id and data.get("user_id") != user_id:
                continue
            summaries.append(
                {
                    "session_id": data["session_id"],
                    "message_count": len(data.get("messages", [])),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "user_id": data.get("user_id"),
                }
            )
        return sorted(summaries, key=lambda s: s["updated_at"], reverse=True)


session_service = SessionService()
