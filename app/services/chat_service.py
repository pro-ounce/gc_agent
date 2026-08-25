"""
services/chat_service.py
────────────────────────
Core agentic loop that orchestrates:
  1. Session history management
  2. Tool discovery from MCP registry
  3. LLM invocation (OLLAMA)
  4. Risk-aware tool execution / pending-action gating
  5. Iterative tool-call → result → LLM loop (up to MAX_ITERATIONS)

Both blocking and streaming chat modes are supported.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, AsyncIterator

from ..commons import metrics as M
from ..commons.config import cfg
from ..commons.flags import flags
from ..services import runtime_config
from ..commons.logger import get_logger
from ..mcp.prompt_registry import prompt_registry
from ..mcp.tool_registry import is_mutation, tool_registry
from ..models.chat import ChatResponse, StreamChunk, UIBlock
from ..models.mcp import PendingAction
from ..services import skills
from ..services.ui_blocks import blocks_from_outputs, blocks_to_text, lead_in
from ..models.session import Session
from ..services.llm_service import LLMResponse, ToolCall, llm
from ..services.session_service import session_service

log = get_logger(__name__)

import re

# Matches a turn that only ANNOUNCES a tool call ("I will fetch…", "Let me look up…")
# without emitting one — qwen sometimes does this and dead-ends the turn. We nudge once.
_INTENT_RE = re.compile(
    r"\b(i['’]?ll|i will|i am going to|i['’]?m going to|let me|let us|first,? (?:let|i))\b"
    r"[^.]*\b(fetch|retrieve|look up|look\-up|get|obtain|find|call|use|check|query)\b",
    re.IGNORECASE,
)


def _looks_like_unfulfilled_intent(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and _INTENT_RE.search(t) is not None


# Never leave the user with a blank bubble — a graceful fallback when the model produced
# neither text, a tool call, nor blocks.
_EMPTY_FALLBACK = (
    "I couldn't find an answer for that. Could you rephrase or add a bit more detail — "
    "for example the application, user, or exactly what you'd like me to do?"
)

def _friendly_status(tool_name: str) -> str:
    """Human 'working on it' line for a tool call — never exposes the method name.
    Delegates to skills.status_label (curated tool labels + generic verb fallback)."""
    return skills.status_label(tool_name)


# Markers that signal the model is emitting a tool call AS TEXT (qwen does this instead of a
# structured tool_call) — a fenced code block, a <tool_call> tag, or a bare {"name": …}. The
# _extract_text_tool_calls fallback recovers the real call, but the raw JSON (and any
# fabricated response the model writes after it) must not leak into the streamed UI.
_TOOL_TEXT_MARKER = re.compile(r"```|<tool_call>|\{\s*\"name\"")
_MARKER_MAXLEN = 11  # longest marker ("<tool_call>") — hold this many tail chars back


class _StreamGate:
    """Streams assistant text live, but the moment a tool-call-ish marker appears it HOLDS
    everything from that point. On close(): if the turn produced a tool call, the held text
    (the JSON + any fabricated response) is dropped; if it was a false alarm, it's flushed.
    Full text is still accumulated separately by the caller for tool extraction / history."""

    def __init__(self, hold_all: bool = False) -> None:
        self._buf = ""
        self._gated = False
        # hold_all: buffer ALL text and reveal nothing mid-turn — the caller flushes it at
        # close() only if the turn was a plain answer (no tool call). This removes the
        # "I'll create the user…" flash that a tool/confirm/validation result then erases.
        self._hold_all = hold_all

    def feed(self, delta: str) -> str:
        self._buf += delta
        if self._hold_all or self._gated:
            return ""
        m = _TOOL_TEXT_MARKER.search(self._buf)
        if m:
            self._gated = True
            out, self._buf = self._buf[: m.start()], self._buf[m.start():]
            return out
        # No marker yet — stream all but a short tail, in case a marker is forming at the edge.
        if len(self._buf) > _MARKER_MAXLEN:
            out, self._buf = self._buf[:-_MARKER_MAXLEN], self._buf[-_MARKER_MAXLEN:]
            return out
        return ""

    def close(self, tool_called: bool) -> str:
        # Discard held text when a tool ran (its result/confirm replaces it); otherwise flush
        # the plain answer. In hold_all mode nothing was streamed, so this is the whole reply.
        if tool_called and (self._gated or self._hold_all):
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out


def _schema_names(tools: list[dict] | None) -> list[str]:
    """Tool NAMES out of the OpenAI-format schemas select_tools returns."""
    names: list[str] = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
    return names


def _log_prompt(session_id: str, user_id: str | None, question: str,
                tools: list[dict] | None, mode: str) -> None:
    """Emit ONE correlatable line pairing the end-user's question with the tools RAG
    retrieved for it — so a turn is diagnosable from the logs (prompt → retrieval →
    answer) without a client screenshot. Auto-carries request_id/trace_id via the logger
    context. Gated by LOG_USER_PROMPTS (default on)."""
    if not runtime_config.get_bool("LOG_USER_PROMPTS"):
        return
    q = question or ""
    names = _schema_names(tools)
    preview = q if len(q) <= 300 else q[:300] + "…"
    log.bind(
        event="chat_prompt", mode=mode, session_id=session_id, user_id=str(user_id or ""),
        question=q, question_len=len(q), retrieved_tools=names, tool_count=len(names),
    ).info(f'prompt [{session_id}] "{preview}" → {len(names)} tools retrieved: {names}')


class ChatService:
    """Stateless orchestrator — all state lives in the Session object (Redis)."""

    # ── Non-streaming chat ────────────────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
        system_prompt: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> ChatResponse:
        session = session_service.get_or_create(session_id, user_id)
        session.add_user(user_message)
        session_service.save(session)

        system = system_prompt or cfg.AGENT_SYSTEM_PROMPT
        # Skill? Pin its backing action tool + ground the model on the required fields.
        skill = skills.match(user_message)
        if skill:
            system = system + skills.grounding(skill)
        # Tool-RAG: pick only tools relevant to this query (falls back to all — see select_tools).
        tools = await tool_registry.select_tools(
            user_message, request_headers, extra=[skill.tool] if skill else None
        )
        _log_prompt(session_id, user_id, user_message, tools, mode="sync")
        tool_calls_made: list[str] = []

        llm_response: LLMResponse | None = None
        tool_outputs: list[tuple[str, Any, bool]] = []

        for iteration in range(runtime_config.get_int("LLM_MAX_ITERATIONS")):
            messages = session.to_llm_messages()
            llm_response = await llm().complete(messages, tools, system)

            log.bind(
                func="chat",
                session_id=session_id,
                iteration=iteration,
                finish=llm_response.finish_reason,
            ).debug(f"LLM iteration {iteration}: finish={llm_response.finish_reason}")

            if not llm_response.has_tool_calls:
                break

            # Validate skill mutations BEFORE creating (format + uniqueness).
            verr = await self._validate_skill_calls(llm_response.tool_calls, request_headers)
            if verr:
                session.add_assistant(verr)
                session_service.save(session)
                return ChatResponse(
                    session_id=session_id,
                    message_id=str(uuid.uuid4()),
                    assistant_message=verr,
                    blocks=[UIBlock(type="notice", level="error", text=verr)],
                    tool_calls_made=tool_calls_made,
                    finish_reason="validation_failed",
                )

            # Process tool calls
            pending = await self._process_tool_calls(
                session=session,
                tool_calls=llm_response.tool_calls,
                llm_text=llm_response.text,
                tool_calls_made=tool_calls_made,
                request_headers=request_headers,
                tool_outputs=tool_outputs,
            )

            if pending:
                session.pending_action_id = pending.id
                session_service.save(session)
                return ChatResponse(
                    session_id=session_id,
                    message_id=str(uuid.uuid4()),
                    assistant_message=(
                        llm_response.text
                        or f"I need your confirmation to run '{pending.tool_name}' "
                        f"(risk: {pending.risk_level})."
                    ),
                    pending_action=pending,
                    tool_calls_made=tool_calls_made,
                    model=llm_response.model,
                    usage=llm_response.usage,
                    finish_reason="tool_confirmation_required",
                )

            # Structured-response mode (Approach B): read-only tool results already render as
            # blocks → skip the synthesis call, emit a one-line lead-in + blocks.
            blocks = blocks_from_outputs(tool_outputs)
            if (blocks and cfg.SKIP_SYNTHESIS_WITH_BLOCKS
                    and not any(is_mutation(n) for n in tool_calls_made)):
                lead = lead_in(blocks)
                session.add_assistant(lead)
                session_service.save(session)
                return ChatResponse(
                    session_id=session_id,
                    message_id=str(uuid.uuid4()),
                    assistant_message=lead,
                    blocks=blocks,
                    tool_calls_made=tool_calls_made,
                    model=llm_response.model,
                    usage=llm_response.usage,
                    finish_reason="stop",
                )

        # Final assistant message + structured blocks (rendered from the actual tool
        # results, not the model's prose — see services/ui_blocks).
        final_text = ((llm_response.text or "").strip() if llm_response else "") or _EMPTY_FALLBACK
        blocks = blocks_from_outputs(tool_outputs)
        # If the model returned no prose but we have data, give a plain-text fallback so
        # non-block clients aren't left empty.
        if not final_text.strip() and blocks:
            final_text = blocks_to_text(blocks)
        session.add_assistant(final_text)
        session_service.save(session)

        return ChatResponse(
            session_id=session_id,
            message_id=str(uuid.uuid4()),
            assistant_message=final_text,
            blocks=blocks,
            tool_calls_made=tool_calls_made,
            model=llm_response.model if llm_response else "",
            usage=llm_response.usage if llm_response else None,
            finish_reason=(llm_response.finish_reason if llm_response else "stop"),
        )

    # ── Streaming chat ────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        session = session_service.get_or_create(session_id, user_id)
        session.add_user(user_message)
        session_service.save(session)

        system = system_prompt or cfg.AGENT_SYSTEM_PROMPT
        tools = await tool_registry.as_tools()
        messages = session.to_llm_messages()

        full_text = ""
        async for chunk in llm().stream(messages, tools, system):
            full_text += chunk
            yield StreamChunk(type="delta", session_id=session_id, content=chunk)

        session.add_assistant(full_text)
        session_service.save(session)

        yield StreamChunk(
            type="done",
            session_id=session_id,
            content=full_text,
            finish_reason="stop",
        )

    async def reply_stream(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
        system_prompt: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Agentic streaming with a CONFIRMATION GATE. Read-only tools run inline; a
        mutating tool (create/update/delete/activate/…) is NOT executed — the stream
        emits a 'confirm_required' chunk carrying the pending action and pauses.
        The caller resumes via confirm_stream() once the user approves/declines.
        """
        turn = M.TurnMetrics(
            agent=session_id.split(":", 1)[0] or "chatbot",
            session_id=session_id,
            user_id=str(user_id or ""),
        )
        session = session_service.get_or_create(session_id, user_id)
        session.add_user(user_message)
        session.metadata["last_user_message"] = user_message
        session_service.save(session)
        system = system_prompt or cfg.AGENT_SYSTEM_PROMPT
        skill = skills.match(user_message)
        if skill:
            system = system + skills.grounding(skill)
        with turn.phase("retrieval"):
            tools = await tool_registry.select_tools(
                user_message, request_headers, extra=[skill.tool] if skill else None
            )
        _log_prompt(session_id, user_id, user_message, tools, mode="stream")
        async for chunk in self._stream_loop(session, tools, system, request_headers, turn):
            yield chunk

    async def confirm_stream(
        self,
        session_id: str,
        action_id: str,
        confirmed: bool,
        request_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Resume a stream paused on 'confirm_required': run (or skip) the pending
        mutating tool, record the result, then stream the model's continuation."""
        turn = M.TurnMetrics(agent=session_id.split(":", 1)[0] or "chatbot", session_id=session_id)
        session = session_service.get_or_create(session_id)
        pending_raw = (session.metadata.get("pending_actions") or {}).get(action_id)
        if not pending_raw:
            yield StreamChunk(type="error", session_id=session_id, error="No such pending action.")
            return
        tc_id = pending_raw.get("_tc_id", action_id)
        tool_name = pending_raw["tool_name"]
        tool_args = pending_raw.get("tool_args", {})
        session.pending_action_id = None
        (session.metadata.get("pending_actions") or {}).pop(action_id, None)

        if not confirmed:
            session.add_tool_result(tc_id, tool_name, "User declined to run this action.")
            session_service.save(session)
            if turn:
                turn.finish("cancelled")
            yield StreamChunk(type="done", session_id=session_id,
                              content="Action cancelled.", finish_reason="cancelled")
            return

        yield StreamChunk(type="tool_use", session_id=session_id, content=_friendly_status(tool_name))
        with turn.phase("tools"):
            result = await tool_registry.execute(tool_name, tool_args, request_headers)
        turn.tool(tool_name)
        output = str(result.output) if result.success else f"Error: {result.error}"
        session.add_tool_result(tc_id, tool_name, output)
        # Run the skill's dependency chain (if any), then render the outcome as a block +
        # one-line lead-in — no re-run LLM synthesis (which garbles on a bare result turn).
        render_out, render_name = result.output, tool_name
        if result.success:
            async for _ev in self._skill_chain_events(tool_name, tool_args, result.output, request_headers):
                if _ev[0] == "status":
                    yield StreamChunk(type="tool_use", session_id=session_id, content=_ev[1])
                elif _ev[0] == "result":
                    render_out, render_name = _ev[1], _ev[2]
        blocks = blocks_from_outputs([(render_name, render_out if result.success else result.error, result.success)])
        lead = lead_in(blocks) if blocks else (output or "Done.")
        session.add_assistant(lead)
        session_service.save(session)
        if turn:
            turn.finish("stop")
        yield StreamChunk(type="done", session_id=session_id, content=lead,
                          blocks=blocks, finish_reason="stop")

    async def _stream_loop(
        self,
        session: Session,
        tools: list[dict[str, Any]],
        system: str,
        request_headers: dict[str, str] | None,
        turn: "M.TurnMetrics | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        """Shared agentic loop for reply_stream / confirm_stream."""
        from ..services.llm_service import _extract_text_tool_calls

        session_id = session.session_id
        nudged = False
        tool_outputs: list[tuple[str, Any, bool]] = []
        for _ in range(runtime_config.get_int("LLM_MAX_ITERATIONS")):
            if turn:
                turn.iterations += 1
            messages = session.to_llm_messages()
            text = ""
            tool_calls: list[dict[str, Any]] = []
            gate = _StreamGate(hold_all=not cfg.STREAM_PARTIAL_TEXT)
            try:
                async for kind, payload in llm().stream_chat(messages, tools, system):
                    if kind == "delta":
                        text += payload
                        visible = gate.feed(payload)
                        if visible:
                            yield StreamChunk(type="delta", session_id=session_id, content=visible)
                    elif kind == "tool_calls":
                        tool_calls = payload
                    elif kind == "usage":
                        secs = payload.get("llm_seconds", 0.0) or 0.0
                        pt = int(payload.get("prompt_tokens", 0) or 0)
                        ct = int(payload.get("completion_tokens", 0) or 0)
                        # Aggregate LLM metrics — streaming calls weren't counted before.
                        M.llm_calls_total.labels(cfg.LLM_PROVIDER, cfg.LLM_MODEL, "success").inc()
                        if secs:
                            M.llm_duration.labels(cfg.LLM_PROVIDER, cfg.LLM_MODEL).observe(secs)
                        if pt:
                            M.llm_tokens_total.labels(cfg.LLM_PROVIDER, cfg.LLM_MODEL, "input").inc(pt)
                        if ct:
                            M.llm_tokens_total.labels(cfg.LLM_PROVIDER, cfg.LLM_MODEL, "output").inc(ct)
                        if turn:
                            turn.add_llm(secs, pt, ct)
            except Exception as exc:  # noqa: BLE001
                log.exception(f"reply_stream error: {exc}")
                if turn:
                    turn.finish("error")
                yield StreamChunk(type="error", session_id=session_id, error=str(exc))
                return

            # Recover a tool call the model emitted as text (fallback).
            if not tool_calls and text:
                recovered, cleaned = _extract_text_tool_calls(text)
                if recovered:
                    tool_calls = [{"id": f"tc_{c.name}", "name": c.name, "input": c.input} for c in recovered]
                    text = cleaned

            # Flush the stream gate: drop held tool-call JSON if a tool was called this turn,
            # otherwise (false alarm) emit the held text so nothing legitimate is lost.
            tail = gate.close(bool(tool_calls))
            if tail:
                yield StreamChunk(type="delta", session_id=session_id, content=tail)

            if not tool_calls:
                # Model announced a tool call ("I will fetch…") but didn't emit one → nudge it
                # ONCE to actually make the call instead of dead-ending the turn.
                if not nudged and _looks_like_unfulfilled_intent(text):
                    nudged = True
                    session.add_assistant(text)
                    session.add_user(
                        "Proceed now: call the appropriate tool to get that data, then answer. "
                        "Do not just restate that you will."
                    )
                    session_service.save(session)
                    continue
                sblocks = blocks_from_outputs(tool_outputs)
                text = (text or "").strip() or ("" if sblocks else _EMPTY_FALLBACK)
                session.add_assistant(text)
                session_service.save(session)
                if turn:
                    turn.finish("stop")
                yield StreamChunk(type="done", session_id=session_id, content=text,
                                  blocks=sblocks, finish_reason="stop")
                return

            # Validate skill mutations BEFORE creating (format + uniqueness).
            verr = await self._validate_skill_calls(tool_calls, request_headers)
            if verr:
                session.add_assistant(verr)
                session_service.save(session)
                if turn:
                    turn.finish("validation_failed")
                yield StreamChunk(type="done", session_id=session_id, content=verr,
                                  blocks=[UIBlock(type="notice", level="error", text=verr)],
                                  finish_reason="validation_failed")
                return

            # Run tools inline; STOP at the first one that needs confirmation.
            executed: list[tuple[dict[str, Any], str]] = []
            pending_tc: dict[str, Any] | None = None
            pending_tool = None
            for tc in tool_calls:
                tool = await tool_registry.get_tool(tc["name"])
                # MCP declares per-tool risk → requires_confirmation (HIGH/CRITICAL). The
                # name-based is_mutation heuristic is only a fallback when metadata is absent.
                needs_confirm = (
                    tool.requires_confirmation if tool is not None else is_mutation(tc["name"])
                )
                if (
                    runtime_config.get_bool("TOOL_RISK_CONFIRMATION")
                    and needs_confirm
                    and not session.metadata.get("bypass_confirmation")
                ):
                    pending_tc = tc
                    pending_tool = tool
                    break
                yield StreamChunk(type="tool_use", session_id=session_id, content=_friendly_status(tc["name"]))
                _t_tool = time.perf_counter()
                result = await tool_registry.execute(tc["name"], tc["input"], request_headers)
                if turn:
                    turn.tools_s += time.perf_counter() - _t_tool
                    turn.tool(tc["name"])
                output = str(result.output) if result.success else f"Error: {result.error}"
                tool_outputs.append((tc["name"], result.output if result.success else result.error, result.success))
                executed.append((tc, output))

            # Record the assistant turn with exactly the tool_calls we handle (the ones run
            # inline + the one gated), so every recorded tool_call gets a matching tool_result
            # (now, or on confirm) — otherwise the next LLM turn 400s on a dangling call.
            handled = [tc for tc, _ in executed] + ([pending_tc] if pending_tc else [])
            session.add_assistant(
                text,
                tool_calls=[
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["input"]}}
                    for tc in handled
                ],
            )
            for tc, output in executed:
                session.add_tool_result(tc["id"], tc["name"], output)

            # Structured-response mode (Approach B): if the read-only results render as blocks,
            # skip the synthesis LLM call and finish now with a one-line lead-in + blocks. This
            # removes the stream-prose-then-swap-to-table double render and its generation cost.
            if (executed and pending_tc is None and cfg.SKIP_SYNTHESIS_WITH_BLOCKS
                    and not any(is_mutation(tc["name"]) for tc, _ in executed)):
                sblocks = blocks_from_outputs(tool_outputs)
                if sblocks:
                    lead = lead_in(sblocks)
                    session.add_assistant(lead)
                    session_service.save(session)
                    if turn:
                        turn.finish("stop")
                    yield StreamChunk(type="done", session_id=session_id, content=lead,
                                      blocks=sblocks, finish_reason="stop")
                    return

            if pending_tc is not None:
                risk = (pending_tool.risk_level if pending_tool else "HIGH").upper()
                pending = PendingAction(
                    session_id=session_id,
                    tool_name=pending_tc["name"],
                    tool_args=pending_tc["input"],
                    risk_level=risk,
                    description=f"Runs '{pending_tc['name']}' (risk: {risk}). Confirm to proceed.",
                )
                session.pending_action_id = pending.id
                stored = pending.model_dump()
                stored["_tc_id"] = pending_tc["id"]   # tie the confirm back to this tool_call
                session.metadata.setdefault("pending_actions", {})[pending.id] = stored
                session_service.save(session)
                if turn:
                    turn.finish("confirm_required")
                yield StreamChunk(
                    type="confirm_required",
                    session_id=session_id,
                    content=f"⚠️ This will run **{pending_tc['name']}** (risk: {risk}) with "
                    f"{pending_tc['input']}. Confirm to proceed.",
                    pending_action=pending,
                )
                return

            session_service.save(session)

        if turn:
            turn.finish("max_iterations")
        yield StreamChunk(type="done", session_id=session_id, content="",
                          blocks=blocks_from_outputs(tool_outputs), finish_reason="max_iterations")

    # ── Background task runner ────────────────────────────────────────────────

    async def run_background_task(
        self, task_id: str, tool: str, args: dict[str, Any],
        request_headers: dict[str, str] | None,
    ) -> None:
        """Execute a long-running skill in the background: run the entry tool + its `then`
        chain, updating the durable Task with progress and the final result blocks."""
        from ..services import task_service
        task_service.update(task_id, status="running",
                            progress={"step": skills.status_label(tool).rstrip("…"), "pct": 15})
        try:
            result = await tool_registry.execute(tool, dict(args), request_headers)
            if not result.success:
                task_service.update(task_id, status="failed", error=str(result.error))
                return
            render_out, render_name = result.output, tool
            async for ev in self._skill_chain_events(tool, args, result.output, request_headers):
                if ev[0] == "status":
                    task_service.update(task_id, progress={"step": str(ev[1]).rstrip("…")})
                elif ev[0] == "result":
                    render_out, render_name = ev[1], ev[2]
            blocks = blocks_from_outputs([(render_name, render_out, True)])
            task_service.update(task_id, status="succeeded",
                                result=[b.model_dump() for b in blocks],
                                progress={"step": "Done", "pct": 100})
            log.bind(func="task_run", task=task_id).info(f"task {task_id} succeeded")
        except Exception as exc:  # noqa: BLE001
            log.exception(f"background task {task_id} failed: {exc}")
            task_service.update(task_id, status="failed", error=str(exc))

    # ── Skill validation & dependency chains ──────────────────────────────────

    async def _validate_skill_calls(self, tool_calls, request_headers) -> str | None:
        """Run pre-create validation (format + uniqueness) for any skill mutation the model
        is about to call. Returns a combined error message, or None if all clear."""
        for tc in tool_calls or []:
            name = getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else None)
            args = getattr(tc, "input", None)
            if args is None and isinstance(tc, dict):
                args = tc.get("input", {})
            errs = await skills.validate_inputs(name or "", args or {}, tool_registry.execute, request_headers)
            if errs:
                return " ".join(errs)
        return None

    async def _skill_chain_events(
        self, entry_tool: str, entry_args: dict[str, Any], entry_output: Any,
        request_headers: dict[str, str] | None,
    ):
        """Async generator for a skill's `then` dependency chain. Yields ("status", text)
        BEFORE each step (so the UI can show 'Fetching…/Assigning…' progress), then a final
        ("result", output, source_tool). Threads each step's captured outputs into the next."""
        skill = skills.by_tool(entry_tool)
        render_out, render_name = entry_output, entry_tool
        if skill and skill.then:
            ctx: dict[str, Any] = dict(entry_args or {})
            for step in skill.then:
                yield ("status", skills.status_label(step.tool, step))
                args = skills.resolve_step_args(step, ctx)
                try:
                    res = await tool_registry.execute(step.tool, args, request_headers)
                except Exception as exc:  # noqa: BLE001
                    log.bind(func="skill_chain", step=step.tool).warning(f"chain step failed: {exc}")
                    if step.optional:
                        continue
                    break
                if not res.success:
                    if step.optional:
                        continue
                    log.bind(func="skill_chain", step=step.tool).warning("chain step failed; stopping")
                    break
                for ck, path in step.capture.items():
                    ctx[ck] = skills.dig(res.output, path)
                if step.render:
                    render_out, render_name = res.output, step.tool
                log.bind(func="skill_chain", step=step.tool).info(f"chain step {step.tool} ok")
        yield ("result", render_out, render_name)

    # ── Action confirmation ───────────────────────────────────────────────────

    async def confirm_action(
        self,
        session_id: str,
        action_id: str,
        confirmed: bool,
        request_headers: dict[str, str] | None = None,
    ) -> ChatResponse:
        session = session_service.get_or_create(session_id)

        if not session.pending_action_id:
            return ChatResponse(
                session_id=session_id,
                message_id=str(uuid.uuid4()),
                assistant_message="No pending action to confirm.",
                finish_reason="no_pending_action",
            )

        pending_raw = session.metadata.get("pending_actions", {}).get(action_id)
        if not pending_raw:
            return ChatResponse(
                session_id=session_id,
                message_id=str(uuid.uuid4()),
                assistant_message="Pending action not found.",
                finish_reason="action_not_found",
            )

        pending = PendingAction(**pending_raw)
        session.pending_action_id = None
        session.metadata.setdefault("pending_actions", {}).pop(action_id, None)

        if not confirmed:
            session.add_tool_result(action_id, pending.tool_name, "User cancelled this operation.")
            session_service.save(session)
            return ChatResponse(
                session_id=session_id,
                message_id=str(uuid.uuid4()),
                assistant_message="Action cancelled by user.",
                finish_reason="cancelled",
            )

        # Execute the confirmed tool
        result = await tool_registry.execute(pending.tool_name, pending.tool_args, request_headers)
        output_text = str(result.output) if result.success else f"Error: {result.error}"
        session.add_tool_result(action_id, pending.tool_name, output_text)

        # Run the skill's dependency chain (if any), threading the entry's collected args.
        render_out, render_name = result.output, pending.tool_name
        if result.success:
            async for _ev in self._skill_chain_events(
                pending.tool_name, pending.tool_args, result.output, request_headers):
                if _ev[0] == "result":
                    render_out, render_name = _ev[1], _ev[2]

        # Render the outcome as a block (success/error notice or a card) rather than a
        # re-run LLM synthesis — which tends to hallucinate on a bare tool-result turn.
        blocks = blocks_from_outputs([(render_name, render_out if result.success else result.error, result.success)])
        final_text = lead_in(blocks) if blocks else output_text
        session.add_assistant(final_text)
        session_service.save(session)

        return ChatResponse(
            session_id=session_id,
            message_id=str(uuid.uuid4()),
            assistant_message=final_text,
            blocks=blocks,
            tool_calls_made=[pending.tool_name],
            finish_reason="stop",
        )

    # ── Prompt execution ──────────────────────────────────────────────────────

    async def execute_prompt(
        self,
        session_id: str,
        prompt_name: str,
        arguments: dict[str, str],
        user_id: str | None = None,
    ) -> ChatResponse:
        """Render a server-side prompt and inject it as a user message."""
        rendered = await prompt_registry.execute_prompt(prompt_name, arguments)
        return await self.chat(session_id=session_id, user_message=rendered, user_id=user_id)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _process_tool_calls(
        self,
        session: Session,
        tool_calls: list[ToolCall],
        llm_text: str,
        tool_calls_made: list[str],
        request_headers: dict[str, str] | None,
        tool_outputs: list[tuple[str, Any, bool]] | None = None,
    ) -> PendingAction | None:
        """
        Execute tool calls sequentially.
        Returns a PendingAction if any tool requires confirmation, else None.
        `tool_outputs`, when given, collects (name, raw_output, success) per execution
        so the caller can build structured UIBlocks from the actual tool results.
        """
        import json

        for tc in tool_calls:
            tool = await tool_registry.get_tool(tc.name)

            # Risk gate — pause and ask user before executing
            if (
                runtime_config.get_bool("TOOL_RISK_CONFIRMATION")
                and tool
                and tool.requires_confirmation
                and not session.metadata.get("bypass_confirmation")
            ):
                pending = PendingAction(
                    id=str(uuid.uuid4()),
                    session_id=session.session_id,
                    tool_name=tc.name,
                    tool_args=tc.input,
                    risk_level=tool.risk_level,
                    description=tool.description,
                )
                # Record the assistant message + tool_call in history
                openai_tool_calls = [
                    {
                        "id": pending.id,
                        "type": "function",
                        # Ollama expects arguments as an OBJECT, not a JSON string.
                        "function": {"name": tc.name, "arguments": tc.input},
                    }
                ]
                session.add_assistant(llm_text, tool_calls=openai_tool_calls)
                session.metadata.setdefault("pending_actions", {})[pending.id] = pending.model_dump()
                return pending

            # Execute immediately
            log.bind(func="tool_call", tool=tc.name, session_id=session.session_id).info(
                f"Executing tool '{tc.name}'"
            )
            result = await tool_registry.execute(tc.name, tc.input, request_headers)
            tool_calls_made.append(tc.name)
            output = str(result.output) if result.success else f"Error: {result.error}"
            if tool_outputs is not None:
                tool_outputs.append((tc.name, result.output if result.success else result.error, result.success))

            # Record assistant tool_call + tool result. Ollama expects arguments as an
            # OBJECT (dict), not a JSON string — a string here 400s the follow-up turn.
            openai_tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.input},
                }
            ]
            session.add_assistant(llm_text, tool_calls=openai_tool_calls)
            session.add_tool_result(tc.id, tc.name, output)

        return None


chat_service = ChatService()
