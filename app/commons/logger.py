"""
commons/logger.py
─────────────────
Structured JSON logging with async context propagation (request-ID tracing).
Mirrors delivery/app/commons/logger.py pattern.
"""
from __future__ import annotations

import collections
import logging
import sys
import time as _time
import uuid
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger

from ..commons.config import cfg

# ── Context variables (propagated across async tasks) ─────────────────────────
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
_session_id_ctx: ContextVar[str] = ContextVar("session_id", default="")
_user_id_ctx: ContextVar[str] = ContextVar("user_id", default="")
_trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")


def set_request_context(
    request_id: str | None = None,
    session_id: str = "",
    user_id: str = "",
    trace_id: str = "",
) -> str:
    rid = request_id or str(uuid.uuid4())
    _request_id_ctx.set(rid)
    _session_id_ctx.set(session_id)
    _user_id_ctx.set(user_id)
    _trace_id_ctx.set(trace_id)
    return rid


def get_trace_id() -> str:
    return _trace_id_ctx.get()


def get_request_id() -> str:
    return _request_id_ctx.get()


def get_session_id() -> str:
    return _session_id_ctx.get()


def get_user_id() -> str:
    return _user_id_ctx.get()


# ── Custom formatter ───────────────────────────────────────────────────────────

class _ContextFormatter(jsonlogger.JsonFormatter):
    """Injects request/session/user IDs from context vars into every log record."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["app"] = cfg.APP_NAME
        log_record["env"] = cfg.ENV
        log_record["level"] = record.levelname

        rid = _request_id_ctx.get()
        if rid:
            log_record["request_id"] = rid
        sid = _session_id_ctx.get()
        if sid:
            log_record["session_id"] = sid
        uid = _user_id_ctx.get()
        if uid:
            log_record["user_id"] = uid
        tid = _trace_id_ctx.get()
        if tid:
            log_record["trace_id"] = tid


# ── In-memory ring buffer (recent logs for the /admin Logs view) ─────────────────
_LOG_RING: collections.deque = collections.deque(maxlen=800)
# Standard LogRecord attributes to skip when capturing the structured 'extra' fields.
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"asctime", "message", "taskName"}


def _jsonable(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, list, dict)) or v is None


class _RingHandler(logging.Handler):
    """Keeps the last N log records in memory so the /admin console can show recent
    turns (turn_summary / chat_prompt / turn_step) + errors without shell access."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = {
                k: v for k, v in record.__dict__.items()
                if k not in _STD_ATTRS and not k.startswith("_") and _jsonable(v)
            }
            rid = _request_id_ctx.get()
            if rid and "request_id" not in fields:
                fields["request_id"] = rid
            _LOG_RING.append({
                "ts": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name.rsplit(".", 1)[-1],
                "msg": record.getMessage(),
                "fields": fields,
            })
        except Exception:  # noqa: BLE001 — logging must never raise
            pass


def recent_logs(limit: int = 150, event: str | None = None, level: str | None = None) -> list[dict]:
    """Most-recent captured logs, newest first. Optional filter by structured event
    (turn_summary/chat_prompt/turn_step/…) or level."""
    items = list(_LOG_RING)
    if event:
        items = [r for r in items if r["fields"].get("event") == event or r["fields"].get("step") == event]
    if level:
        lv = level.upper()
        items = [r for r in items if r["level"] == lv]
    return list(reversed(items[-max(1, limit):]))


# ── Logger factory ─────────────────────────────────────────────────────────────

def _build_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    fmt = _ContextFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(fmt)
    return handler


def _configure_root() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # Already configured
    root.setLevel(getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO))
    root.addHandler(_build_handler())
    root.addHandler(_RingHandler())  # capture recent logs for the /admin Logs view

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_root()


class _BoundLogger:
    """Logger wrapper that carries static tags/func context."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._extra: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> "_BoundLogger":
        clone = _BoundLogger.__new__(_BoundLogger)
        clone._logger = self._logger
        clone._extra = {**self._extra, **kwargs}
        return clone

    def _log(self, level: int, msg: str, **kw: Any) -> None:
        extra = {**self._extra, **kw}
        self._logger.log(level, msg, extra=extra, stacklevel=3)

    def debug(self, msg: str, **kw: Any) -> None:
        self._log(logging.DEBUG, msg, **kw)

    def info(self, msg: str, **kw: Any) -> None:
        self._log(logging.INFO, msg, **kw)

    def warning(self, msg: str, **kw: Any) -> None:
        self._log(logging.WARNING, msg, **kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._log(logging.ERROR, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        self._logger.exception(msg, extra={**self._extra, **kw}, stacklevel=2)


def get_logger(name: str) -> _BoundLogger:
    return _BoundLogger(name)
