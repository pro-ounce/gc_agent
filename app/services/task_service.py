"""
services/task_service.py
────────────────────────
CRUD for background Tasks on the durable store (OpenSearch `agent-kv`, `task:` keys —
same layer as sessions, so tasks are snapshotted by the daily backup).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..commons.logger import get_logger
from ..connections import redis_client, redis_get_json, redis_set_json
from ..models.task import Task

log = get_logger(__name__)

_PREFIX = "task:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(task: Task) -> Task:
    redis_set_json(_PREFIX + task.id, task.model_dump())
    log.bind(func="task_create", task=task.id, type=task.type).info(f"task {task.id} created ({task.type})")
    return task


def get(task_id: str) -> Task | None:
    d = redis_get_json(_PREFIX + task_id)
    return Task(**d) if d else None


def update(task_id: str, **fields: Any) -> Task | None:
    d = redis_get_json(_PREFIX + task_id)
    if not d:
        return None
    d.update(fields)
    d["updated_at"] = _now()
    redis_set_json(_PREFIX + task_id, d)
    return Task(**d)


def list_by_user(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Most-recent tasks for a user (summary dicts, newest first)."""
    out: list[dict[str, Any]] = []
    for raw in redis_client.keys(f"{_PREFIX}*"):
        key = raw.decode() if isinstance(raw, bytes) else raw
        d = redis_get_json(key)
        if not d or (user_id and d.get("user_id") != user_id):
            continue
        out.append({
            "id": d.get("id"), "type": d.get("type"), "title": d.get("title"),
            "status": d.get("status"), "progress": d.get("progress"),
            "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
        })
    out.sort(key=lambda t: str(t.get("updated_at") or ""), reverse=True)
    return out[:limit]


def reconcile_interrupted() -> int:
    """On startup, mark tasks stuck in queued/running as interrupted (their in-process
    runner died with the previous process). Returns how many were reconciled."""
    n = 0
    for raw in redis_client.keys(f"{_PREFIX}*"):
        key = raw.decode() if isinstance(raw, bytes) else raw
        d = redis_get_json(key)
        if d and d.get("status") in ("queued", "running"):
            d["status"] = "interrupted"
            d["error"] = "Interrupted by a service restart — please re-run."
            d["updated_at"] = _now()
            redis_set_json(key, d)
            n += 1
    if n:
        log.bind(func="reconcile").info(f"marked {n} interrupted task(s) on startup")
    return n
