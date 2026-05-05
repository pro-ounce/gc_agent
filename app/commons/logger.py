"""
commons/logger.py
─────────────────
Structured JSON logging with async context propagation (request-ID tracing).
Mirrors delivery/app/commons/logger.py pattern.
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger

from app.commons.config import cfg

# ── Context variables (propagated across async tasks) ─────────────────────────
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
_session_id_ctx: ContextVar[str] = ContextVar("session_id", default="")
_user_id_ctx: ContextVar[str] = ContextVar("user_id", default="")


def set_request_context(
    request_id: str | None = None,
    session_id: str = "",
    user_id: str = "",
) -> str:
    rid = request_id or str(uuid.uuid4())
    _request_id_ctx.set(rid)
    _session_id_ctx.set(session_id)
    _user_id_ctx.set(user_id)
    return rid


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

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "anthropic", "uvicorn.access"):
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
