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
_MAX_COLS = 8           # cap table columns
_MAX_FIELD_VAL = 400    # truncate a single scalar value


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


def _humanize(key: str) -> str:
    """firstName / user_profile / getUserProfile_get → readable Title Case."""
    if not key:
        return ""
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


def _table_block(name: str, rows_in: list[dict]) -> UIBlock:
    cols: list[str] = []
    for row in rows_in[:_MAX_ROWS]:
        for k in row.keys():
            if k not in cols:
                cols.append(k)
            if len(cols) >= _MAX_COLS:
                break
    rows = [[_fmt(row.get(c)) for c in cols] for row in rows_in[:_MAX_ROWS]]
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


def _block_for(name: str, raw: Any, success: bool) -> UIBlock:
    if not success:
        return UIBlock(type="notice", level="error", title=_humanize(name),
                       text=_fmt(raw), source_tool=name)
    data = _unwrap(_coerce(raw))
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
