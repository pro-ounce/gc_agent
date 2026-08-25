"""
routers/platform.py
────────────────────
GC-platform-facing API for the AI service. Served under the `/ai-service`
context so the gateway's dynamic route rewrites:

    FE  POST /api/ai/{agent}/chat
      → gateway  /api/ai/(seg)  →  http://localhost:17024/ai-service/(seg)
      → here     POST /ai-service/{agent}/chat

Contract (matches the deployed FE): request { "message": "..." } (plaintext),
response = platform ApiResponse envelope, PLAINTEXT with header Encrypted:false.
Auth is gateway-vouched: the gateway forwards X-INT-TKN, verified by the RBAC
middleware (PLATFORM_AUTH_MODE=gateway); that same token is forwarded to MCP.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..agents.registry import STRICT_GROUNDING_INSTRUCTION, get_agent, list_agents
from ..commons.config import cfg
from ..commons.flags import flags
from ..services import runtime_config
from ..commons.logger import get_logger
from ..models.platform import ApiResponse
from ..rbac.middleware import get_current_user
from ..rbac.models import User
from ..services.chat_service import chat_service
from ..services.session_service import session_service
from ..mcp.tool_registry import tool_registry

log = get_logger(__name__)
router = APIRouter(prefix="/ai-service", tags=["platform"])


class AgentQuery(BaseModel):
    """Mirrors administration's ChatAgentRequest so the admin ChatAgentService can
    proxy straight through to this 'real agent' without field remapping."""

    question: str = Field(..., min_length=1, max_length=4000, description="User message / question")
    context: str | None = Field(
        "", max_length=8000, description="Optional grounding context prepended to the prompt"
    )
    # Absent/null → new conversation (per user+agent); set → continue that conversation.
    sessionId: str | None = None
    # ChatAgentScope: GLOBAL = platform-wide knowledge (default when null);
    # APPLICATION = scoped to the calling app identified by appCode.
    scope: str | None = "GLOBAL"
    # Application code, meaningful only for APPLICATION scope (mirrors ChatAgentRequest.appCode).
    appCode: str | None = Field(None, max_length=100)


def _resolve_app_code(scope: str, app_code: str | None) -> str | None:
    """Mirror administration's resolveAppCode: appCode only applies to APPLICATION scope."""
    return app_code if scope == "APPLICATION" else None


def _user_context(user: "User | None") -> str:
    """Tell the model who is asking, so 'me/my' self-references resolve to the real
    user (requires AUTH_ENABLED=true, else the user is anonymous and we say nothing)."""
    if user is None:
        return ""
    uid = str(getattr(user, "id", "") or "")
    uname = getattr(user, "username", None)
    if not uname or uid in ("", "anonymous") or uname == "anonymous":
        return ""
    return (
        f"\n\nThe current user (the person you are talking to) is username={uname!r}, "
        f"userId={uid!r}. When they refer to themselves ('me', 'my', 'I'), use these values "
        f"as tool parameters — e.g. pass userName={uname!r} to a by-username lookup, or "
        f"userId={uid!r} to a by-id lookup. Never ask them for their own username or id."
    )


def _ground(
    system_prompt: str,
    context: str | None,
    scope: str,
    app_code: str | None,
    user: "User | None" = None,
) -> str:
    """Assemble the final system prompt: base + strict-grounding + current-user + context."""
    if runtime_config.get_bool("AGENT_STRICT_GROUNDING"):
        system_prompt = f"{system_prompt}{STRICT_GROUNDING_INSTRUCTION}"
    system_prompt = f"{system_prompt}{_user_context(user)}"
    grounding = (context or "").strip()
    if scope == "APPLICATION" and app_code:
        grounding = f"{grounding}\nApplication scope: {app_code}".strip()
    return f"{system_prompt}\n\nCurrent context: {grounding}" if grounding else system_prompt


def _forward_headers(request: Request) -> dict[str, str]:
    """Pass gateway/user context downstream to MCP → gateway → domain services."""
    forward = (
        cfg.GC_INTERNAL_HEADER,   # X-INT-TKN — gateway internal token
        cfg.GC_ROLE_HEADER,       # X-AR-KEY  — role
        "Authorization",
        "X-Trace-Id", "X-Time-Zone", "X-Date-Time",
        "X-Selected-App", "X-Selected-Role",
    )
    return {h: request.headers[h] for h in forward if h in request.headers}


