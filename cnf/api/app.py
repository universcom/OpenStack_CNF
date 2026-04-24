"""
cnf.api.app — FastAPI application factory.

All endpoints are proxied to the master CNF node if the current
node is a worker.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from cnf.api.v1.router import v1_router
from cnf.config import get_settings
from cnf.utils.logging import get_logger

if TYPE_CHECKING:
    from cnf.agent.agent import CNFAgent

logger = get_logger(__name__)


def create_app(agent: "CNFAgent") -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("api_startup")
        app.state.agent = agent
        yield
        logger.info("api_shutdown")

    app = FastAPI(
        title="CNF — Cluster Nova Federation",
        description="Cross-cluster VM distribution, migration, and live-migration for OpenStack.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request proxy middleware: if we're a worker, forward to master
    @app.middleware("http")
    async def proxy_to_master(request: Request, call_next):
        agent: CNFAgent = request.app.state.agent
        # Skip health/metrics endpoints
        if request.url.path in ("/healthz", "/readyz", "/metrics"):
            return await call_next(request)

        if not agent.is_master:
            master_addr = agent.master_grpc_addr
            if master_addr:
                return await _forward_to_master(request, master_addr)

        return await call_next(request)

    # Routers
    app.include_router(v1_router, prefix="/v1")

    # Health endpoints
    @app.get("/healthz", tags=["health"])
    async def health():
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def ready(request: Request):
        ag: CNFAgent = request.app.state.agent
        return {
            "ready":      True,
            "cluster_id": settings.cluster_id,
            "role":       "master" if ag.is_master else "worker",
        }

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("unhandled_exception", path=str(request.url), error=str(exc))
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    FastAPIInstrumentor.instrument_app(app)
    return app


async def _forward_to_master(request: Request, master_addr: str) -> JSONResponse:
    """Proxy the incoming HTTP request to the master REST API."""
    import httpx
    host, _, port = master_addr.partition(":")
    port = port or "8080"
    url = f"http://{host}:{port}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    body = await request.body()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            headers=dict(request.headers),
            content=body,
        )
    return JSONResponse(
        status_code=resp.status_code,
        content=resp.json() if resp.content else None,
        headers=dict(resp.headers),
    )
