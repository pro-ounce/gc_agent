"""
services/backup_service.py
──────────────────────────
On-demand OpenSearch snapshots — the "take a snapshot now" action (e.g. before a major
push), complementing the scheduled SM policy. Both write into the same registered repo
(OPENSEARCH_SNAPSHOT_REPO); this just triggers one immediately and lists recent ones.

Thin wrapper over the OpenSearch snapshot API via the store's raw client — no new deps.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from ..commons.config import cfg
from ..commons.logger import get_logger
from ..connections import os_client

log = get_logger(__name__)


class BackupError(RuntimeError):
    pass


def _client() -> Any:
    c = os_client()
    if c is None:
        raise BackupError("snapshots require the OpenSearch store backend (STORE_BACKEND=opensearch)")
    return c


def _snapshot_name(label: str | None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")[:40]
    return f"manual-{slug}-{ts}" if slug else f"manual-{ts}"


def create_snapshot(label: str | None = None, wait: bool = False) -> dict[str, Any]:
    """Trigger a snapshot into the configured repo. Non-blocking by default (returns as
    IN_PROGRESS); pass wait=True to block until it finishes."""
    c = _client()
    repo = cfg.OPENSEARCH_SNAPSHOT_REPO
    indices = cfg.OPENSEARCH_SNAPSHOT_INDICES
    name = _snapshot_name(label)
    body = {
        "indices": indices,
        "ignore_unavailable": True,
        "include_global_state": False,
        "metadata": {"trigger": "manual", "label": label or "", "taken_by": "gc-agent"},
    }
    try:
        c.snapshot.create(repository=repo, snapshot=name, body=body, wait_for_completion=wait)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "repository_missing" in msg or "404" in msg:
            raise BackupError(
                f"snapshot repository '{repo}' is not registered — register it first "
                f"(PUT /_snapshot/{repo})."
            ) from exc
        raise BackupError(f"snapshot failed: {msg}") from exc
    log.bind(func="create_snapshot", repo=repo, snapshot=name).info(
        f"manual snapshot triggered: {name} (repo={repo}, indices={indices})"
    )
    return {"snapshot": name, "repository": repo, "indices": indices,
            "state": "SUCCESS" if wait else "IN_PROGRESS"}


def list_snapshots(limit: int = 15) -> dict[str, Any]:
    """Recent snapshots in the configured repo, newest first."""
    repo = cfg.OPENSEARCH_SNAPSHOT_REPO
    result = {"repository": repo, "indices": cfg.OPENSEARCH_SNAPSHOT_INDICES, "snapshots": []}
    try:
        c = _client()
    except BackupError as exc:
        result["error"] = str(exc)
        return result
    try:
        resp = c.snapshot.get(repository=repo, snapshot="_all", ignore=[404])
        snaps = resp.get("snapshots", []) if isinstance(resp, dict) else []
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"repo '{repo}' not reachable/registered: {exc}"
        return result
    snaps = sorted(snaps, key=lambda s: s.get("start_time_in_millis", 0), reverse=True)[:limit]
    result["snapshots"] = [
        {
            "snapshot": s.get("snapshot"),
            "state": s.get("state"),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
            "duration_ms": s.get("duration_in_millis"),
            "indices": s.get("indices") or [],
            "trigger": (s.get("metadata") or {}).get("trigger", "scheduled"),
            "label": (s.get("metadata") or {}).get("label", ""),
        }
        for s in snaps
    ]
    return result


# ── low-level plugin/cluster calls (SM + repo APIs aren't in the py client namespace) ──
def _perform(method: str, path: str, body: Any = None) -> Any:
    c = _client()
    try:
        return c.transport.perform_request(method, path, body=body)
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"{method} {path} failed: {exc}") from exc


def _parse_days(max_age: Any) -> int | None:
    if not max_age:
        return None
    m = re.match(r"(\d+)\s*d", str(max_age))
    return int(m.group(1)) if m else None


def verify_repo() -> bool:
    try:
        _perform("POST", f"/_snapshot/{cfg.OPENSEARCH_SNAPSHOT_REPO}/_verify")
        return True
    except BackupError:
        return False


def get_schedule() -> dict[str, Any]:
    """The managed SM policy — its cron, retention, enabled state, and last/next run.
    Never raises; returns exists=False if the policy isn't set up yet."""
    name = cfg.OPENSEARCH_SM_POLICY
    out: dict[str, Any] = {"name": name, "exists": False, "enabled": None, "cron": None,
                           "timezone": cfg.OPENSEARCH_SM_TIMEZONE, "retention_days": None,
                           "last_execution": None, "next_execution": None}
    try:
        pol = _perform("GET", f"/_plugins/_sm/policies/{name}")
    except BackupError:
        return out
    sm = pol.get("sm_policy") or {}
    out["exists"] = True
    out["enabled"] = sm.get("enabled")
    cron = ((sm.get("creation") or {}).get("schedule") or {}).get("cron") or {}
    out["cron"] = cron.get("expression")
    out["timezone"] = cron.get("timezone") or out["timezone"]
    out["retention_days"] = _parse_days(((sm.get("deletion") or {}).get("condition") or {}).get("max_age"))
    try:
        ex = _perform("GET", f"/_plugins/_sm/policies/{name}/_explain")
        pols = ex.get("policies") or []
        if pols:
            cr = (pols[0].get("creation") or {})
            le = cr.get("latest_execution") or {}
            out["last_execution"] = {"status": le.get("status"),
                                     "time": le.get("start_time") or le.get("end_time"),
                                     "message": (le.get("info") or {}).get("message")}
            out["next_execution"] = (cr.get("trigger") or {}).get("time")
    except BackupError:
        pass
    return out


