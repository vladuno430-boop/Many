"""FastAPI application: REST API + admin web panel.

Security notes
--------------
* Every response carries hardening headers (``X-Frame-Options``,
  ``X-Content-Type-Options``, a strict ``Content-Security-Policy`` and HSTS in
  production), which covers the XSS/clickjacking requirements for the panel.
* CORS is opt-in through ``API__CORS_ORIGINS``; the default is "no origins",
  so a browser on another site cannot call the API with a user's cookies.
* Domain exceptions are converted into structured JSON, never stack traces.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from mediabot import __version__
from mediabot.core.container import Container
from mediabot.core.exceptions import MediaBotError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.infrastructure.metrics import API_LATENCY, ERRORS_TOTAL
from mediabot.presentation.api.routers import downloads, promos, public, users
from mediabot.presentation.web.panel import router as panel_router

log = get_logger(LogChannel.API, component="api")

API_PREFIX = "/api/v1"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds defensive headers to every response."""

    def __init__(self, app: FastAPI, *, production: bool) -> None:
        super().__init__(app)
        self._production = production

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
        if self._production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request latency per endpoint."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        endpoint = getattr(route, "path", request.url.path)
        API_LATENCY.labels(request.method, endpoint).observe(time.perf_counter() - started)
        return response


def create_app(container: Container | None = None) -> FastAPI:
    """Build the FastAPI application."""
    app_container = container or Container()
    settings = app_container.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = app_container
        await app_container.admin.ensure_bootstrap_admin()
        log.info("API started (env={} version={})", settings.app.env, __version__)
        yield
        await app_container.shutdown()

    app = FastAPI(
        title="MediaBot API",
        description="REST API of the MediaBot Telegram downloader",
        version=__version__,
        root_path=settings.api.root_path,
        docs_url="/docs" if settings.api.docs_enabled else None,
        redoc_url="/redoc" if settings.api.docs_enabled else None,
        openapi_url="/openapi.json" if settings.api.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.container = app_container

    app.add_middleware(
        SecurityHeadersMiddleware,
        production=settings.app.is_production,
    )
    app.add_middleware(MetricsMiddleware)
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        )

    # ---- exception handlers ------------------------------------------- #
    @app.exception_handler(MediaBotError)
    async def _domain_error(request: Request, exc: MediaBotError) -> JSONResponse:
        ERRORS_TOTAL.labels(exc.code, "api").inc()
        log.warning("{} {} -> {} ({})", request.method, request.url.path, exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.as_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # ``exc.errors()`` may carry non-serialisable objects (the original
        # ``ValueError`` in ``ctx``), so only the safe fields are echoed back —
        # that also avoids leaking internals to the caller.
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "type": error.get("type", "invalid"),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors()[:10]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": details},
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        ERRORS_TOTAL.labels("unhandled", "api").inc()
        log.opt(exception=exc).error("unhandled API error at {}", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "Internal server error",
                "details": {},
            },
        )

    # ---- routes -------------------------------------------------------- #
    app.include_router(public.router, prefix=API_PREFIX)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(downloads.router, prefix=API_PREFIX)
    app.include_router(promos.router, prefix=API_PREFIX)
    app.include_router(panel_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": "mediabot", "version": __version__, "docs": "/docs"}

    return app
