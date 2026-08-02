"""FastAPI dependencies: container access, authentication and authorisation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mediabot.core.container import Container
from mediabot.core.exceptions import AuthenticationError, PermissionDeniedError
from mediabot.core.security import constant_time_compare
from mediabot.domain.enums import AdminRole

bearer_scheme = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    """Return the container stored on the FastAPI application state."""
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def current_admin(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AdminRole:
    """Authenticate a request via JWT or a static API key.

    API keys are meant for machine-to-machine integrations and always map to
    the ``ADMIN`` role; interactive sessions use JWTs, whose ``role`` claim is
    honoured.  Anything else is rejected.
    """
    if x_api_key:
        for configured in container.settings.security.api_keys:
            if constant_time_compare(x_api_key, configured):
                return AdminRole.ADMIN
        raise AuthenticationError("Invalid API key")

    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")

    payload = container.jwt.decode(credentials.credentials, expected_type="access")
    role_value = str(payload.get("role") or AdminRole.SUPPORT.value)
    try:
        return AdminRole(role_value)
    except ValueError as exc:  # pragma: no cover - tampered token
        raise AuthenticationError("Unknown role in token") from exc


AdminDep = Annotated[AdminRole, Depends(current_admin)]


def require_role(required: AdminRole) -> Callable[[AdminRole], Awaitable[AdminRole]]:
    """Build a dependency enforcing a minimum staff role."""

    async def _dependency(role: AdminDep) -> AdminRole:
        if not role.can(required):
            raise PermissionDeniedError(f"{required.value} role is required")
        return role

    return _dependency


async def current_actor_id(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> int:
    """Numeric id of the authenticated admin (``0`` for API-key callers)."""
    if credentials is None or not credentials.credentials:
        return 0
    try:
        payload = container.jwt.decode(credentials.credentials, expected_type="access")
    except AuthenticationError:  # pragma: no cover - checked again by current_admin
        return 0
    subject = str(payload.get("sub") or "")
    return int(subject) if subject.isdigit() else 0


ActorDep = Annotated[int, Depends(current_actor_id)]