def set_schedule(cron: str, retention_days: int, enabled: bool = True) -> dict[str, Any]:
    """Create or update the managed SM policy (daily-style schedule + retention)."""
    name = cfg.OPENSEARCH_SM_POLICY
    body = {
        "description": "Managed by GC Agent admin",
        "creation": {
            "schedule": {"cron": {"expression": cron, "timezone": cfg.OPENSEARCH_SM_TIMEZONE}},
            "time_limit": "1h",
        },
        "deletion": {
            "condition": {"max_age": f"{int(retention_days)}d", "min_count": 5, "max_count": 500},
        },
        "snapshot_config": {
            "repository": cfg.OPENSEARCH_SNAPSHOT_REPO,
            "indices": cfg.OPENSEARCH_SNAPSHOT_INDICES,
            "ignore_unavailable": True,
            "include_global_state": False,
            "date_format": "yyyy-MM-dd-HH-mm",
        },
    }
    existing = None
    try:
        existing = _perform("GET", f"/_plugins/_sm/policies/{name}")
    except BackupError:
        existing = None
    if existing:
        seq, pt = existing.get("_seq_no"), existing.get("_primary_term")
        _perform("PUT", f"/_plugins/_sm/policies/{name}?if_seq_no={seq}&if_primary_term={pt}", body)
    else:
        _perform("POST", f"/_plugins/_sm/policies/{name}", body)
    if enabled:
        try:
            _perform("POST", f"/_plugins/_sm/policies/{name}/_start")
        except BackupError:
            pass
    else:
        toggle_schedule(False)
    log.bind(func="set_schedule").info(f"SM policy '{name}' set: cron={cron!r} retention={retention_days}d enabled={enabled}")
    return get_schedule()


def toggle_schedule(enabled: bool) -> dict[str, Any]:
    name = cfg.OPENSEARCH_SM_POLICY
    _perform("POST", f"/_plugins/_sm/policies/{name}/_{'start' if enabled else 'stop'}")
    return get_schedule()


def _stats(snaps: list[dict]) -> dict[str, Any]:
    by_state: dict[str, int] = {}
    last_success = None
    for s in snaps:
        st = s.get("state") or "?"
        by_state[st] = by_state.get(st, 0) + 1
        if st == "SUCCESS" and last_success is None:
            last_success = s.get("end_time") or s.get("start_time")
    return {"total": len(snaps), "by_state": by_state, "last_success": last_success}


def backup_health() -> dict[str, Any]:
    """Backup freshness for /actuator/health. Returns a component dict:
      - UP        : a SUCCESS snapshot exists within OPENSEARCH_SNAPSHOT_MAX_AGE_HOURS
      - DEGRADED  : repo registered but newest SUCCESS is stale / none exists
      - UNKNOWN   : backups not set up (no OS store, or repo not registered) → not an alarm
      - DISABLED  : the freshness check is turned off (max_age_hours = 0)
    Never raises."""
    max_age_h = cfg.OPENSEARCH_SNAPSHOT_MAX_AGE_HOURS
    if max_age_h <= 0:
        return {"status": "DISABLED"}
    repo = cfg.OPENSEARCH_SNAPSHOT_REPO
    try:
        c = _client()
    except BackupError:
        return {"status": "UNKNOWN", "reason": "store is not OpenSearch"}
    try:
        resp = c.snapshot.get(repository=repo, snapshot="_all", ignore=[404])
        snaps = resp.get("snapshots", []) if isinstance(resp, dict) else []
    except Exception:  # noqa: BLE001
        return {"status": "UNKNOWN", "repository": repo, "reason": "repository not registered"}
    successes = [s for s in snaps if s.get("state") == "SUCCESS"]
    if not successes:
        return {"status": "DEGRADED", "repository": repo, "reason": "no successful snapshot yet"}
    newest = max(successes, key=lambda s: s.get("end_time_in_millis") or s.get("start_time_in_millis") or 0)
    end_ms = newest.get("end_time_in_millis") or newest.get("start_time_in_millis") or 0
    age_h = round((time.time() * 1000 - end_ms) / 3_600_000, 1)
    fresh = age_h <= max_age_h
    return {
        "status": "UP" if fresh else "DEGRADED",
        "repository": repo,
        "latest": newest.get("snapshot"),
        "age_hours": age_h,
        "max_age_hours": max_age_h,
        **({} if fresh else {"reason": f"newest snapshot is {age_h}h old (> {max_age_h}h)"}),
    }


def overview() -> dict[str, Any]:
    """Everything the /admin Backups tab needs in one call — repo, schedule, snapshots, stats."""
    snaps = list_snapshots(limit=15)
    return {
        "repository": {"name": cfg.OPENSEARCH_SNAPSHOT_REPO, "verified": verify_repo()},
        "indices": cfg.OPENSEARCH_SNAPSHOT_INDICES,
        "schedule": get_schedule(),
        "snapshots": snaps.get("snapshots", []),
        "error": snaps.get("error"),
        "stats": _stats(snaps.get("snapshots", [])),
    }