@router.get("/{agent}/me", summary="Current user identity for the UI greeting")
async def agent_me(
    agent: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> ApiResponse:
    """Lightweight identity for a personalized greeting. Best-effort first/full name from
    the caller's own profile; falls back to the username."""
    first = full = None
    try:
        res = await tool_registry.execute("getUserProfile_get", {}, _forward_headers(request))
        data = res.output.get("data") if getattr(res, "success", False) and isinstance(res.output, dict) else None
        if isinstance(data, dict):
            first = (str(data.get("firstName") or "").strip()) or None
            full = (str(data.get("fullName") or "").strip()) or None
    except Exception:  # noqa: BLE001 — a greeting must never fail the widget
        pass
    display = first or full or (user.username if user.username not in ("", "anonymous") else "there")
    return ApiResponse.ok(message="ok", data={
        "userName": user.username, "firstName": first, "fullName": full, "displayName": display,
    })


@router.get("/{agent}/resume", summary="Resume hint — the user's last question, to pick up where they left off")
async def agent_resume(
    agent: str,
    request: Request,
    sessionId: str = "",
    user: User = Depends(get_current_user),
) -> ApiResponse:
    """Look at the caller's most recent conversation and return their last question, so the UI
    can offer a 'pick up where you left off' suggestion."""
    sess = session_service.get(sessionId) if sessionId else None
    if sess is None:
        listed = session_service.list_sessions(user.id) or []
        listed.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
        for item in listed:
            s = session_service.get(item.get("session_id", ""))
            if s and any(m.role == "user" for m in s.messages):
                sess = s
                break
    if not sess or not sess.messages:
        return ApiResponse.ok(message="ok", data={"hasHistory": False})
    last_q = next((m.content for m in reversed(sess.messages) if m.role == "user"), None)
    return ApiResponse.ok(message="ok", data={
        "hasHistory": bool(last_q), "sessionId": sess.session_id,
        "lastQuestion": last_q, "messageCount": len(sess.messages),
    })


@router.post("/{agent}/reply", summary="Ask an AI agent and get a reply")
async def agent_reply(
    agent: str,
    body: AgentQuery,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
) -> ApiResponse:
    response.headers["Encrypted"] = "false"

    spec = get_agent(agent)
    if spec is None:
        return ApiResponse.fail(
            f"Unknown agent '{agent}'. Available: {', '.join(list_agents())}",
            status_code=404,
        )

    # sessionId from the FE (multi-turn / "new chat"), else one conversation per user+agent.
    session_id = body.sessionId or f"{agent}:{user.id}"

    scope = (body.scope or "GLOBAL").upper()
    app_code = _resolve_app_code(scope, body.appCode)
    system_prompt = _ground(spec.system_prompt, body.context, scope, app_code, user)

    log.bind(
        func="agent_reply", agent=agent, session_id=session_id, user_id=user.id,
        scope=scope, app_code=app_code,
    ).info("Platform agent request")

    try:
        result = await chat_service.chat(
            session_id=session_id,
            user_message=body.question,
            user_id=user.id,
            system_prompt=system_prompt,
            request_headers=_forward_headers(request),
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean envelope, never a 500 HTML
        log.exception(f"agent_reply failed: {exc}")
        return ApiResponse.fail(f"Agent request failed: {exc}")

    # Field names mirror administration's ChatAgentReply so the admin facade maps it 1:1.
    # `stub` is False — this is the real agent, not administration's placeholder.
    return ApiResponse.ok(
        message="Chat agent reply generated successfully",
        data={
            "reply": result.assistant_message,
            "blocks": [b.model_dump() for b in result.blocks],
            "sessionId": session_id,
            "agentId": agent,
            "scope": scope,
            "appCode": app_code,
            "stub": False,
            "toolsUsed": result.tool_calls_made,
        },
    )


@router.post("/{agent}/reply/stream", summary="Stream an AI agent reply via SSE")
async def agent_reply_stream(
    agent: str,
    body: AgentQuery,
    request: Request,
    user: User = Depends(get_current_user),
) -> EventSourceResponse:
    if not flags.streaming_enabled:
        raise HTTPException(status_code=400, detail="Streaming is disabled")

    spec = get_agent(agent)
    session_id = body.sessionId or f"{agent}:{user.id}"
    scope = (body.scope or "GLOBAL").upper()
    app_code = _resolve_app_code(scope, body.appCode)
    system_prompt = _ground(spec.system_prompt, body.context, scope, app_code, user) if spec else ""

    async def gen():
        if spec is None:
            yield {"data": ApiResponse.fail(
                f"Unknown agent '{agent}'. Available: {', '.join(list_agents())}", 404
            ).model_dump_json()}
            return
        async for chunk in chat_service.reply_stream(
            session_id=session_id,
            user_message=body.question,
            user_id=user.id,
            system_prompt=system_prompt,
            request_headers=_forward_headers(request),
        ):
            yield {"data": chunk.model_dump_json()}
            if chunk.type in ("done", "error"):
                break

    # Headers that keep SSE flowing through proxies (Apache/gateway/nginx/ALB).
    return EventSourceResponse(
        gen(),
        headers={
            "Encrypted": "false",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class AgentConfirm(BaseModel):
    """Approve or decline a mutating action the stream paused on (confirm_required)."""

    sessionId: str = Field(..., description="Session the pending action belongs to")
    actionId: str = Field(..., description="pending_action.id from the confirm_required chunk")
    confirmed: bool = Field(..., description="true = run it, false = skip it")


@router.post("/{agent}/confirm/stream", summary="Confirm/decline a paused mutating action (SSE)")
async def agent_confirm_stream(
    agent: str,
    body: AgentConfirm,
    request: Request,
    user: User = Depends(get_current_user),
) -> EventSourceResponse:
    if not flags.streaming_enabled:
        raise HTTPException(status_code=400, detail="Streaming is disabled")

    async def gen():
        async for chunk in chat_service.confirm_stream(
            session_id=body.sessionId,
            action_id=body.actionId,
            confirmed=body.confirmed,
            request_headers=_forward_headers(request),
        ):
            yield {"data": chunk.model_dump_json()}
            if chunk.type in ("done", "error"):
                break

    return EventSourceResponse(
        gen(),
        headers={"Encrypted": "false", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
