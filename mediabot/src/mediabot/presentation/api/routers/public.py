"""Public endpoints: authentication, health and Prometheus metrics."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from mediabot import __version__
from mediabot.core.exceptions import AuthenticationError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.infrastructure.metrics import collect_system_health, render_metrics
from mediabot.presentation.api.dependencies import ContainerDep
from mediabot.presentation.api.schemas import (
    HealthResponse,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)

router = APIRouter(tags=["public"])
log = get_logger(LogChannel.SECURITY, component="api.auth")


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    container: ContainerDep,
) -> TokenResponse:
    """Exchange staff credentials for a JWT pair."""
    admin = await container.admin.authenticate(
        payload.username,
        payload.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    tokens = container.jwt.create_token_pair(
        str(admin.id), role=admin.role.value, username=admin.username
    )
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, container: ContainerDep) -> TokenResponse:
    """Rotate an access token using a valid refresh token."""
    claims = container.jwt.decode(payload.refresh_token, expected_type="refresh")
    subject = str(claims.get("sub") or "")
    if not subject:
        raise AuthenticationError("Malformed refresh token")
    async with container.uow_factory() as uow:
        admin = await uow.admins.get(int(subject)) if subject.isdigit() else None
    if admin is None or not admin.is_active:
        raise AuthenticationError("Account is no longer active")
    tokens = container.jwt.create_token_pair(
        subject, role=admin.role.value, username=admin.username
    )
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.get("/health", response_model=HealthResponse)
async def health(container: ContainerDep) -> HealthResponse:
    """Liveness/readiness probe used by Docker, nginx and Prometheus."""
    database_ok = True
    try:
        async with container.uow_factory() as uow:
            await uow.users.count()
    except Exception:  # pragma: no cover - the probe must never raise
        database_ok = False

    redis_ok = await container.cache.ping()
    system = collect_system_health(str(container.settings.app.storage_dir))

    queue_size = 0
    active_jobs = 0
    if database_ok:
        async with container.uow_factory() as uow:
            queue_size = await uow.queue.size()
            active_jobs = await uow.downloads.count_running()

    healthy = database_ok and redis_ok and system.is_healthy
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=__version__,
        database=database_ok,
        redis=redis_ok,
        ffmpeg=container.ffmpeg.available,
        queue_size=queue_size,
        active_jobs=active_jobs,
        disk_free_bytes=system.disk_free_bytes,
        memory_percent=system.memory_percent,
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")
