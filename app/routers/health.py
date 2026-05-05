"""
routers/health.py
─────────────────
Health & readiness probes.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.commons.config import cfg
from app.mcp.client import mcp_client

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    env: str
    mcp: dict


@router.get("/health", response_model=HealthResponse, summary="Health probe")
@router.get("/api/health", response_model=HealthResponse, include_in_schema=False)
async def health() -> HealthResponse:
    mcp_status = await mcp_client.health()
    overall = "UP" if mcp_status.get("status", "").upper() == "UP" else "DEGRADED"
    return HealthResponse(
        status=overall,
        app=cfg.APP_NAME,
        version=cfg.APP_VERSION,
        env=cfg.ENV,
        mcp=mcp_status,
    )
