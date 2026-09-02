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
from ..commons.logger import get_logger, get_request_id
from ..mcp.prompt_registry import prompt_registry
from ..mcp.tool_registry import is_mutation, tool_registry
from ..models.chat import ChatResponse, StreamChunk, Suggestion, UIBlock
from ..models.mcp import PendingAction
from ..services import skills
from ..services import flows
from ..services import meta
from ..agents.registry import STRICT_GROUNDING_INSTRUCTION
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
    r"[^.]*\b(fetch|retrieve|look up|look\-up|get|obtain|find|call|use|check|query|"
    r"create|add|register|save|update|assign|onboard|set up|deactivate|activate|remove|delete)\b",
    re.IGNORECASE,
)


# The model is legitimately ASKING the user for input (a clarifying question) — the turn
# should END here, never be nudged to "proceed" (which makes it invent values).
_ASKING_RE = re.compile(
    r"\?|\b(could you|can you|would you|please provide|please share|what (is|are|'s)|"
    r"which|provide (the|these|your|me)|i need (the|some|to know|more)|let me know|"
    r"do you (have|want)|may i)\b",
    re.IGNORECASE,
)


def _looks_like_unfulfilled_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t or _ASKING_RE.search(t):   # a question to the user is a valid turn end, not a dead-end
        return False
    return _INTENT_RE.search(t) is not None


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

    @staticmethod
    def _ground(system: str) -> str:
        """Enforce the air-gap on EVERY entry point: append the strict GC360 grounding to the
        system prompt (idempotent) whenever AGENT_STRICT_GROUNDING is on. Centralised here so
        /api/chat and the platform/gateway path are equally confined — no world knowledge."""
        if runtime_config.get_bool("AGENT_STRICT_GROUNDING") and STRICT_GROUNDING_INSTRUCTION not in system:
            return f"{system}{STRICT_GROUNDING_INSTRUCTION}"
        return system

    # ── Non-streaming chat ────────────────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
        system_prompt: str | None = None,
        request_headers: dict[str, str] | None = None,
        detail: str | None = None,
    ) -> ChatResponse:
        session = session_service.get_or_create(session_id, user_id)
        session.add_user(user_message)
        session.metadata["detail"] = (detail or "standard").lower()
        session_service.save(session)

        # Guided flow active (e.g. onboarding)? Advance it deterministically — no LLM.
        # Or a fresh intent (create user without full detail) may START a guided intake.
        if flows.is_active(session):
            fr = await flows.handle(session, user_message, request_headers)
            if fr is not None:
                return self._flow_response(session, fr)
        else:
            mr = await meta.handle(user_message, request_headers)
            if mr is not None:
                return self._flow_response(session, mr)
            started = flows.maybe_start(session, user_message)
            if started is not None:
                return self._flow_response(session, started)

        system = self._ground(system_prompt or cfg.AGENT_SYSTEM_PROMPT)
        # Skill? Pin its backing action tool + ground the model on the required fields.
        skill = skills.match(user_message)
        if skill:
            system = system + skills.grounding(skill)
        # Tool-RAG: pick only tools relevant to this query (falls back to all — see select_tools).
        # For a mutation skill, offer ONLY its action tool so a weaker model can't read-and-stop.
        only = [skill.tool] if (skill and (is_mutation(skill.tool) or skill.focused)) else None
        slim = {skill.tool: skill.schema} if (skill and skill.schema) else None
        tools = await tool_registry.select_tools(
            user_message, request_headers, extra=[skill.tool] if skill else None,
            only=only, slim=slim,
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
                vmsg, vlevel = verr
                session.add_assistant(vmsg)
                session_service.save(session)
                return ChatResponse(
                    session_id=session_id,
                    message_id=str(uuid.uuid4()),
                    assistant_message=vmsg,
                    blocks=[UIBlock(type="notice", level=vlevel, text=vmsg)],
                    tool_calls_made=tool_calls_made,
                    finish_reason="needs_input" if vlevel == "info" else "validation_failed",
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
                        or skills.confirm_summary(pending.tool_name, pending.tool_args)
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
                lead = await self._synthesize_answer(session, tool_outputs) or lead_in(blocks)
                blocks = await self._apply_detail(session, blocks, tool_outputs, request_headers)
                session.add_assistant(lead)
                self._log_answer(session_id, lead, blocks)
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
        self._log_answer(session_id, final_text, blocks)
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

        system = self._ground(system_prompt or cfg.AGENT_SYSTEM_PROMPT)
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

    # ── Guided-flow helpers ───────────────────────────────────────────────────

    def _arm_flow_pending(self, session: Session, fr: "flows.FlowResult") -> PendingAction:
        """Turn a flow's hand-off into a stored pending action (reuses the confirm path)."""
        p = fr.pending or {}
        pending = PendingAction(
            id=str(uuid.uuid4()),
            session_id=session.session_id,
            tool_name=p["tool_name"],
            tool_args=p["tool_args"],
            description=p.get("summary") or skills.confirm_summary(p["tool_name"], p["tool_args"]),
        )
        session.add_assistant(fr.message, tool_calls=[{
            "id": pending.id, "type": "function",
            "function": {"name": p["tool_name"], "arguments": p["tool_args"]},
        }])
        session.metadata.setdefault("pending_actions", {})[pending.id] = pending.model_dump()
        session.pending_action_id = pending.id
        return pending

    def _post_confirm_flow(
        self, session: Session, tool_name: str, tool_args: dict[str, Any], final_text: str,
    ) -> tuple[str, list[Suggestion]]:
        """After a successful confirmed mutation, chain the guided flow:
        creating a user arms onboarding; an in-flow assign resumes it; otherwise the
        skill's plain text follow-up (if any) is appended. Returns (text, chips)."""
        args = tool_args or {}
        if tool_name == "addUser_post":
            fr = flows.start_onboarding(session, args.get("firstName"), args.get("userName"))
            return f"{final_text}\n\n{fr.message}", [Suggestion(**s) for s in fr.suggestions]
        if flows.is_active(session) and tool_name == "addUserApplicationAndRole_post":
            fr = flows.after_assign(session)
            if fr is not None:
                return f"{final_text}\n\n{fr.message}", [Suggestion(**s) for s in fr.suggestions]
        fu = skills.follow_up_for(tool_name, args)
        return (f"{final_text}\n\n{fu}" if fu else final_text), []

    def _flow_response(self, session: Session, fr: "flows.FlowResult") -> ChatResponse:
        """Render a guided-flow turn (sync). Either a confirm hand-off or a prompt+chips."""
        sid = session.session_id
        if fr.pending:
            pending = self._arm_flow_pending(session, fr)
            session_service.save(session)
            return ChatResponse(
                session_id=sid, message_id=str(uuid.uuid4()),
                assistant_message=fr.message, pending_action=pending,
                finish_reason="tool_confirmation_required",
            )
        session.add_assistant(fr.message)
        session_service.save(session)
        return ChatResponse(
            session_id=sid, message_id=str(uuid.uuid4()),
            assistant_message=fr.message,
            suggestions=[Suggestion(**s) for s in fr.suggestions],
            finish_reason="stop",
        )

    def _flow_chunks(self, session: Session, fr: "flows.FlowResult") -> list[StreamChunk]:
        """Render a guided-flow turn (stream) as chunks to yield."""
        sid = session.session_id
        if fr.pending:
            pending = self._arm_flow_pending(session, fr)
            session_service.save(session)
            return [StreamChunk(type="confirm_required", session_id=sid,
                                content=fr.message, pending_action=pending,
                                finish_reason="confirm_required")]
        session.add_assistant(fr.message)
        session_service.save(session)
        return [
            StreamChunk(type="delta", session_id=sid, content=fr.message),
            StreamChunk(type="done", session_id=sid, content=fr.message,
                        suggestions=[Suggestion(**s) for s in fr.suggestions],
                        finish_reason="stop"),
        ]

    async def reply_stream(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
        system_prompt: str | None = None,
        request_headers: dict[str, str] | None = None,
        detail: str | None = None,
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
            request_id=get_request_id(),   # correlate turn_summary with chat_prompt/answer
        )
        session = session_service.get_or_create(session_id, user_id)
        session.add_user(user_message)
        session.metadata["last_user_message"] = user_message
        session.metadata["detail"] = (detail or "standard").lower()
        session_service.save(session)

        # Guided flow active (e.g. onboarding)? Advance it deterministically — no LLM.
        # Or a fresh intent (create user without full detail) may START a guided intake.
        if flows.is_active(session):
            fr = await flows.handle(session, user_message, request_headers)
            if fr is not None:
                for ch in self._flow_chunks(session, fr):
                    yield ch
                turn.finish("stop")
                return
        else:
            mr = await meta.handle(user_message, request_headers)
            started = mr or flows.maybe_start(session, user_message)
            if started is not None:
                for ch in self._flow_chunks(session, started):
                    yield ch
                turn.finish("stop")
                return

        system = self._ground(system_prompt or cfg.AGENT_SYSTEM_PROMPT)
        skill = skills.match(user_message)
        if skill:
            system = system + skills.grounding(skill)
        only = [skill.tool] if (skill and (is_mutation(skill.tool) or skill.focused)) else None
        slim = {skill.tool: skill.schema} if (skill and skill.schema) else None
        with turn.phase("retrieval"):
            tools = await tool_registry.select_tools(
                user_message, request_headers, extra=[skill.tool] if skill else None,
                only=only, slim=slim,
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
            fr = flows.on_decline(session)
            if fr is not None:
                for ch in self._flow_chunks(session, fr):
                    yield ch
                if turn:
                    turn.finish("stop")
                return
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
        suggestions: list[Suggestion] = []
        if result.success:
            lead, suggestions = self._post_confirm_flow(session, tool_name, tool_args, lead)
        session.add_assistant(lead)
        session_service.save(session)
        if turn:
            turn.finish("stop")
        yield StreamChunk(type="done", session_id=session_id, content=lead,
                          blocks=blocks, suggestions=suggestions, finish_reason="stop")

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

            # Flush the stream gate. If a tool was called, or we're about to nudge on a false-
            # start intent ("I will fetch…"), the held text is a non-answer → DROP it (don't
            # leak it as the reply). Only a genuine final answer flushes its tail.
            tail = gate.close(bool(tool_calls))

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
                    continue  # drop the tail — it's a false-start, not the answer
                if tail:
                    yield StreamChunk(type="delta", session_id=session_id, content=tail)
                sblocks = blocks_from_outputs(tool_outputs)
                text = (text or "").strip() or ("" if sblocks else _EMPTY_FALLBACK)
                session.add_assistant(text)
                self._log_answer(session_id, text, sblocks)
                session_service.save(session)
                if turn:
                    turn.finish("stop")
                yield StreamChunk(type="done", session_id=session_id, content=text,
                                  blocks=sblocks, finish_reason="stop")
                return

            # Gate skill mutations BEFORE creating: ask for missing required fields (info),
            # else block on format/uniqueness failure (error).
            verr = await self._validate_skill_calls(tool_calls, request_headers)
            if verr:
                vmsg, vlevel = verr
                fin = "needs_input" if vlevel == "info" else "validation_failed"
                session.add_assistant(vmsg)
                session_service.save(session)
                if turn:
                    turn.finish(fin)
                yield StreamChunk(type="done", session_id=session_id, content=vmsg,
                                  blocks=[UIBlock(type="notice", level=vlevel, text=vmsg)],
                                  finish_reason=fin)
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
                    # Reason over the data to answer the actual question (grounded, no tools),
                    # then attach the card. Falls back to a generic lead-in if synthesis fails.
                    answer = await self._synthesize_answer(session, tool_outputs) or lead_in(sblocks)
                    sblocks = await self._apply_detail(session, sblocks, tool_outputs, request_headers)
                    session.add_assistant(answer)
                    self._log_answer(session_id, answer, sblocks)
                    session_service.save(session)
                    if turn:
                        turn.finish("stop")
                    yield StreamChunk(type="done", session_id=session_id, content=answer,
                                      blocks=sblocks, finish_reason="stop")
                    return

            if pending_tc is not None:
                risk = (pending_tool.risk_level if pending_tool else "HIGH").upper()
                human = skills.confirm_summary(pending_tc["name"], pending_tc["input"])
                pending = PendingAction(
                    session_id=session_id,
                    tool_name=pending_tc["name"],
                    tool_args=pending_tc["input"],
                    risk_level=risk,
                    description=human,
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
                    content=human,
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

    async def _apply_detail(
        self, session: Session, blocks: list, tool_outputs: list[tuple[str, Any, bool]],
        request_headers: dict[str, str] | None,
    ) -> list:
        """Shape structured output to the requested verbosity:
        concise → drop the card (answer only) · standard → the card · detailed → card + related."""
        level = (session.metadata.get("detail") or "standard").lower()
        if level == "concise":
            return []
        if level == "detailed":
            return blocks + await self._detailed_extras(tool_outputs, request_headers)
        return blocks

    async def _detailed_extras(
        self, tool_outputs: list[tuple[str, Any, bool]], request_headers: dict[str, str] | None,
    ) -> list:
        """Detailed level: auto-pull closely-related data. For a user lookup, also fetch that
        user's applications + roles and render them as extra cards."""
        from ..services.ui_blocks import _unwrap
        extras: list = []
        for name, output, ok in tool_outputs:
            if not ok or name != "getUserByUserName_get":
                continue
            try:
                data = _unwrap(output)
            except Exception:  # noqa: BLE001
                data = output
            rec = data[0] if isinstance(data, list) and data else data
            uid = (rec.get("id") or rec.get("userId")) if isinstance(rec, dict) else None
            if not uid:
                continue
            for tool in ("getUserAppsByUserId_get", "getUserAppRoleByUserId_post"):
                try:
                    res = await tool_registry.execute(tool, {"userId": uid}, request_headers)
                    if getattr(res, "success", False):
                        extras += blocks_from_outputs([(tool, res.output, True)])
                except Exception:  # noqa: BLE001 — extras are best-effort
                    pass
        return extras

    def _log_answer(self, session_id: str, answer: str, blocks: Any, outcome: str = "stop") -> None:
        """Record the turn's answer snippet in the log ring (request_id auto-attaches) so the
        admin 'Activity' feed can show prompt→answer + errors for validity analysis."""
        try:
            log.bind(event="chat_answer", session_id=session_id,
                     answer=(answer or "")[:240], blocks=len(blocks or []),
                     outcome=outcome).info("chat answer")
        except Exception:  # noqa: BLE001 — telemetry must never break a turn
            pass

    async def _synthesize_answer(self, session: Session, tool_outputs: list[tuple[str, Any, bool]]) -> str:
        """Answer the user's question from the tool data with ONE focused, no-tools LLM call.
        This is far more reliable on a small model than re-running the agentic loop over a big
        result (which derails into "I need more information"). Grounded to GC360, concise."""
        import json as _json
        from ..services.ui_blocks import _unwrap, _coerce, _envelope_error

        question = next((m.content for m in reversed(session.messages) if m.role == "user"), "")
        ctx: list[str] = []
        total = errored = 0
        for name, output, success in tool_outputs:
            total += 1
            # Detect errors the SAME way the rendered card does (transport failure OR a GC
            # envelope carrying a business error) so the lead never contradicts the card:
            # a successful envelope with an empty "errors" field is DATA, not an error.
            coerced = output
            if success:
                try:
                    coerced = _coerce(output)
                except Exception:  # noqa: BLE001
                    coerced = output
            err = str(output) if not success else _envelope_error(coerced)
            if err:
                errored += 1
                ctx.append(f"{name}: ERROR — {str(err)[:300]}")
                continue
            data = coerced
            try:
                data = _unwrap(coerced)
            except Exception:  # noqa: BLE001
                pass
            n = len(data) if isinstance(data, list) else None
            body = _json.dumps(data, default=str)
            if len(body) > 6000:
                body = body[:6000] + " …(truncated)"
            ctx.append(f"{name}{f' returned {n} records' if n is not None else ''}: {body}")

        # Every tool errored → state the failure plainly (detail shows in the card); never let
        # the model report an error as "no matching …". A mix keeps the successful data.
        if total and errored == total:
            return ("I couldn't complete that — the request to the platform failed. "
                    "Please try again in a moment.")
        sys = (
            "You answer questions about the GovConnect 360 platform using ONLY the tool data "
            "provided below — never outside knowledge, never invented values. Answer the user's "
            "question directly in 1-2 sentences: for 'how many' give the count; for 'does X exist' "
            "/ 'is there' say yes or no first; for 'who' / 'which' name the specific record; for "
            "'what is' state the value. Do NOT list every field — the full records are shown to "
            "the user separately as a card. If the data is empty, say nothing matches. Only an "
            "entry explicitly marked 'ERROR' means a tool failed — for those say the request "
            "could not be completed (NEVER call an error 'no results'); ignore empty 'errors' "
            "fields inside otherwise-valid data, and answer from the data that did come back."
        )
        level = (session.metadata.get("detail") or "standard").lower()
        if level == "concise":
            sys += (" CONCISE MODE: there is NO card shown in this mode, so your text must carry "
                    "the information the user asked for. Give the specific values requested, "
                    "compactly — e.g. for 'account details for X' include the key fields (name, "
                    "id, email, account type, status) as a short inline list or one tight "
                    "sentence. Be brief, but NEVER reduce it to a bare 'exists'/yes-no when the "
                    "user asked for details; include the actual values from the data.")
        elif level == "detailed":
            sys += (" DETAILED MODE: give a fuller answer (2-4 sentences) — state the direct "
                    "answer, then note the most relevant supporting fields and any related "
                    "context from the data. Still never invent anything beyond the tool data.")
        msgs = [{"role": "user", "content": f"Question: {question}\n\nData:\n" + "\n".join(ctx)}]
        try:
            resp = await llm().complete(msgs, [], sys)
            return (resp.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.bind(func="synthesize").warning(f"synthesis failed: {exc}")
            return ""

    # ── Skill validation & dependency chains ──────────────────────────────────

    async def _validate_skill_calls(self, tool_calls, request_headers) -> tuple[str, str] | None:
        """Gate a skill mutation the model is about to call. Returns (message, level) or None:
          • ("…what would you like…", "info")  → required fields missing: ask, don't error.
          • ("… already exists.", "error")      → format/uniqueness failure: block the create.
        The required-field gate is deterministic — a small model can't skip past it by
        inventing partial values."""
        for tc in tool_calls or []:
            name = getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else None)
            args = getattr(tc, "input", None)
            if args is None and isinstance(tc, dict):
                args = tc.get("input", {})
            missing = skills.missing_required(name or "", args or {})
            if missing:
                return (skills.ask_for_required(name or "", missing), "info")
            errs = await skills.validate_inputs(name or "", args or {}, tool_registry.execute, request_headers)
            if errs:
                return (" ".join(errs), "error")
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
            # If declining an in-flow assign, resume onboarding instead of a dead end.
            fr = flows.on_decline(session)
            if fr is not None:
                return self._flow_response(session, fr)
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
        suggestions: list[Suggestion] = []
        if result.success:
            final_text, suggestions = self._post_confirm_flow(
                session, pending.tool_name, pending.tool_args, final_text)
        session.add_assistant(final_text)
        session_service.save(session)

        return ChatResponse(
            session_id=session_id,
            message_id=str(uuid.uuid4()),
            assistant_message=final_text,
            blocks=blocks,
            suggestions=suggestions,
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
                    description=skills.confirm_summary(tc.name, tc.input),
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
