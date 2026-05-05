"""
services/chat_service.py
────────────────────────
Core agentic loop that orchestrates:
  1. Session history management
  2. Tool discovery from MCP registry
  3. LLM invocation (Claude or OLLAMA)
  4. Risk-aware tool execution / pending-action gating
  5. Iterative tool-call → result → LLM loop (up to MAX_ITERATIONS)

Both blocking and streaming chat modes are supported.
"""
from __future__ import annotations

import json
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
        session.add_message("user", user_message)
        session_service.save(session)

        system = system_prompt or cfg.AGENT_SYSTEM_PROMPT
        tools = await tool_registry.as_anthropic_tools()
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
                tool_calls_made=tool_calls_made,
                request_headers=request_headers,
            )

            if pending:
                # Save session with pending action, return to user for confirmation
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
        session.add_message("assistant", final_text)
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
        session.add_message("user", user_message)
        session_service.save(session)

        system = system_prompt or cfg.AGENT_SYSTEM_PROMPT
        tools = await tool_registry.as_anthropic_tools()
        messages = session.to_llm_messages()

        full_text = ""
        async for chunk in llm().stream(messages, tools, system):
            full_text += chunk
            yield StreamChunk(type="delta", session_id=session_id, content=chunk)

        session.add_message("assistant", full_text)
        session_service.save(session)

        yield StreamChunk(
            type="done",
            session_id=session_id,
            content=full_text,
            finish_reason="stop",
        )

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

        # Load the pending action from the session metadata
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
            session.add_message(
                "tool_result",
                "User cancelled this operation.",
                tool_use_id=action_id,
                tool_name=pending.tool_name,
            )
            session_service.save(session)
            return ChatResponse(
                session_id=session_id,
                message_id=str(uuid.uuid4()),
                assistant_message="Action cancelled by user.",
                finish_reason="cancelled",
            )

        # Execute the confirmed tool
        result = await tool_registry.execute(
            pending.tool_name, pending.tool_args, request_headers
        )
        output_text = str(result.output) if result.success else f"Error: {result.error}"

        session.add_message(
            "tool_result",
            output_text,
            tool_use_id=action_id,
            tool_name=pending.tool_name,
        )
        session_service.save(session)

        # Re-run LLM to generate final response with tool result
        system = cfg.AGENT_SYSTEM_PROMPT
        tools_schema = await tool_registry.as_anthropic_tools()
        llm_response = await llm().complete(session.to_llm_messages(), tools_schema, system)
        final_text = llm_response.text or output_text

        session.add_message("assistant", final_text)
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
        return await self.chat(
            session_id=session_id,
            user_message=rendered,
            user_id=user_id,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _process_tool_calls(
        self,
        session: Session,
        tool_calls: list[ToolCall],
        tool_calls_made: list[str],
        request_headers: dict[str, str] | None,
    ) -> PendingAction | None:
        """
        Execute tool calls sequentially.
        Returns a PendingAction if any tool requires confirmation, else None.
        """
        for tc in tool_calls:
            tool = await tool_registry.get_tool(tc.name)

            # Risk gate
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
                # Persist tool_use block in session so LLM history is correct
                session.add_message(
                    "tool_use",
                    json.dumps(tc.input),
                    tool_use_id=pending.id,
                    tool_name=tc.name,
                )
                session.metadata.setdefault("pending_actions", {})[pending.id] = (
                    pending.model_dump()
                )
                return pending

            # Execute immediately
            log.bind(func="tool_call", tool=tc.name, session_id=session.session_id).info(
                f"Executing tool '{tc.name}'"
            )
            result = await tool_registry.execute(tc.name, tc.input, request_headers)
            tool_calls_made.append(tc.name)
            output = str(result.output) if result.success else f"Error: {result.error}"

            # Record in session history (tool_use + tool_result pair)
            session.add_message(
                "tool_use",
                json.dumps(tc.input),
                tool_use_id=tc.id,
                tool_name=tc.name,
            )
            session.add_message(
                "tool_result",
                output,
                tool_use_id=tc.id,
                tool_name=tc.name,
            )

        return None


chat_service = ChatService()
