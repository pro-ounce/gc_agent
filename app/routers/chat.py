"""
routers/chat.py
───────────────
Chat endpoints:
  POST /api/chat              — non-streaming chat turn
  POST /api/chat/confirm      — confirm / reject a pending tool action
  POST /api/chat/prompt       — render a server-side prompt and chat

Streaming lives on the platform surface the widget/gateway use, NOT here:
  POST /ai-service/{agent}/reply/stream  → chat_service.reply_stream
The former POST /api/chat/stream (bare, un-routed) is archived — see the note below.
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from ..commons import metrics as M
from ..commons.config import cfg
from ..commons.logger import get_logger
from ..models.chat import (
    ChatRequest,
    ChatResponse,
    ConfirmRequest,
    PromptExecuteRequest,
)
from ..rbac.middleware import get_current_user, require_permission
from ..rbac.models import User
from ..rbac.permissions import Permissions
from ..services.chat_service import chat_service

log = get_logger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


def _request_headers(request: Request) -> dict[str, str]:
    """
    Pass-through headers to MCP tool execution. Includes the gc gateway's internal
    token (X-INT-TKN) and role/tracing context so MCP can act on behalf of the user.
    """
    forward = (
        cfg.GC_INTERNAL_HEADER,  # X-INT-TKN — gateway internal token (gateway mode)
        cfg.GC_ROLE_HEADER,      # X-AR-KEY  — role
        "Authorization", "X-API-KEY", "X-Forwarded-For",
        "X-TRACE-ID", "X-Time-Zone", "X-Date-Time",
    )
    headers: dict[str, str] = {}
    for h in forward:
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
        result = await chat_service.chat(
            session_id=body.session_id,
            user_message=body.message,
            user_id=user.id,
            system_prompt=body.system_prompt,
            request_headers=_request_headers(request),
            detail=body.detail,
        )
        M.chat_requests_total.labels("sync", "success").inc()
        return result
    except Exception as exc:
        M.chat_requests_total.labels("sync", "error").inc()
        log.exception(f"Chat error: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat failed: {exc}")


# ── Streaming ─────────────────────────────────────────────────────────────────
# ARCHIVED: the old `POST /api/chat/stream` route (backed by chat_service.chat_stream,
# a bare LLM passthrough with NO guided-flow / skill / confirmation routing) has been
# removed — nothing consumed it, and it silently produced un-routed LLM answers.
# The real streaming surface is the platform endpoint the widget/gateway uses:
#   POST /ai-service/{agent}/reply/stream  → chat_service.reply_stream (full routing)
# Do NOT re-add a streaming route here that bypasses reply_stream's flow routing.


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
    from ..mcp.client import MCPClientError

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
