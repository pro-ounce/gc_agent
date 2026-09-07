"""
services/balloon_store.py
─────────────────────────
Admin-editable, persisted per-application balloon overrides. The widget's balloons are
normally generated from the ecosystem catalogue + curated workflow entry points (agent
intelligence); this store lets an admin PIN a specific headline set for an application from
the admin section. Store overrides take the top slot; the agent-derived balloons are still
appended, so it stays "driven from the store AND the agent" (the user's ask).

Persistence mirrors services/skill_store.py:
  • OpenSearch KV (redis-compatible) — survives restarts + git deploys.
  • A flat JSON file (gitignored) — human-inspectable, hand-editable, wins over KV.
Kept forgiving: any backend hiccup degrades to "no override", never raises.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from ..commons.logger import get_logger
from ..connections import redis_client, redis_get_json, redis_set_json, redis_delete

log = get_logger(__name__)

_KV_PREFIX = "gcballoon:"
_FILE = os.environ.get("BALLOON_OVERRIDES_FILE", "balloon_overrides.json")
_MAX = 8  # cap balloons per application

# in-memory cache of {CODE: [{label, send}]}, refreshed on write / short TTL on read
_cache: dict[str, list[dict[str, str]]] | None = None
_cache_exp: float = 0.0
_TTL = 30.0


def _code(app_code: str) -> str:
    return (app_code or "").strip().upper()


def _clean(balloons: Any) -> list[dict[str, str]]:
    """Coerce arbitrary input into a safe [{label, send}] list."""
    out: list[dict[str, str]] = []
    for b in (balloons if isinstance(balloons, list) else []):
        if not isinstance(b, dict):
            continue
        label = str(b.get("label") or "").strip()[:60]
        send = str(b.get("send") or b.get("message") or label).strip()[:200]
        if label and send:
            out.append({"label": label, "send": send})
        if len(out) >= _MAX:
            break
    return out


def _read_file() -> dict[str, list[dict[str, str]]]:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {_code(k): _clean(v) for k, v in data.items() if _clean(v)}
    except Exception as exc:  # noqa: BLE001 — a bad file must never break the widget
        log.warning(f"balloon overrides file read failed: {exc}")
    return {}


def _write_file(all_ov: dict[str, list[dict[str, str]]]) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(all_ov, f, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"balloon overrides file write failed: {exc}")


def _load() -> dict[str, list[dict[str, str]]]:
    """Merge KV + file (file wins), cached for a short TTL."""
    global _cache, _cache_exp
    if _cache is not None and time.monotonic() < _cache_exp:
        return _cache
    merged: dict[str, list[dict[str, str]]] = {}
    try:
        for raw in (redis_client.keys(f"{_KV_PREFIX}*") or []):
            key = raw.decode() if isinstance(raw, bytes) else raw
            code = key[len(_KV_PREFIX):]
            d = redis_get_json(key)
            cleaned = _clean(d.get("balloons") if isinstance(d, dict) else d)
            if cleaned:
                merged[_code(code)] = cleaned
    except Exception as exc:  # noqa: BLE001
        log.warning(f"balloon overrides KV load failed: {exc}")
    merged.update(_read_file())  # file wins
    _cache = merged
    _cache_exp = time.monotonic() + _TTL
    return merged


def _invalidate() -> None:
    global _cache, _cache_exp
    _cache, _cache_exp = None, 0.0


# ── public API ────────────────────────────────────────────────────────────────
def get_overrides(app_code: str) -> list[dict[str, str]] | None:
    """The admin-pinned balloons for an application, or None if none set."""
    return _load().get(_code(app_code)) or None


def all_overrides() -> dict[str, list[dict[str, str]]]:
    return dict(_load())


def set_overrides(app_code: str, balloons: Any) -> list[dict[str, str]]:
    """Persist (KV + file) the admin-pinned balloons for an application."""
    code = _code(app_code)
    cleaned = _clean(balloons)
    if not code:
        raise ValueError("application code required")
    try:
        redis_set_json(f"{_KV_PREFIX}{code}", {"balloons": cleaned})
    except Exception as exc:  # noqa: BLE001
        log.warning(f"balloon overrides KV save failed: {exc}")
    all_ov = _read_file()
    if cleaned:
        all_ov[code] = cleaned
    else:
        all_ov.pop(code, None)
    _write_file(all_ov)
    _invalidate()
    log.bind(func="set_overrides", app=code, count=len(cleaned)).info("balloon overrides saved")
    return cleaned


def delete_overrides(app_code: str) -> None:
    """Remove the admin override for an application (revert to agent-generated balloons)."""
    code = _code(app_code)
    try:
        redis_delete(f"{_KV_PREFIX}{code}")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"balloon overrides KV delete failed: {exc}")
    all_ov = _read_file()
    if all_ov.pop(code, None) is not None:
        _write_file(all_ov)
    _invalidate()
    log.bind(func="delete_overrides", app=code).info("balloon overrides cleared")
