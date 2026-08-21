"""
routers/prompts.py
──────────────────
Endpoints for browsing and executing MCP prompts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..mcp.prompt_registry import prompt_registry
from ..rbac.middleware import require_permission
from ..rbac.permissions import Permissions

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptExecuteRequest(BaseModel):
    arguments: dict[str, str] = {}


@router.get("", summary="List all MCP prompts")
async def list_prompts(
    _: object = Depends(require_permission(Permissions.PROMPT_LIST)),
) -> dict:
    prompts = await prompt_registry.get_prompts()
    return {
        "total": len(prompts),
        "prompts": [
            {
                "name": p.name,
                "description": p.description,
                "arguments": [a.model_dump() for a in p.arguments],
            }
            for p in prompts
        ],
    }


@router.get("/{prompt_name}", summary="Get a single prompt definition")
async def get_prompt(
    prompt_name: str,
    _: object = Depends(require_permission(Permissions.PROMPT_LIST)),
) -> dict:
    prompt = await prompt_registry.get_prompt(prompt_name)
    if not prompt:
        raise HTTPException(status_code=404, detail=f"Prompt '{prompt_name}' not found")
    return {
        "name": prompt.name,
        "description": prompt.description,
        "arguments": [a.model_dump() for a in prompt.arguments],
    }


@router.post("/{prompt_name}/execute", summary="Render a prompt server-side")
async def execute_prompt(
    prompt_name: str,
    body: PromptExecuteRequest,
    _: object = Depends(require_permission(Permissions.PROMPT_EXECUTE)),
) -> dict:
    from ..mcp.client import MCPClientError

    try:
        text = await prompt_registry.execute_prompt(prompt_name, body.arguments)
        return {"prompt": prompt_name, "text": text}
    except MCPClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
