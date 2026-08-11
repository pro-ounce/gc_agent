"""
models/mcp.py
─────────────
Pydantic models for MCP tools, prompts, and pending actions.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ToolParameter(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None


class Tool(BaseModel):
    name: str
    description: str = ""
    risk_level: str = "MEDIUM"          # LOW | MEDIUM | HIGH | CRITICAL
    requires_confirmation: bool = False
    parameters: list[ToolParameter] = []
    input_schema: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)  # Original payload from MCP server

    @classmethod
    def from_mcp_payload(cls, payload: dict[str, Any]) -> "Tool":
        """
        Parse a Spring Boot MCP tool definition.

        Server returns flat near-OpenAI format:
          { "type":"function", "name":"opId_httpMethod",
            "description":"... Risk level: HIGH. ...",
            "parameters": { "type":"object", "properties":{...}, "required":[...] } }

        Risk level and confirmation requirement are embedded in the description
        by the SwaggerLoaderService.
        """
        desc = payload.get("description", "")
        # Prefer MCP's STRUCTURED metadata — SwaggerLoaderService now emits `risk` and
        # `requiresConfirmation` as fields on the tool. Fall back to parsing the description
        # text (older MCP builds embed "Risk level: X" / "Requires user confirmation").
        risk = str(payload.get("risk") or _extract_risk(desc) or "MEDIUM").upper()
        if "requiresConfirmation" in payload:
            confirm = bool(payload.get("requiresConfirmation"))
        else:
            confirm = (
                "requires user confirmation" in desc.lower()
                or _needs_confirmation(payload, risk)
            )
        # Parameters schema uses "parameters" key (not "inputSchema")
        param_schema = payload.get("parameters") or payload.get("inputSchema") or {}
        return cls(
            name=payload.get("name", ""),
            description=desc,
            risk_level=risk,
            requires_confirmation=confirm,
            parameters=_parse_parameters(param_schema),
            input_schema=param_schema,
            raw=payload,
        )


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    duration_ms: float | None = None


class PromptArgument(BaseModel):
    name: str
    description: str = ""
    required: bool = False


class Prompt(BaseModel):
    name: str
    description: str = ""
    arguments: list[PromptArgument] = []
    raw: dict[str, Any] = Field(default_factory=dict)


class PendingAction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "MEDIUM"
    description: str = ""

    model_config = {"json_encoders": {}}


# ── Risk helpers ──────────────────────────────────────────────────────────────

import re

_RISK_RE = re.compile(r"Risk\s+level[:\s]+(LOW|MEDIUM|HIGH|CRITICAL)", re.IGNORECASE)
_CONFIRM_KEYWORDS = frozenset(
    {"delete", "remove", "update", "modify", "create", "write", "execute", "deploy", "reset"}
)


def _extract_risk(description: str) -> str:
    m = _RISK_RE.search(description)
    return m.group(1).upper() if m else "MEDIUM"


def _needs_confirmation(payload: dict[str, Any], risk: str) -> bool:
    if risk == "HIGH":
        return True
    if risk == "MEDIUM":
        text = (payload.get("name", "") + " " + payload.get("description", "")).lower()
        return any(kw in text for kw in _CONFIRM_KEYWORDS)
    return False


def _parse_parameters(schema: dict[str, Any]) -> list[ToolParameter]:
    """Parse a JSON Schema 'parameters' or 'inputSchema' object into ToolParameter list."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    params: list[ToolParameter] = []
    for name, spec in props.items():
        params.append(
            ToolParameter(
                name=name,
                type=spec.get("type", "string"),
                description=spec.get("description", ""),
                required=name in required,
                enum=spec.get("enum"),
            )
        )
    return params
