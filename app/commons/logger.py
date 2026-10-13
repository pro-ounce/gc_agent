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
import re as _re
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
# Client fingerprint for snooping/intrusion detection: browser+OS (User-Agent) and origin.
_user_agent_ctx: ContextVar[str] = ContextVar("user_agent", default="")
_origin_ctx: ContextVar[str] = ContextVar("origin", default="")


def set_request_context(
    request_id: str | None = None,
    session_id: str = "",
    user_id: str = "",
    trace_id: str = "",
    client_ip: str = "",
    user_agent: str = "",
    origin: str = "",
) -> str:
    rid = request_id or str(uuid.uuid4())
    _request_id_ctx.set(rid)
    _session_id_ctx.set(session_id)
    _user_id_ctx.set(user_id)
    _trace_id_ctx.set(trace_id)
    if client_ip:
        _client_ip_ctx.set(client_ip)
    if user_agent:
        _user_agent_ctx.set(user_agent[:400])
    if origin:
        _origin_ctx.set(origin[:200])
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

# ── On-disk redaction ────────────────────────────────────────────────────────────
# NO PII reaches the on-disk sinks — the stdout log and the metrics JSONL are world-readable, and a
# tool result once wrote a user's name + email there. The in-memory ring that powers the
# access-controlled /admin feed keeps the RAW text (and the audit 'who/where'); the files get a
# length placeholder for any content- or identity-bearing field, and every remaining string has
# email / SSN / phone patterns masked (recursively, so nested dicts/lists are covered too).
#
# Field names whose VALUE is user content or directly identifies a person — dropped to a length.
_REDACT_FIELDS = frozenset({
    # free-text content (user-typed or model-generated — may contain anything)
    "question", "answer", "result_preview", "preview", "message_text",
    "user_message", "assistant_message", "system_prompt", "prompt", "content", "text",
    "title", "description", "note", "hint", "reason", "summary",
    # directly identifying
    "user_name", "username", "sso_user_name", "full_name", "first_name", "last_name",
    "middle_name", "client_ip", "user_agent", "origin", "referer",
    # PII-typed values + header dumps that carry IP / UA / referer
    "email", "email_address", "emailaddress", "phone", "phone_num", "fax", "ssn",
    "user_image", "carried_headers",
})
_EMAIL_RE = _re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN_RE = _re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Phone: require separators so bare digit runs (ids, org numbers, token counts, timestamps) are safe.
_PHONE_RE = _re.compile(r"(?<!\d)(?:\+?\d{1,2}[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
# A human-readable log MESSAGE sometimes quotes the user's text (e.g. prompt "<question>"); mask any
# double-quoted span in message strings so embedded content never reaches disk. Structured data uses
# the fields (redacted above), and tool lists render with single quotes, so this is well-targeted.
_QUOTED_RE = _re.compile(r'"[^"]*"')
_MSG_KEYS = ("msg", "message")


def _mask_text(v: str) -> str:
    v = _EMAIL_RE.sub("<email>", v)
    v = _SSN_RE.sub("<ssn>", v)
    return _PHONE_RE.sub("<phone>", v)


def _scrub(key: str | None, v: Any) -> Any:
    """Redact one value for an on-disk sink. `key` (its field name) drops content/identity fields to
    a length; otherwise strings are pattern-masked (and quoted spans in log messages dropped) and
    dicts/lists scrubbed recursively."""
    if key is not None and key.lower() in _REDACT_FIELDS:
        return f"<redacted:{len(v)}>" if isinstance(v, str) else "<redacted>"
    if isinstance(v, str):
        v = _mask_text(v)
        if key is not None and key.lower() in _MSG_KEYS:
            v = _QUOTED_RE.sub('"<redacted>"', v)
        return v
    if isinstance(v, dict):
        return {k: _scrub(k, vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_scrub(None, vv) for vv in v]
    return v


def _redact_for_file(fields: dict) -> dict:
    """A copy of `fields` safe for the on-disk sinks. Never mutates the input (the ring keeps raw)."""
    return {k: _scrub(k, v) for k, v in fields.items()}


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

        # Keep ALL content / PII out of the (world-readable) stdout file: content & identity fields
        # become a length, email/SSN/phone patterns are masked everywhere (recursively). The
        # in-memory /admin ring is built separately and keeps the raw text.
        for _k in list(log_record.keys()):
            log_record[_k] = _scrub(_k, log_record[_k])


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
_METRICS_MAXLINES = 8000      # rotate the live file when it grows past ~2x this
# DURABILITY: the live file is ROTATED (never trim-dropped) — kept as a bounded local cache of the
# last N rotations. The full, "very-low-loss" copy is the durable OpenSearch audit index, which is
# snapshotted/backed up. Each metric-relevant record is queued and drained into it OFF the log
# hot-path (a slow/down OpenSearch never adds latency; retried next cycle).
_METRICS_ROTATIONS = int(_os.environ.get("METRICS_LOG_ROTATIONS", "12") or 12)
_AUDIT_INDEX = _os.environ.get("OPENSEARCH_AUDIT_INDEX", "agent-audit")
_AUDIT_Q: "collections.deque" = collections.deque(maxlen=20000)   # non-blocking overflow buffer
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
        if _persist_n % 500 == 0:               # occasional rotation to keep the live file bounded
            _trim_metrics_file()
    except Exception:  # noqa: BLE001
        pass
    # Queue for the durable OpenSearch audit sink (non-blocking; drained by flush_audit_sink()).
    try:
        _AUDIT_Q.append(rec)
    except Exception:  # noqa: BLE001
        pass


def _trim_metrics_file() -> None:
    """ROTATE (never drop): when the live file exceeds 2x the target, roll it to a timestamped
    archive and start fresh, keeping the last _METRICS_ROTATIONS archives (local durable cache).
    The OpenSearch audit index holds the full, snapshotted trail."""
    try:
        if not _os.path.exists(_METRICS_FILE):
            return
        with open(_METRICS_FILE, encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
        if line_count <= 2 * _METRICS_MAXLINES:
            return
        import time as _time
        import glob as _glob
        _os.replace(_METRICS_FILE, f"{_METRICS_FILE}.{int(_time.time())}")   # rotate, don't drop
        rots = sorted(_glob.glob(f"{_glob.escape(_METRICS_FILE)}.*"))
        for old in rots[:-_METRICS_ROTATIONS] if len(rots) > _METRICS_ROTATIONS else []:
            try:
                _os.remove(old)                  # bounded local cache; OpenSearch keeps everything
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


async def flush_audit_sink(interval: float = 5.0) -> None:
    """Background worker: drain the audit queue into the durable OpenSearch audit index in batches,
    OFF the logging hot-path. Best-effort — if the sink is unavailable the records stay in the local
    rotating file, and the next cycle retries. Runs for the life of the process."""
    import asyncio
    while True:
        try:
            await asyncio.sleep(interval)
            if not _AUDIT_Q:
                continue
            try:
                from ..connections import os_client
                client = os_client()
            except Exception:  # noqa: BLE001
                client = None
            if client is None:                   # memory/redis backend → local file is the copy
                _AUDIT_Q.clear()
                continue
            batch = []
            while _AUDIT_Q and len(batch) < 500:
                batch.append(_AUDIT_Q.popleft())
            if not batch:
                continue
            body: list = []
            for rec in batch:
                body.append({"index": {"_index": _AUDIT_INDEX}})   # auto-id → indexed once
                body.append(rec)
            try:
                client.bulk(body=body, request_timeout=10)
            except Exception:  # noqa: BLE001 — keep the local copy; don't re-queue (avoid growth)
                pass
        except Exception:  # noqa: BLE001 — the worker must never die
            try:
                import asyncio as _a
                await _a.sleep(interval)
            except Exception:  # noqa: BLE001
                pass


def _rehydrate_from_opensearch(limit: int) -> int:
    """Fallback rehydrate: pull the newest `limit` audit records from the durable OpenSearch index
    (used when the local file is missing — e.g. a fresh box after disk loss)."""
    try:
        from ..connections import os_client
        client = os_client()
        if client is None:
            return 0
        res = client.search(index=_AUDIT_INDEX, body={
            "size": limit, "query": {"match_all": {}},
            "sort": [{"ts.keyword": {"order": "desc", "unmapped_type": "keyword"}}]},
            request_timeout=10)
        hits = [h.get("_source") for h in (res.get("hits", {}).get("hits") or [])]
        n = 0
        for rec in reversed(hits):               # oldest-first into the ring
            if isinstance(rec, dict) and "fields" in rec:
                _LOG_RING.append(rec)
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0


def load_persisted_metrics(limit: int = 800) -> int:
    """Rehydrate the ring on startup so the admin dashboards show history after a restart. Prefers
    the local rotating file; falls back to the durable OpenSearch audit index if the file is gone."""
    lines: list = []
    try:
        if _os.path.exists(_METRICS_FILE):
            with open(_METRICS_FILE, encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
    except Exception:  # noqa: BLE001
        lines = []
    if not lines:
        return _rehydrate_from_opensearch(limit)
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
                             ("trace_id", _trace_id_ctx), ("client_ip", _client_ip_ctx),
                             ("user_agent", _user_agent_ctx), ("origin", _origin_ctx)):
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
            _LOG_RING.append(rec)                            # in-memory /admin feed — raw text
            if _metric_relevant(fields, record.levelname):   # durable → survives restart
                # The JSONL rehydrates the ring after a restart; persist a fully-redacted copy so no
                # chat content / PII lands on disk — scrub BOTH the message string and the fields
                # (the live ring still has the raw text until the next restart).
                _persist_record({
                    **rec,
                    "msg": _scrub("msg", rec.get("msg")),
                    "fields": _redact_for_file(fields),
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
