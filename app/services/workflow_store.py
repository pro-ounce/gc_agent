"""
services/workflow_store.py
──────────────────────────
Persistence + live registration for ADMIN-AUTHORED workflows (created/edited on the admin
console's Workflows tab). A workflow is data, not code — the same JSON spec the loader reads
from `services/workflows/*.json` (see workflow.from_dict / to_dict) — so an operator can
author one at runtime and it hot-registers into `workflow.REGISTRY`, exactly like the
`create_skill` flow does for skills.

Layering (mirrors services/skill_store.py):
  • Repo files (`services/workflows/*.json`) load first at import (workflow.load_dir).
  • This store then layers ON TOP: KV (OpenSearch, redis-compatible) + a gitignored flat file
    (`custom_workflows.json`), the file winning over KV. An admin edit to a repo-shipped
    workflow persists here as an override, so it survives the pull-deploy hard-reset that
    would otherwise revert the repo file.

Kept forgiving: any backend hiccup degrades to "no custom workflow", never raises — a broken
store must never take the agent down.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..commons.logger import get_logger
from ..connections import redis_client, redis_get_json, redis_set_json, redis_delete
from . import workflow as _wf

log = get_logger(__name__)

_KV_PREFIX = "gcwf:"
# Flat file, relative to the app working dir (/apps/gc_agent under supervisor). Gitignored so
# the pull-deploy hard-reset never clobbers it. Override with CUSTOM_WORKFLOWS_FILE.
_FILE = os.environ.get("CUSTOM_WORKFLOWS_FILE", "custom_workflows.json")

# Ids that came from this store (admin-authored or admin-overridden) — lets the console tell an
# operator-owned workflow apart from a repo-shipped one. Populated on load/save, cleared on delete.
_custom_ids: set[str] = set()


def custom_ids() -> set[str]:
    """The workflow ids this store owns (admin-authored or -overridden)."""
    return set(_custom_ids)


def _read_file() -> list[dict[str, Any]]:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 — a bad file must never break startup
        log.warning(f"custom workflows file read failed: {exc}")
    return []


def _write_file(items: list[dict[str, Any]]) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"custom workflows file write failed: {exc}")


def load_custom_workflows() -> int:
    """Load persisted admin workflows (KV first, then file overrides) and register them live,
    layering on top of the repo-authored workflows already in REGISTRY. Returns the count."""
    merged: dict[str, dict[str, Any]] = {}
    try:
        for raw in (redis_client.keys(f"{_KV_PREFIX}*") or []):
            key = raw.decode() if isinstance(raw, bytes) else raw
            d = redis_get_json(key)
            if isinstance(d, dict) and d.get("id"):
                merged[d["id"]] = d
    except Exception as exc:  # noqa: BLE001
        log.warning(f"custom workflows KV load failed: {exc}")
    for d in _read_file():                       # file wins over KV (hand-edits authoritative)
        if d.get("id"):
            merged[d["id"]] = d
    n = 0
    for spec in merged.values():
        try:
            _wf.register(_wf.from_dict(spec))     # validates + adds to REGISTRY (replaces repo one)
            _custom_ids.add(spec["id"])
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(f"skip malformed custom workflow {spec.get('id')!r}: {exc}")
    if n:
        log.bind(func="load_custom_workflows", count=n).info(f"loaded {n} custom workflow(s)")
    return n


def validate(spec: dict[str, Any]) -> str:
    """Translate a spec through the loader; return "" if valid, else the error message. Never
    mutates REGISTRY — for live editor feedback before a save."""
    try:
        _wf.from_dict(spec)
        return ""
    except Exception as exc:  # noqa: BLE001
        return str(exc)


def save_workflow(spec: dict[str, Any]) -> _wf.Workflow:
    """Validate, persist to KV + file, and hot-register a workflow. Raises ValueError with the
    loader's message if the spec is invalid (nothing is persisted or registered in that case)."""
    wf = _wf.from_dict(spec)                       # raises on a bad spec — before any persistence
    clean = _wf.to_dict(wf)                        # canonical form (drops empties, normalizes)
    try:
        redis_set_json(f"{_KV_PREFIX}{wf.id}", clean)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"custom workflow KV save failed: {exc}")
    items = [x for x in _read_file() if x.get("id") != wf.id]
    items.append(clean)
    _write_file(items)
    _wf.register(wf)
    _custom_ids.add(wf.id)
    log.bind(func="save_workflow", workflow=wf.id, nodes=len(wf.nodes)).info("custom workflow saved")
    return wf


def delete_workflow(wf_id: str) -> bool:
    """Remove an admin-owned workflow from KV, file, and REGISTRY. A repo-shipped workflow that
    was never overridden here is not ours to delete — returns False and leaves it registered.
    Deleting an override reverts to the repo copy on the next restart (not re-loaded live)."""
    if wf_id not in _custom_ids:
        return False
    try:
        redis_delete(f"{_KV_PREFIX}{wf_id}")
    except Exception:  # noqa: BLE001
        pass
    _write_file([x for x in _read_file() if x.get("id") != wf_id])
    _custom_ids.discard(wf_id)
    _wf.REGISTRY.pop(wf_id, None)                  # drop the live override; repo copy returns on restart
    log.bind(func="delete_workflow", workflow=wf_id).info("custom workflow deleted")
    return True
