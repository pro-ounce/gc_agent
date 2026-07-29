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

import uuid
from typing import Any, AsyncIterator

from app.commons.config import cfg
from app.commons.flags import flags
from app.commons.logger import get_logger
from app.mcp.prompt_registry import prompt_registry
from app.mcp.tool_registry import tool_registry
from app.models.chat import ChatResponse, StreamChunk
from app.models.mcp import PendingAction
from app.models.session import Session
from app.services.llm_service import LLMResponse, ToolCall, llm
from app.services.session_service import session_service

log = get_logger(__name__)


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
        # Tool-RAG: pick only tools relevant to this query (falls back to all — see select_tools).
        tools = await tool_registry.select_tools(user_message, request_headers)
        tool_calls_made: list[str] = []

        llm_response: LLMResponse | None = None

        for iteration in range(cfg.LLM_MAX_ITERATIONS):
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

            # Process tool calls
            pending = await self._process_tool_calls(
                session=session,
                tool_calls=llm_response.tool_calls,
                llm_text=llm_response.text,
                tool_calls_made=tool_calls_made,
                request_headers=request_headers,
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

        # Final assistant message
        final_text = llm_response.text if llm_response else "I was unable to process your request."
        session.add_assistant(final_text)
        session_service.save(session)

        return ChatResponse(
            session_id=session_id,
            message_id=str(uuid.uuid4()),
            assistant_message=final_text,
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
        session = session_service.get_or_create(session_id, user_id)
        session.add_user(user_message)
        session.metadata["last_user_message"] = user_message
        session_service.save(session)
        system = system_prompt or cfg.AGENT_SYSTEM_PROMPT
        tools = await tool_registry.select_tools(user_message, request_headers)
        async for chunk in self._stream_loop(session, tools, system, request_headers):
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
        else:
            yield StreamChunk(type="tool_use", session_id=session_id, content=f"Running {tool_name}…")
            result = await tool_registry.execute(tool_name, tool_args, request_headers)
            output = str(result.output) if result.success else f"Error: {result.error}"
            session.add_tool_result(tc_id, tool_name, output)
        session_service.save(session)

        last_user = session.metadata.get("last_user_message", "")
        tools = await tool_registry.select_tools(last_user, request_headers)
        async for chunk in self._stream_loop(session, tools, cfg.AGENT_SYSTEM_PROMPT, request_headers):
            yield chunk

    async def _stream_loop(
        self,
        session: Session,
        tools: list[dict[str, Any]],
        system: str,
        request_headers: dict[str, str] | None,
    ) -> AsyncIterator[StreamChunk]:
        """Shared agentic loop for reply_stream / confirm_stream."""
        from app.services.llm_service import _extract_text_tool_calls

        session_id = session.session_id
        for _ in range(cfg.LLM_MAX_ITERATIONS):
            messages = session.to_llm_messages()
            text = ""
            tool_calls: list[dict[str, Any]] = []
            try:
                async for kind, payload in llm().stream_chat(messages, tools, system):
                    if kind == "delta":
                        text += payload
                        yield StreamChunk(type="delta", session_id=session_id, content=payload)
                    elif kind == "tool_calls":
                        tool_calls = payload
            except Exception as exc:  # noqa: BLE001
                log.exception(f"reply_stream error: {exc}")
                yield StreamChunk(type="error", session_id=session_id, error=str(exc))
                return

            # Recover a tool call the model emitted as text (fallback).
            if not tool_calls and text:
                recovered, cleaned = _extract_text_tool_calls(text)
                if recovered:
                    tool_calls = [{"id": f"tc_{c.name}", "name": c.name, "input": c.input} for c in recovered]
                    text = cleaned

            if not tool_calls:
                session.add_assistant(text)
                session_service.save(session)
                yield StreamChunk(type="done", session_id=session_id, content=text, finish_reason="stop")
                return

            # Run read-only tools inline; STOP at the first mutation and ask to confirm.
            executed: list[tuple[dict[str, Any], str]] = []
            pending_tc: dict[str, Any] | None = None
            for tc in tool_calls:
                if (
                    flags.tool_risk_confirmation
                    and tool_registry.is_mutation(tc["name"])
                    and not session.metadata.get("bypass_confirmation")
                ):
                    pending_tc = tc
                    break
                yield StreamChunk(type="tool_use", session_id=session_id, content=f"Running {tc['name']}…")
                result = await tool_registry.execute(tc["name"], tc["input"], request_headers)
                output = str(result.output) if result.success else f"Error: {result.error}"
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

            if pending_tc is not None:
                pending = PendingAction(
                    session_id=session_id,
                    tool_name=pending_tc["name"],
                    tool_args=pending_tc["input"],
                    risk_level="HIGH",
                    description=f"Runs '{pending_tc['name']}', which changes data. Confirm to proceed.",
                )
                session.pending_action_id = pending.id
                stored = pending.model_dump()
                stored["_tc_id"] = pending_tc["id"]   # tie the confirm back to this tool_call
                session.metadata.setdefault("pending_actions", {})[pending.id] = stored
                session_service.save(session)
                yield StreamChunk(
                    type="confirm_required",
                    session_id=session_id,
                    content=f"⚠️ This will run **{pending_tc['name']}** with {pending_tc['input']}. Confirm to proceed.",
                    pending_action=pending,
                )
                return

            session_service.save(session)

        yield StreamChunk(type="done", session_id=session_id, content="", finish_reason="max_iterations")

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
        session_service.save(session)

        # Re-run LLM with tool result to generate the final response
        system = cfg.AGENT_SYSTEM_PROMPT
        tools_schema = await tool_registry.as_tools(request_headers)
        llm_response = await llm().complete(session.to_llm_messages(), tools_schema, system)
        final_text = llm_response.text or output_text

        session.add_assistant(final_text)
        session_service.save(session)

        return ChatResponse(
            session_id=session_id,
            message_id=str(uuid.uuid4()),
            assistant_message=final_text,
            tool_calls_made=[pending.tool_name],
            model=llm_response.model,
            usage=llm_response.usage,
            finish_reason=llm_response.finish_reason,
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
    ) -> PendingAction | None:
        """
        Execute tool calls sequentially.
        Returns a PendingAction if any tool requires confirmation, else None.
        """
        import json

        for tc in tool_calls:
            tool = await tool_registry.get_tool(tc.name)

            # Risk gate — pause and ask user before executing
            if (
                flags.tool_risk_confirmation
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
