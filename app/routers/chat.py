"""
routers/chat.py
───────────────
Chat endpoints:
  POST /api/chat              — non-streaming chat turn
  POST /api/chat/stream       — SSE streaming chat
  POST /api/chat/confirm      — confirm / reject a pending tool action
  POST /api/chat/prompt       — render a server-side prompt and chat
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.commons.flags import flags
from app.commons.logger import get_logger
from app.models.chat import (
    ChatRequest,
    ChatResponse,
    ConfirmRequest,
    PromptExecuteRequest,
)
from app.rbac.middleware import get_current_user, require_permission
from app.rbac.models import User
from app.rbac.permissions import Permissions
from app.services.chat_service import chat_service

log = get_logger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


def _request_headers(request: Request) -> dict[str, str]:
    """Pass-through selected headers to MCP tool execution."""
    headers: dict[str, str] = {}
    for h in ("Authorization", "X-API-KEY", "X-Forwarded-For"):
        if h in request.headers:
            headers[h] = request.headers[h]
    return headers


# ── Non-streaming ─────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=ChatResponse,
    summary="Send a message and receive a complete response",
)
async def chat(
    body: ChatRequest,
    request: Request,
    user: User = Depends(require_permission(Permissions.CHAT_SEND)),
) -> ChatResponse:
    log.bind(
        func="chat",
        session_id=body.session_id,
        user_id=user.id,
    ).info(f"Chat request: session={body.session_id}")

    try:
        return await chat_service.chat(
            session_id=body.session_id,
            user_message=body.message,
            user_id=user.id,
            system_prompt=body.system_prompt,
            request_headers=_request_headers(request),
        )
    except Exception as exc:
        log.exception(f"Chat error: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat failed: {exc}")


# ── Streaming ─────────────────────────────────────────────────────────────────

@router.post("/stream", summary="Stream a chat response via Server-Sent Events")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: User = Depends(require_permission(Permissions.CHAT_STREAM)),
):
    if not flags.streaming_enabled:
        raise HTTPException(status_code=400, detail="Streaming is disabled")

    log.bind(func="chat_stream", session_id=body.session_id, user_id=user.id).info(
        "Stream request"
    )

    async def event_generator():
        try:
            async for chunk in chat_service.chat_stream(
                session_id=body.session_id,
                user_message=body.message,
                user_id=user.id,
                system_prompt=body.system_prompt,
            ):
                yield {"data": chunk.model_dump_json()}
                if chunk.type == "done":
                    break
        except Exception as exc:
            log.exception(f"Stream error: {exc}")
            from app.models.chat import StreamChunk

            yield {
                "data": StreamChunk(
                    type="error",
                    session_id=body.session_id,
                    error=str(exc),
                ).model_dump_json()
            }

    return EventSourceResponse(event_generator())


# ── Confirm / reject pending action ───────────────────────────────────────────

@router.post(
    "/confirm",
    response_model=ChatResponse,
    summary="Confirm or reject a pending tool action",
)
async def confirm_action(
    body: ConfirmRequest,
    request: Request,
    user: User = Depends(require_permission(Permissions.CHAT_SEND)),
) -> ChatResponse:
    try:
        return await chat_service.confirm_action(
            session_id=body.session_id,
            action_id=body.action_id,
            confirmed=body.confirmed,
            request_headers=_request_headers(request),
        )
    except Exception as exc:
        log.exception(f"Confirm error: {exc}")
        raise HTTPException(status_code=500, detail=f"Confirm failed: {exc}")


# ── Prompt-driven chat ────────────────────────────────────────────────────────

@router.post(
    "/prompt",
    response_model=ChatResponse,
    summary="Execute a server-side MCP prompt and chat",
)
async def prompt_chat(
    body: PromptExecuteRequest,
    user: User = Depends(require_permission(Permissions.PROMPT_EXECUTE)),
) -> ChatResponse:
    from app.mcp.client import MCPClientError

    try:
        return await chat_service.execute_prompt(
            session_id=body.session_id,
            prompt_name=body.prompt_name,
            arguments=body.arguments,
            user_id=user.id,
        )
    except MCPClientError as exc:
        raise HTTPException(status_code=502, detail=f"MCP error: {exc}")
    except Exception as exc:
        log.exception(f"Prompt chat error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
