import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


JAVA_MCP_BASE_URL = os.getenv("JAVA_MCP_BASE_URL", "http://localhost:19170/mcp-service/mcp").rstrip("/")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
ROOT_PATH = os.getenv("ROOT_PATH", "")

TOOLS_CACHE_TTL_SECONDS = int(os.getenv("TOOLS_CACHE_TTL_SECONDS", "600"))


class ChatRequest(BaseModel):
    session_id: str
    message: str


class PendingAction(BaseModel):
    id: str
    tool: str
    args: Dict[str, Any]
    risk: str


class ChatResponse(BaseModel):
    session_id: str
    assistant_message: str
    pending_action: Optional[PendingAction] = None


class ConfirmRequest(BaseModel):
    session_id: str
    pending_action_id: str
    confirm: bool


class ConfirmResponse(BaseModel):
    session_id: str
    assistant_message: str
    tool_result: Optional[Dict[str, Any]] = None


app = FastAPI(root_path=ROOT_PATH)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "X-API-KEY", "Content-Type"],
)


_session_messages: Dict[str, List[Dict[str, str]]] = {}
_pending_actions: Dict[str, PendingAction] = {}
_tools_cache: Dict[str, Any] = {"loaded_at": 0.0, "tools": []}


def _extract_risk(description: str) -> str:
    m = re.search(r"Risk level:\s*(LOW|MEDIUM|HIGH)", description, re.IGNORECASE)
    if not m:
        return "MEDIUM"
    return m.group(1).upper()


def _needs_confirmation(tool: Dict[str, Any]) -> bool:
    desc = str(tool.get("description", ""))
    risk = _extract_risk(desc)
    if risk == "HIGH":
        return True

    if risk == "MEDIUM":
        name = str(tool.get("name", "")).lower()
        desc_l = desc.lower()
        keywords = [
            "lock",
            "unlock",
            "disable",
            "enable",
            "delete",
            "update",
            "create",
            "reset",
            "terminate",
            "revoke",
        ]
        return any(k in name or k in desc_l for k in keywords)

    return False


def _headers_for_java(authorization: Optional[str], x_api_key: Optional[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if authorization:
        headers["Authorization"] = authorization
    if x_api_key:
        headers["X-API-KEY"] = x_api_key
    return headers


async def _load_tools(authorization: Optional[str], x_api_key: Optional[str]) -> List[Dict[str, Any]]:
    now = time.time()
    if (
        _tools_cache["tools"]
        and now - float(_tools_cache["loaded_at"]) < TOOLS_CACHE_TTL_SECONDS
    ):
        return _tools_cache["tools"]

    url = f"{JAVA_MCP_BASE_URL}/tools"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=_headers_for_java(authorization, x_api_key))

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail={"java_status": r.status_code, "body": r.text})

    payload = r.json()
    tools = payload.get("result", [])
    if not isinstance(tools, list):
        tools = []

    _tools_cache["loaded_at"] = now
    _tools_cache["tools"] = tools
    return tools


async def _call_java_tool(
    tool_name: str,
    args: Dict[str, Any],
    authorization: Optional[str],
    x_api_key: Optional[str],
) -> Dict[str, Any]:
    url = f"{JAVA_MCP_BASE_URL}"
    body = {"jsonrpc": "2.0", "id": "1", "method": tool_name, "params": args}

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=_headers_for_java(authorization, x_api_key), json=body)

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail={"java_status": r.status_code, "body": r.text})

    return r.json()


async def _ollama_chat(messages: List[Dict[str, str]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    prompt_tools = [
        {
            "name": t.get("name"),
            "description": t.get("description"),
            "parameters": t.get("parameters"),
        }
        for t in tools
    ]

    system = (
        "You are an agent that can call backend tools. "
        "When you need a tool, output ONLY valid JSON like: "
        "{\"tool\":\"tool_name\",\"arguments\":{...}}. "
        "When you can answer without tools, output ONLY valid JSON like: {\"final\":\"...\"}. "
        "Never output anything except JSON. "
        "Tools available: "
        + str(prompt_tools)
    )

    ollama_messages = [{"role": "system", "content": system}] + messages

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": ollama_messages, "stream": False},
        )

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail={"ollama_status": r.status_code, "body": r.text})

    payload = r.json()
    content = payload.get("message", {}).get("content", "")
    try:
        return httpx.Response(200, content=content).json()
    except Exception:
        raise HTTPException(status_code=500, detail={"error": "LLM did not return valid JSON", "raw": content})


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/tools")
async def tools(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
) -> Dict[str, Any]:
    t = await _load_tools(authorization, x_api_key)
    return {"java_mcp_base_url": JAVA_MCP_BASE_URL, "count": len(t), "tools": t}


@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
) -> ChatResponse:
    if not authorization or not x_api_key:
        raise HTTPException(status_code=400, detail="Missing Authorization or X-API-KEY")

    tools_list = await _load_tools(authorization, x_api_key)

    history = _session_messages.setdefault(req.session_id, [])
    history.append({"role": "user", "content": req.message})

    decision = await _ollama_chat(history[-20:], tools_list)

    if "final" in decision:
        assistant_text = str(decision.get("final", ""))
        history.append({"role": "assistant", "content": assistant_text})
        return ChatResponse(session_id=req.session_id, assistant_message=assistant_text)

    tool_name = decision.get("tool")
    args = decision.get("arguments")
    if not tool_name or not isinstance(args, dict):
        raise HTTPException(status_code=500, detail={"error": "Invalid tool decision", "decision": decision})

    tool = next((t for t in tools_list if t.get("name") == tool_name), None)
    if not tool:
        raise HTTPException(status_code=400, detail={"error": "Unknown tool", "tool": tool_name})

    risk = _extract_risk(str(tool.get("description", "")))

    if _needs_confirmation(tool):
        pending_id = str(uuid.uuid4())
        pending = PendingAction(id=pending_id, tool=tool_name, args=args, risk=risk)
        _pending_actions[pending_id] = pending

        msg = f"This action requires confirmation (risk: {risk}). Confirm to execute tool '{tool_name}'."
        history.append({"role": "assistant", "content": msg})
        return ChatResponse(session_id=req.session_id, assistant_message=msg, pending_action=pending)

    java_result = await _call_java_tool(tool_name, args, authorization, x_api_key)
    assistant_text = f"Tool '{tool_name}' executed successfully."
    history.append({"role": "assistant", "content": assistant_text})
    return ChatResponse(session_id=req.session_id, assistant_message=assistant_text)


@app.post("/confirm", response_model=ConfirmResponse)
async def confirm(
    req: ConfirmRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
) -> ConfirmResponse:
    if not authorization or not x_api_key:
        raise HTTPException(status_code=400, detail="Missing Authorization or X-API-KEY")

    pending = _pending_actions.get(req.pending_action_id)
    if not pending or req.session_id not in _session_messages:
        raise HTTPException(status_code=404, detail="Pending action not found")

    if not req.confirm:
        _pending_actions.pop(req.pending_action_id, None)
        msg = "Cancelled."
        _session_messages[req.session_id].append({"role": "assistant", "content": msg})
        return ConfirmResponse(session_id=req.session_id, assistant_message=msg)

    java_result = await _call_java_tool(pending.tool, pending.args, authorization, x_api_key)
    _pending_actions.pop(req.pending_action_id, None)

    msg = f"Confirmed and executed tool '{pending.tool}'."
    _session_messages[req.session_id].append({"role": "assistant", "content": msg})
    return ConfirmResponse(session_id=req.session_id, assistant_message=msg, tool_result=java_result)
