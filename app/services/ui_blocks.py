"""
services/ui_blocks.py
─────────────────────
Turn tool results into typed, render-ready UIBlocks (cards / tables / lists).

Rationale: for a data lookup the LLM shouldn't re-type the tool's JSON as prose — that
is slow (most of the turn latency is generation) and can hallucinate. Instead we render
the tool's actual result deterministically. The LLM only writes a short header sentence.

Accepts whatever tool_registry produced (dict, list, or a JSON string envelope) and is
defensive: anything it can't structure degrades to a text block, never raises.
"""
from __future__ import annotations

import json
from typing import Any

from ..models.chat import UIBlock

_MAX_ROWS = 50          # cap table rows so a huge list can't bloat the reply
_MAX_COLS = 6           # cap table columns (keep the widget readable)
_MAX_FIELD_VAL = 400    # truncate a single scalar value (fields card)
_MAX_CELL_VAL = 60      # truncate a table cell (keeps rows compact)


def _coerce(value: Any) -> Any:
    """A tool output may be a JSON string (MCP text envelope). Parse it if so."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                return value
    return value


_ENVELOPE_SIBLINGS = {"success", "status", "statuscode", "message", "errors", "error", "code", "timestamp"}


def _unwrap(data: Any) -> Any:
    """Peel common wrappers so we card the payload, not the envelope:
    - the GC standard {success, message, statusCode, data, errors} → its `data`
    - any single-key {k: <dict|list>} wrapper."""
    for _ in range(3):
        if isinstance(data, dict):
            # GC envelope: a `data` payload surrounded by status/meta siblings.
            if "data" in data and isinstance(data["data"], (dict, list)) and \
                    all(k.lower() in _ENVELOPE_SIBLINGS or k == "data" for k in data):
                data = data["data"]
                continue
            if len(data) == 1:
                only = next(iter(data.values()))
                if isinstance(only, (dict, list)):
                    data = only
                    continue
        break
    return data


# Fields whose auto-Title-Case reads poorly (id-suffixed fields carrying name values in
# skill flows). Kept small and generic so it improves confirm/ask/table labels everywhere.
_LABEL_OVERRIDES = {
    "userId": "User",
    "applicationId": "Application",
    "applicationRoleId": "Role",
}


def _humanize(key: str) -> str:
    """firstName / user_profile / getUserProfile_get → readable Title Case."""
    if not key:
        return ""
    if key in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[key]
    k = key
    for suffix in ("_get", "_post", "_put", "_delete"):
        if k.endswith(suffix):
            k = k[: -len(suffix)]
    out: list[str] = []
    prev_lower = False
    for ch in k.replace("_", " ").replace("-", " "):
        if ch.isupper() and prev_lower:
            out.append(" ")
        out.append(ch)
        prev_lower = ch.islower()
    words = " ".join("".join(out).split())
    return words[:1].upper() + words[1:] if words else key


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        try:
            s = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(value)
    else:
        s = str(value)
    s = s.strip()
    return s if len(s) <= _MAX_FIELD_VAL else s[: _MAX_FIELD_VAL - 1] + "…"


def _fields_block(name: str, data: dict) -> UIBlock:
    items = [
        {"label": _humanize(k), "value": _fmt(v)}
        for k, v in data.items()
        if _scalar(v) or isinstance(v, (dict, list))
    ]
    return UIBlock(type="fields", title=_humanize(name), items=items, source_tool=name)


def _cell(value: Any) -> str:
    s = _fmt(value)
    return s if len(s) <= _MAX_CELL_VAL else s[: _MAX_CELL_VAL - 1] + "…"


def _is_id_col(k: str) -> bool:
    kl = k.lower()
    return kl == "id" or kl.endswith("id")


_NOISE_COL_HINTS = ("icon", "url", "image", "img", "href", "logo", "avatar", "attribute",
                    "rnum", "rownum", "rowid")


def _is_noise_col(k: str) -> bool:
    kl = k.lower()
    return any(h in kl for h in _NOISE_COL_HINTS)


def _table_block(name: str, rows_in: list[dict]) -> UIBlock:
    sample = rows_in[:_MAX_ROWS]
    all_keys: list[str] = []
    for row in sample:
        for k in row.keys():
            if k not in all_keys:
                all_keys.append(k)
    # Drop url/icon/rownum noise columns (never useful in a chat table).
    keys = [k for k in all_keys if not _is_noise_col(k)] or all_keys
    # Drop constant columns — a value repeated in every row carries no information in a
    # table (e.g. the user's own name when the query is scoped to them). Keep them only
    # if dropping would leave fewer than 2 columns.
    if len(sample) >= 2:
        varying = [k for k in keys if len({_cell(r.get(k)) for r in sample}) > 1]
        if len(varying) >= 2:
            keys = varying
    # Prefer human-meaningful columns; push id-like ones to the end.
    ordered = [k for k in keys if not _is_id_col(k)] + [k for k in keys if _is_id_col(k)]
    cols = ordered[:_MAX_COLS]
    rows = [[_cell(row.get(c)) for c in cols] for row in sample]
    block = UIBlock(
        type="table",
        title=_humanize(name),
        columns=[_humanize(c) for c in cols],
        rows=rows,
        source_tool=name,
    )
    if len(rows_in) > _MAX_ROWS:
        block.text = f"Showing {_MAX_ROWS} of {len(rows_in)} rows."
    return block


def _is_falsey(v: Any) -> bool:
    return v in (False, None, "", "false", "False", 0, "0")


def _envelope_error(data: Any) -> str | None:
    """If a GC {success,message,statusCode,errors} envelope signals a business error,
    return a human message; else None. A tool can return HTTP 200 but success=false."""
    if not isinstance(data, dict) or not any(k.lower() in _ENVELOPE_SIBLINGS for k in data):
        return None
    sc = data.get("statusCode", data.get("status", data.get("code")))
    try:
        sc_int = int(str(sc)) if sc is not None else None
    except (ValueError, TypeError):
        sc_int = None
    errors = data.get("errors") or data.get("error")
    has_errors = bool(errors) and errors not in ("", [], {})
    failed = ("success" in data and _is_falsey(data.get("success"))) \
        or (sc_int is not None and sc_int >= 400) or has_errors
    if not failed:
        return None
    msg = data.get("message") or "Request failed"
    return f"{msg} — {_fmt(errors)}" if has_errors else str(msg)


def _block_for(name: str, raw: Any, success: bool) -> UIBlock:
    if not success:
        return UIBlock(type="notice", level="error", title=_humanize(name),
                       text=_fmt(raw), source_tool=name)
    coerced = _coerce(raw)
    err = _envelope_error(coerced)
    if err:
        return UIBlock(type="notice", level="error", title=_humanize(name),
                       text=err, source_tool=name)
    # A successful GC envelope with no (or empty) payload → show its message as a note,
    # not a card of {success, statusCode, message} meta.
    if isinstance(coerced, dict) and any(k.lower() in _ENVELOPE_SIBLINGS for k in coerced):
        if coerced.get("data") in (None, "", [], {}):
            return UIBlock(type="notice", level="info", title=_humanize(name),
                           text=str(coerced.get("message") or "No results found."), source_tool=name)
    data = _unwrap(coerced)
    if isinstance(data, dict) and data:
        return _fields_block(name, data)
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return _table_block(name, data)
    if isinstance(data, list) and data:
        return UIBlock(type="list", title=_humanize(name),
                       items=[_fmt(x) for x in data[:_MAX_ROWS]], source_tool=name)
    return UIBlock(type="text", text=_fmt(data), source_tool=name)


def blocks_from_outputs(outputs: list[tuple[str, Any, bool]]) -> list[UIBlock]:
    """outputs: [(tool_name, raw_output, success), …] captured during the turn."""
    blocks: list[UIBlock] = []
    for name, raw, success in outputs:
        try:
            blocks.append(_block_for(name, raw, success))
        except Exception:  # noqa: BLE001 — a bad tool payload must never break the reply
            blocks.append(UIBlock(type="text", text=_fmt(raw), source_tool=name))
    return blocks


def lead_in(blocks: list[UIBlock]) -> str:
    """A one-line header to show above rendered blocks when the synthesis LLM call is
    skipped (the data is IN the blocks, so the text must not re-list it)."""
    if not blocks:
        return ""
    b = blocks[0]
    if b.type == "notice":                 # a notice already carries the full message
        return b.text or ""
    if b.type == "table":
        n = len(b.rows or [])
        return f"Here {'is' if n == 1 else 'are'} {n} result{'' if n == 1 else 's'}:"
    if b.type == "list":
        n = len(b.items or [])
        return f"Here {'is' if n == 1 else 'are'} {n} item{'' if n == 1 else 's'}:"
    if b.type == "fields":
        return "Here are the details:"
    return "Here's what I found:"


def blocks_to_text(blocks: list[UIBlock]) -> str:
    """Flatten blocks to a plain-text fallback for `assistant_message` (clients that
    don't render blocks still get readable content)."""
    parts: list[str] = []
    for b in blocks:
        if b.title:
            parts.append(b.title)
        if b.type == "fields" and b.items:
            parts += [f"- {it.get('label')}: {it.get('value')}" for it in b.items]
        elif b.type == "list" and b.items:
            parts += [f"- {it}" for it in b.items]
        elif b.type == "table" and b.rows is not None:
            parts.append(" | ".join(b.columns or []))
            parts += [" | ".join(str(c) for c in row) for row in b.rows]
        elif b.text:
            parts.append(b.text)
    return "\n".join(parts).strip()
