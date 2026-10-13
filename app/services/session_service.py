"""
services/session_service.py
────────────────────────────
CRUD operations for chat sessions backed by Redis.
"""
from __future__ import annotations

from ..commons.config import cfg
from ..commons.logger import get_logger
from ..connections import redis_delete, redis_get_json, redis_set_json
from ..models.session import Session

log = get_logger(__name__)

_PREFIX = "session:"
# ── History archive (durability) ─────────────────────────────────────────────────
# The LIVE session (`session:<id>`) carries the 24h TTL for active-chat behaviour. The HISTORY
# archive (`history:*`) is written with NO TTL, so conversations are RETAINED INDEFINITELY, survive
# restarts, and ride the agent-kv S3 snapshot (durability) — the "memory → durable sink" copy.
#   history:<id>        → the latest state of the CURRENT conversation (refreshed each save)
#   history:<id>:<ms>   → an IMMUTABLE, append-only record of a conversation that was cleared
#                         ("new chat"), so prior conversations are never lost.
_HISTORY_PREFIX = "history:"


def _key(session_id: str) -> str:
    return f"{_PREFIX}{session_id}"


def _hkey(session_id: str) -> str:
    return f"{_HISTORY_PREFIX}{session_id}"


def _hkey_archived(session_id: str, ms: int) -> str:
    return f"{_HISTORY_PREFIX}{session_id}:{ms}"


def count_sessions() -> int:
    """Approximate active-session count (for metrics)."""
    from ..connections import redis_client
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
        payload = session.model_dump()
        redis_set_json(_key(session.session_id), payload, ex=cfg.SESSION_TTL_SECONDS)
        # Durable, indefinitely-retained copy of the current conversation (NO ttl) — history must
        # outlive the live-session TTL and survive restarts. Non-fatal if the sink is unavailable.
        try:
            redis_set_json(_hkey(session.session_id), payload)
        except Exception as exc:  # noqa: BLE001
            log.bind(func="history_archive", session_id=session.session_id).warning(
                f"history archive write failed (non-fatal): {exc}")

    def delete(self, session_id: str) -> bool:
        exists = self.get(session_id) is not None
        redis_delete(_key(session_id))
        log.bind(func="delete", session_id=session_id).info("Session deleted")
        return exists

    def clear_messages(self, session_id: str) -> bool:
        session = self.get(session_id)
        if not session:
            return False
        # "New chat": before clearing, preserve the conversation as an IMMUTABLE, append-only
        # history record (no TTL) so a new chat never loses the prior one.
        if session.messages:
            try:
                import time as _time
                redis_set_json(_hkey_archived(session_id, int(_time.time() * 1000)),
                               session.model_dump())
            except Exception as exc:  # noqa: BLE001
                log.bind(func="history_archive_on_clear", session_id=session_id).warning(
                    f"archive-on-clear failed (non-fatal): {exc}")
        session.messages = []
        session.pending_action_id = None
        self.save(session)
        return True

    def list_history(self, user_id: str | None = None) -> list[dict]:
        """Preserved conversations (current + archived) for a user, newest first — the DURABLE
        history, independent of the live-session TTL. Reads the `history:*` sink."""
        from ..connections import redis_client
        try:
            keys = redis_client.keys(f"{_HISTORY_PREFIX}*")
        except Exception:  # noqa: BLE001
            keys = []
        out: list[dict] = []
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, (bytes, bytearray)) else str(raw_key)
            data = redis_get_json(key)
            if not isinstance(data, dict):
                continue
            if user_id and str(data.get("user_id")) != str(user_id):
                continue
            msgs = data.get("messages") or []
            out.append({
                "session_id": data.get("session_id"),
                "archived": ":" in key[len(_HISTORY_PREFIX):],   # True → immutable, cleared convo
                "message_count": len(msgs),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "user_id": data.get("user_id"),
                "last_question": next(
                    (m.get("content") for m in reversed(msgs) if m.get("role") == "user"), None),
            })
        return sorted(out, key=lambda s: s.get("updated_at") or "", reverse=True)

    def list_sessions(self, user_id: str | None = None) -> list[dict]:
        """List all active sessions (optionally filtered by user_id)."""
        from ..connections import redis_client

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
