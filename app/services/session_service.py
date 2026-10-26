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


def _category(data: dict) -> str:
    """A coarse CATEGORY for a conversation (for the admin history filters) — from the active
    flow if known, else a keyword scan of the turns."""
    md = data.get("metadata") or {}
    flow = md.get("flow") if isinstance(md.get("flow"), dict) else {}
    name = (flow or {}).get("name") or ""
    if name == "formulation_baseline":
        return "Baseline"
    if name in ("create_user", "entity_create", "remove_role", "onboard"):
        return "Access"
    if name == "data_call":
        return "Data call"
    txt = " ".join(str(m.get("content") or "") for m in (data.get("messages") or [])).lower()
    if "baseline" in txt:
        return "Baseline"
    if any(w in txt for w in ("role", "application", "user", "onboard", "access")):
        return "Access"
    if "data call" in txt:
        return "Data call"
    if "report" in txt:
        return "Reporting"
    return "General"


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
        # The live session is gone (expired past its TTL, or never existed). If a prior conversation
        # still sits in the durable mirror history:<id>, the new empty session's save() is about to
        # OVERWRITE it — losing that conversation. Preserve it as an immutable, per-conversation
        # archive first, so an expired session's history survives exactly like an explicit "new chat".
        self._archive_live_history(session_id)
        session = Session(session_id=session_id, user_id=user_id)
        self.save(session)
        log.bind(func="get_or_create", session_id=session_id).info("New session created")
        return session

    def _archive_live_history(self, session_id: str) -> None:
        """Snapshot the current history:<id> (the soon-to-be-overwritten conversation) into an
        immutable history:<id>:<ms> record. No-op when there is nothing to preserve. This is what
        keeps a session that expired WITHOUT an explicit 'new chat' from losing its history."""
        try:
            prior = redis_get_json(_hkey(session_id))
            if isinstance(prior, dict) and (prior.get("messages") or []):
                import time as _time
                redis_set_json(_hkey_archived(session_id, int(_time.time() * 1000)), prior)
                log.bind(func="archive_live_history", session_id=session_id,
                         msgs=len(prior.get("messages") or [])).info(
                    "archived expired-session history before new session")
        except Exception as exc:  # noqa: BLE001 — preservation is best-effort, never block a new chat
            log.bind(func="archive_live_history", session_id=session_id).warning(
                f"archive-before-new-session failed (non-fatal): {exc}")

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

    def list_history(self, user_id: str | None = None, session_id: str | None = None) -> list[dict]:
        """Preserved conversations (current + archived) for a user, newest first — the DURABLE
        history, independent of the live-session TTL. Reads the `history:*` sink.

        `session_id` scopes the list to ONE conversation base (e.g. `chatbot:<user>:<app>` for
        per-app history) — both the live record and its immutable archives share that session_id.
        None = every conversation for the user (the cross-app view)."""
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
            if session_id and str(data.get("session_id")) != str(session_id):
                continue
            msgs = data.get("messages") or []
            # archived = the key carries a `:<ms>` suffix BEYOND `history:<session_id>` (an
            # immutable, cleared-convo record). NB the stable session id is itself `agent:<userId>`
            # (contains a colon), so a naive ":" scan would misflag every CURRENT convo as archived —
            # derive it from the record's own session_id instead.
            sid = data.get("session_id") or ""
            archived = key.startswith(f"{_HISTORY_PREFIX}{sid}:")
            # PER-DAY SEPARATION: split one conversation record into one entry PER CALENDAR DAY (UTC,
            # from each message's own timestamp), so a continuous session shows as distinct, reopenable
            # per-day chats. The detail endpoint re-filters to that day via the `#<day>` key suffix.
            by_day: dict[str, list] = {}
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                ts = str(m.get("timestamp") or "")
                day = ts[:10] if len(ts) >= 10 else (str(data.get("created_at") or "")[:10] or "undated")
                by_day.setdefault(day, []).append(m)
            if not by_day:                       # empty conversation — still list it once
                by_day = {str(data.get("updated_at") or "")[:10] or "undated": []}
            for day, dmsgs in by_day.items():
                last_ts = (dmsgs[-1].get("timestamp") if dmsgs else "") or data.get("updated_at", "")
                _parts = str(sid).split(":")
                out.append({
                    "key": f"{key}#{day}",                       # drill-down handle (day-scoped)
                    "session_id": sid,
                    "app": _parts[2] if len(_parts) >= 3 else "",  # app code (per-app id), "" = legacy/unscoped
                    "day": day,
                    "archived": archived,                        # True → immutable, cleared convo
                    "category": _category({"messages": dmsgs, "metadata": data.get("metadata")}),
                    "message_count": len(dmsgs),
                    "created_at": (dmsgs[0].get("timestamp") if dmsgs else "") or data.get("created_at", ""),
                    "updated_at": last_ts,
                    "user_id": data.get("user_id"),
                    "last_question": next(
                        (m.get("content") for m in reversed(dmsgs) if m.get("role") == "user"), None),
                })
        return sorted(out, key=lambda s: s.get("updated_at") or "", reverse=True)

    def get_history_detail(self, key: str) -> dict | None:
        """One preserved conversation's record (messages), by its `history:*` key. A `#<day>` suffix
        (from the per-day list) filters the messages to that single calendar day."""
        if not key:
            return None
        base, _, day = key.partition("#")
        if not base.startswith(_HISTORY_PREFIX):
            return None
        data = redis_get_json(base)
        if not isinstance(data, dict):
            return None
        if day:
            msgs = [m for m in (data.get("messages") or [])
                    if isinstance(m, dict) and str(m.get("timestamp") or "")[:10] == day]
            data = {**data, "messages": msgs}
        return data

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
