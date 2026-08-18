"""
commons/metrics.py
──────────────────
Prometheus metrics for the agent — HTTP, LLM, tools, MCP, chat, store, and
process/runtime. Everything is registered on a private CollectorRegistry so the
module is safe to import repeatedly (reload / tests) without double-registration.

Exposed at GET /metrics (Prometheus text format), scraped alongside the Java
services' /actuator/prometheus.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from prometheus_client import GCCollector, PlatformCollector, ProcessCollector

registry = CollectorRegistry(auto_describe=True)

# process / runtime / platform metrics (cpu, memory, fds, gc, python version)
ProcessCollector(registry=registry)
PlatformCollector(registry=registry)
GCCollector(registry=registry)

_LAT = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

# ── build / meta ────────────────────────────────────────────────────────────────
build_info = Gauge("agent_build_info", "Agent build/runtime info", ["version", "env", "provider", "store"], registry=registry)

# ── HTTP ── micrometer-compatible name/labels so platform dashboards apply as-is ──
# (Spring services emit http_server_requests_seconds{method,uri,status,outcome})
http_server_requests = Histogram(
    "http_server_requests_seconds", "HTTP server request latency",
    ["method", "uri", "status", "outcome"], buckets=_LAT, registry=registry,
)
http_in_flight = Gauge("agent_http_requests_in_flight", "In-flight HTTP requests", registry=registry)


def http_outcome(status: int) -> str:
    if status < 400:
        return "SUCCESS"
    if status < 500:
        return "CLIENT_ERROR"
    return "SERVER_ERROR"

# ── LLM ──────────────────────────────────────────────────────────────────────────
llm_calls_total = Counter("agent_llm_calls_total", "LLM completions", ["provider", "model", "outcome"], registry=registry)
llm_duration = Histogram("agent_llm_request_duration_seconds", "LLM completion latency", ["provider", "model"], buckets=_LAT, registry=registry)
llm_tokens_total = Counter("agent_llm_tokens_total", "LLM tokens", ["provider", "model", "direction"], registry=registry)

# ── Tools ─────────────────────────────────────────────────────────────────────────
tool_exec_total = Counter("agent_tool_executions_total", "Tool executions", ["tool", "outcome"], registry=registry)
tool_exec_duration = Histogram("agent_tool_execution_duration_seconds", "Tool execution latency", ["tool"], buckets=_LAT, registry=registry)

# ── MCP ──────────────────────────────────────────────────────────────────────────
mcp_requests_total = Counter("agent_mcp_requests_total", "MCP requests", ["op", "outcome"], registry=registry)
mcp_duration = Histogram("agent_mcp_request_duration_seconds", "MCP request latency", ["op"], buckets=_LAT, registry=registry)

# ── Chat ─────────────────────────────────────────────────────────────────────────
chat_requests_total = Counter("agent_chat_requests_total", "Chat requests", ["mode", "outcome"], registry=registry)
chat_loop_iterations = Histogram("agent_chat_loop_iterations", "Agentic loop iterations per chat", buckets=(1, 2, 3, 4, 5, 6, 8, 10, 15, 20), registry=registry)

# ── Store / dependencies (gauges refreshed at scrape / health) ─────────────────────
active_sessions = Gauge("agent_active_sessions", "Active sessions (approx)", registry=registry)
dependency_up = Gauge("agent_dependency_up", "Dependency reachable (1=up,0=down)", ["dependency"], registry=registry)


@contextmanager
def timed(hist: Histogram, *labels: str):
    """Observe elapsed seconds into a labelled Histogram."""
    start = time.perf_counter()
    try:
        yield
    finally:
        hist.labels(*labels).observe(time.perf_counter() - start)


def set_dependency(name: str, up: bool) -> None:
    dependency_up.labels(name).set(1 if up else 0)


def render() -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST


def summary() -> dict:
    """Live counters/histograms as a compact JSON snapshot (totals + per-outcome
    breakdowns + average latencies), for /actuator/info. Reads the same registry
    Prometheus scrapes, so the numbers match /actuator/prometheus."""
    totals: dict[str, float] = {}
    breakdown: dict[tuple[str, str], dict[str, float]] = {}
    hist: dict[str, dict[str, float]] = {}

    for metric in registry.collect():
        for s in metric.samples:
            n = s.name
            if n.endswith("_total"):
                base = n[:-6]
                totals[base] = totals.get(base, 0.0) + s.value
                for lk in ("outcome", "direction", "mode"):
                    if lk in s.labels:
                        d = breakdown.setdefault((base, lk), {})
                        d[s.labels[lk]] = d.get(s.labels[lk], 0.0) + s.value
            elif n.endswith("_count"):
                hist.setdefault(n[:-6], {"count": 0.0, "sum": 0.0})["count"] += s.value
            elif n.endswith("_sum"):
                hist.setdefault(n[:-4], {"count": 0.0, "sum": 0.0})["sum"] += s.value

    def _total(base: str) -> int:
        return int(totals.get(base, 0.0))

    def _by(base: str, label: str) -> dict[str, int]:
        return {k: int(v) for k, v in breakdown.get((base, label), {}).items()}

    def _avg_ms(base: str) -> float | None:
        h = hist.get(base)
        return round((h["sum"] / h["count"]) * 1000, 1) if h and h["count"] else None

    def _avg(base: str) -> float | None:
        h = hist.get(base)
        return round(h["sum"] / h["count"], 2) if h and h["count"] else None

    return {
        "http": {
            "requests": int(hist.get("http_server_requests_seconds", {}).get("count", 0)),
            "avg_ms": _avg_ms("http_server_requests_seconds"),
        },
        "llm": {
            "calls": _total("agent_llm_calls"),
            "by_outcome": _by("agent_llm_calls", "outcome"),
            "tokens": _by("agent_llm_tokens", "direction"),
            "avg_ms": _avg_ms("agent_llm_request_duration_seconds"),
        },
        "tools": {
            "executions": _total("agent_tool_executions"),
            "by_outcome": _by("agent_tool_executions", "outcome"),
            "avg_ms": _avg_ms("agent_tool_execution_duration_seconds"),
        },
        "mcp": {
            "requests": _total("agent_mcp_requests"),
            "by_outcome": _by("agent_mcp_requests", "outcome"),
            "avg_ms": _avg_ms("agent_mcp_request_duration_seconds"),
        },
        "chat": {
            "requests": _total("agent_chat_requests"),
            "by_outcome": _by("agent_chat_requests", "outcome"),
            "avg_loop_iterations": _avg("agent_chat_loop_iterations"),
        },
    }
