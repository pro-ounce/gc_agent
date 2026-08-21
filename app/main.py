"""
main.py
───────
FastAPI application factory.

Usage:
    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.commons.config import cfg
from app.commons.flags import flags
from app.commons.logger import get_logger
from app.commons.middleware import RequestContextMiddleware, SecurityHeadersMiddleware

log = get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle."""
    log.bind(func="lifespan").info(
        f"Starting {cfg.APP_NAME} v{cfg.APP_VERSION} [{cfg.ENV}]"
    )

    # Pre-warm MCP tool & prompt caches
    try:
        from app.mcp.tool_registry import tool_registry
        from app.mcp.prompt_registry import prompt_registry

        await tool_registry.get_tools()
        await prompt_registry.get_prompts()
    except Exception as exc:
        log.warning(f"MCP pre-warm failed (non-fatal): {exc}")

    if cfg.DEBUG:
        log.bind(func="lifespan").debug(f"Config: {cfg.to_dict()}")

    yield  # Application running

    log.bind(func="lifespan").info("Shutting down — closing connections")
    from app.connections import close_connections
    await close_connections()


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title=cfg.APP_NAME,
        version=cfg.APP_VERSION,
        description=(
            "Enterprise-grade MCP Agent & Chatbot — "
            "FastAPI bridge to a Spring Boot MCP server with agentic tool orchestration."
        ),
        docs_url="/docs" if not cfg.is_production() else None,
        redoc_url="/redoc" if not cfg.is_production() else None,
        # Path prefix when served behind the gc gateway rewrite (e.g. /ai-agent-service).
        root_path=cfg.ROOT_PATH or "",
        lifespan=lifespan,
    )

    # ── Middleware (order matters: outermost = last added) ────────────────────

    # Security headers
    app.add_middleware(SecurityHeadersMiddleware)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "X-API-KEY", "X-INT-TKN", "X-AR-KEY", "X-Request-ID", "X-Session-ID", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    # Request context (request-ID + access logging) — innermost, runs first
    app.add_middleware(RequestContextMiddleware)

    # RBAC / auth middleware
    if flags.auth_enabled or flags.rbac_enabled:
        from app.rbac.middleware import RBACMiddleware
        app.add_middleware(RBACMiddleware)

    # ── Routers ───────────────────────────────────────────────────────────────

    from app.routers.health import router as health_router
    from app.routers.chat import router as chat_router
    from app.routers.tools import router as tools_router
    from app.routers.prompts import router as prompts_router
    from app.routers.sessions import router as sessions_router
    from app.routers.platform import router as platform_router
    from app.routers.admin import router as admin_router

    app.include_router(health_router)
    app.include_router(admin_router)  # /admin — runtime config UI (IP-gated like actuator)
    app.include_router(chat_router)
    app.include_router(tools_router)
    app.include_router(prompts_router)
    app.include_router(sessions_router)
    app.include_router(platform_router)  # /ai-service/{agent}/chat — GC platform-facing

    if flags.rbac_enabled:
        from app.routers.auth import router as auth_router
        app.include_router(auth_router)

    # ── Static UI ─────────────────────────────────────────────────────────────
    _static = Path(__file__).parent / "static"
    if _static.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static)), name="static")

        @app.get("/", include_in_schema=False)
        async def ui():
            return FileResponse(str(_static / "index.html"))

    log.bind(func="create_app").info("Application initialised")
    return app


app = create_app()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=cfg.HOST,
        port=cfg.PORT,
        reload=cfg.RELOAD,
        workers=cfg.WORKERS,
        log_config=None,  # Use our structured logger
    )
