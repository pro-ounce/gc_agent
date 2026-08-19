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

from app.commons.logger import get_logger

_tlog = get_logger("app.metrics.turn")

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

# ── End-to-end turn metrics (one chatbot turn: prompt → answer) ─────────────────────
turn_duration = Histogram(
    "agent_turn_duration_seconds", "Full chatbot turn latency (prompt→answer)",
    ["agent", "outcome"], buckets=_LAT, registry=registry,
)
turn_phase = Histogram(
    "agent_turn_phase_seconds", "Per-phase latency within a turn (retrieval|llm|tools)",
    ["agent", "phase"], buckets=_LAT, registry=registry,
)
turn_tools = Histogram(
    "agent_turn_tools", "Tools executed per turn", ["agent"],
    buckets=(0, 1, 2, 3, 4, 5, 8, 10, 15), registry=registry,
)


class TurnMetrics:
    """Accumulates the end-to-end breakdown of ONE chatbot turn — retrieval / LLM /
    tool time, iterations, tools called, tokens — and on finish() emits a single
    `turn_summary` log line (correlatable by request_id) plus Prometheus histograms.
    LLM time uses Ollama's own reported duration (excludes client backpressure);
    retrieval/tools are wall-clock around discrete awaits."""

    def __init__(self, agent: str = "chatbot", session_id: str = "", user_id: str = "",
                 request_id: str | None = None) -> None:
        self.agent = agent
        self.session_id = session_id
        self.user_id = user_id
        self.request_id = request_id
        self._t0 = time.perf_counter()
        self.retrieval_s = 0.0
        self.llm_s = 0.0
        self.tools_s = 0.0
        self.iterations = 0
        self.tools_used: list[str] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._done = False

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def _step(self, event: str, message: str, **fields) -> None:
        """Emit ONE concise in-flight log line (critical metric as it happens), tagged
        `turn_step` and correlatable by request_id. Complements the `turn_summary` line
        finish() writes at the end — so the log shows the turn WHILE in action, not only
        once completed."""
        _tlog.bind(
            event="turn_step", step=event, agent=self.agent,
            session_id=self.session_id, user_id=self.user_id, request_id=self.request_id,
            elapsed_ms=self._elapsed_ms(), **fields,
        ).info(message)

    @contextmanager
    def phase(self, name: str):
        """Wall-clock timer for a discrete-await phase ('retrieval' | 'tools')."""
        start = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - start
            if name == "retrieval":
                self.retrieval_s += dt
            elif name == "tools":
                self.tools_s += dt
            elif name == "llm":
                self.llm_s += dt
            # In-flight timing for the retrieval/tools phases (llm is logged per add_llm).
            if name in ("retrieval", "tools"):
                self._step(
                    name, f"↳ {name} {round(dt * 1000)}ms (turn {round(self._elapsed_ms())}ms)",
                    phase_ms=round(dt * 1000, 1),
                )

    def add_llm(self, seconds: float, prompt: int = 0, completion: int = 0) -> None:
        secs = max(0.0, seconds or 0.0)
        self.llm_s += secs
        self.prompt_tokens += int(prompt or 0)
        self.completion_tokens += int(completion or 0)
        self._step(
            "llm",
            f"↳ llm call #{self.iterations} {round(secs * 1000)}ms "
            f"tokens={int(prompt or 0)}/{int(completion or 0)} (turn {round(self._elapsed_ms())}ms)",
            iteration=self.iterations, call_ms=round(secs * 1000, 1),
            prompt_tokens=int(prompt or 0), completion_tokens=int(completion or 0),
        )

    def tool(self, name: str) -> None:
        self.tools_used.append(name)
        self._step(
            "tool", f"↳ tool #{len(self.tools_used)} {name} (turn {round(self._elapsed_ms())}ms)",
            tool=name, tool_index=len(self.tools_used),
        )

    def finish(self, outcome: str = "stop") -> None:
        if self._done:
            return
        self._done = True
        total = time.perf_counter() - self._t0
        turn_duration.labels(self.agent, outcome).observe(total)
        turn_phase.labels(self.agent, "retrieval").observe(self.retrieval_s)
        turn_phase.labels(self.agent, "llm").observe(self.llm_s)
        turn_phase.labels(self.agent, "tools").observe(self.tools_s)
        turn_tools.labels(self.agent).observe(len(self.tools_used))
        # Feed the aggregate chat counters (streaming turns weren't counted otherwise).
        chat_requests_total.labels("stream", "error" if outcome == "error" else "success").inc()
        chat_loop_iterations.observe(self.iterations)
        _tlog.bind(
            event="turn_summary", agent=self.agent, session_id=self.session_id,
            user_id=self.user_id, request_id=self.request_id, outcome=outcome,
            total_ms=round(total * 1000, 1), retrieval_ms=round(self.retrieval_s * 1000, 1),
            llm_ms=round(self.llm_s * 1000, 1), tools_ms=round(self.tools_s * 1000, 1),
            iterations=self.iterations, tools_used=self.tools_used,
            prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens,
        ).info(
            f"turn done: {round(total * 1000)}ms "
            f"[retrieval={round(self.retrieval_s * 1000)} llm={round(self.llm_s * 1000)} "
            f"tools={round(self.tools_s * 1000)}] iters={self.iterations} "
            f"tools={self.tools_used} tokens={self.prompt_tokens}/{self.completion_tokens} "
            f"outcome={outcome}"
        )


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
        # End-to-end per-turn: total latency + where it goes (retrieval/llm/tools).
        # Per-phase averages merge phases here (labels flattened) — for the split by
        # phase use /actuator/prometheus (agent_turn_phase_seconds{phase=...}) or the
        # per-turn `turn_summary` log line.
        "turn": {
            "count": int(hist.get("agent_turn_duration_seconds", {}).get("count", 0)),
            "avg_ms": _avg_ms("agent_turn_duration_seconds"),
            "avg_tools": _avg("agent_turn_tools"),
        },
    }
