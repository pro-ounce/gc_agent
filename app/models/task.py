"""
models/task.py
──────────────
A background/async task — a long-running action the user asked for ("generate a report")
that runs after the request returns and is polled for completion. Persisted in the store
(OpenSearch) so it survives restarts and is covered by the daily snapshot.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Task(BaseModel):
    id: str = Field(default_factory=lambda: f"tsk_{uuid.uuid4().hex[:12]}")
    user_id: str = ""
    session_id: str = ""
    type: str = ""                                   # skill name
    title: str = ""
    status: str = "queued"                           # queued|running|succeeded|failed|interrupted
    progress: dict[str, Any] = Field(default_factory=dict)   # {step, pct}
    request: dict[str, Any] = Field(default_factory=dict)    # collected inputs (no secrets)
    result: list[dict[str, Any]] = Field(default_factory=list)  # UIBlocks as dicts
    error: str | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    @property
    def terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "interrupted")
