"""
commons/logger.py
─────────────────
Structured JSON logging with async context propagation (request-ID tracing).
Mirrors delivery/app/commons/logger.py pattern.
"""
from __future__ import annotations

import collections
import json as _json
import logging
import os as _os
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
# Audit context (FedRAMP): who fired the request and from where — stamped on every log record.
_user_name_ctx: ContextVar[str] = ContextVar("user_name", default="")
_client_ip_ctx: ContextVar[str] = ContextVar("client_ip", default="")


def set_request_context(
    request_id: str | None = None,
    session_id: str = "",
    user_id: str = "",
    trace_id: str = "",
    client_ip: str = "",
) -> str:
    rid = request_id or str(uuid.uuid4())
    _request_id_ctx.set(rid)
    _session_id_ctx.set(session_id)
    _user_id_ctx.set(user_id)
    _trace_id_ctx.set(trace_id)
    if client_ip:
        _client_ip_ctx.set(client_ip)
    return rid


def set_identity(user_id: str = "", user_name: str = "") -> None:
    """Record who the authenticated caller is, once the route has resolved them — so every
    subsequent log line (and the persisted audit trail) carries the actual username."""
    if user_id:
        _user_id_ctx.set(str(user_id))
    if user_name:
        _user_name_ctx.set(str(user_name))


def get_user_name() -> str:
    return _user_name_ctx.get()


def get_client_ip() -> str:
    return _client_ip_ctx.get()


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

# ── durable metrics ────────────────────────────────────────────────────────────
# The /admin Metrics·Activity·inference views + workflow timelines reconstruct from this
# ring, which is in-memory — so a restart (every deploy restarts the service) wiped them.
# Persist the METRIC-relevant records (turn prompt/summary/source/answer, per-step, tool
# completions, errors) to a gitignored JSONL and rehydrate the ring on startup, so the
# dashboards survive restarts. Best-effort throughout: persistence never breaks logging.
_METRICS_FILE = _os.environ.get("METRICS_LOG_FILE", "metrics_log.jsonl")
_METRICS_EVENTS = {"chat_prompt", "turn_summary", "turn_source", "chat_answer"}
_METRICS_MAXLINES = 8000      # trim target when the file grows past ~2x this
_persist_n = 0


def _metric_relevant(fields: dict, level: str) -> bool:
    if not fields.get("request_id"):
        return False
    return (fields.get("event") in _METRICS_EVENTS or "step" in fields
            or fields.get("func") == "execute_tool" or level == "ERROR")


def _persist_record(rec: dict) -> None:
    global _persist_n
    try:
        with open(_METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        _persist_n += 1
        if _persist_n % 500 == 0:               # occasional trim to keep the file bounded
            _trim_metrics_file()
    except Exception:  # noqa: BLE001
        pass


def _trim_metrics_file() -> None:
    try:
        with open(_METRICS_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 2 * _METRICS_MAXLINES:
            with open(_METRICS_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-_METRICS_MAXLINES:])
    except Exception:  # noqa: BLE001
        pass


def load_persisted_metrics(limit: int = 800) -> int:
    """Rehydrate the ring from the last `limit` persisted records (oldest-first) so the admin
    dashboards show history immediately after a restart. Called once at startup."""
    try:
        if not _os.path.exists(_METRICS_FILE):
            return 0
        with open(_METRICS_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for ln in lines:
        try:
            rec = _json.loads(ln)
            if isinstance(rec, dict) and "fields" in rec:
                _LOG_RING.append(rec)
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n
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
            # Stamp the full audit identity (who / where / which session) onto every record so
            # the persisted trail answers FedRAMP's who-did-what-when-from-where without gaps.
            for key, ctx in (("request_id", _request_id_ctx), ("session_id", _session_id_ctx),
                             ("user_id", _user_id_ctx), ("user_name", _user_name_ctx),
                             ("trace_id", _trace_id_ctx), ("client_ip", _client_ip_ctx)):
                val = ctx.get()
                if val and key not in fields:
                    fields[key] = val
            rec = {
                "ts": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name.rsplit(".", 1)[-1],
                "msg": record.getMessage(),
                "fields": fields,
            }
            _LOG_RING.append(rec)
            if _metric_relevant(fields, record.levelname):   # durable → survives restart
                _persist_record(rec)
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
