"""Server-rendered admin web panel (dark theme).

Architecture note
-----------------
The panel is intentionally server-rendered with Jinja2 and ships **no external
assets**: the strict Content-Security-Policy set in
:mod:`mediabot.presentation.api.app` forbids third-party scripts, and charts
are drawn as inline SVG.  That removes an entire class of supply-chain and XSS
risks from an interface that can ban users and grant subscriptions.

Authentication uses the same JWT as the API, stored in an ``HttpOnly``,
``SameSite=Strict`` cookie so it is unreachable from JavaScript and immune to
cross-site form posts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mediabot import __version__
from mediabot.core.container import Container
from mediabot.core.exceptions import MediaBotError, PermissionDeniedError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import AdminRole, PaymentStatus, PromoType, SubscriptionTier
from mediabot.domain.value_objects import humanize_bytes

router = APIRouter(prefix="/admin", tags=["admin-panel"], include_in_schema=False)
log = get_logger(LogChannel.ADMIN, component="web_panel")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["bytes"] = humanize_bytes

COOKIE_NAME = "mediabot_admin"
COOKIE_MAX_AGE = 60 * 60 * 8


def _container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def _session(request: Request) -> dict[str, Any]:
    """Decode the session cookie or raise."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise PermissionDeniedError("Not signed in")
    return _container(request).jwt.decode(token, expected_type="access")


def _role_of(request: Request) -> AdminRole:
    claims = _session(request)
    try:
        return AdminRole(str(claims.get("role")))
    except ValueError:  # pragma: no cover - tampered cookie
        raise PermissionDeniedError("Unknown role") from None


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    claims = _session(request)
    return {
        "request": request,
        "version": __version__,
        "username": claims.get("username", "admin"),
        "role": claims.get("role", AdminRole.SUPPORT.value),
        **extra,
    }


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "error": None, "version": __version__}
    )


@router.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=128)],
) -> Any:
    container = _container(request)
    try:
        admin = await container.admin.authenticate(
            username,
            password,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except MediaBotError as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": exc.message, "version": __version__},
            status_code=401,
        )

    tokens = container.jwt.create_token_pair(
        str(admin.id), role=admin.role.value, username=admin.username
    )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        tokens.access_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=container.settings.app.is_production,
        path="/admin",
    )
    return response


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = _redirect_to_login()
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return response


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    try:
        context = _context(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    metrics = await container.statistics.dashboard()
    history = await container.statistics.history(days=14)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **context,
            "metrics": metrics,
            "history": history,
            "chart": _sparkline([int(str(row["downloads"])) for row in history]),
            "hours": _bars(metrics.by_hour),
            "platforms": sorted(metrics.by_platform.items(), key=lambda item: -item[1])[:10],
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, q: str = "", page: int = 1) -> Any:
    try:
        context = _context(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    limit = 25
    offset = max(page - 1, 0) * limit
    users = await container.admin.find_users(q, limit=limit, offset=offset)
    rows = []
    async with container.uow_factory() as uow:
        for user in users:
            wallet = await uow.wallets.for_user(user.id)
            rows.append({"user": user, "balance": wallet.balance if wallet else 0})
    return templates.TemplateResponse(
        request,
        "users.html",
        {**context, "rows": rows, "query": q, "page": page},
    )


@router.post("/users/{user_id}/action")
async def user_action(
    request: Request,
    user_id: int,
    action: Annotated[str, Form()],
    value: Annotated[str, Form()] = "",
) -> Any:
    """Apply a moderation or grant action from the users table."""
    try:
        role = _role_of(request)
        claims = _session(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    actor_id = int(str(claims.get("sub") or 0))
    try:
        match action:
            case "ban":
                await container.admin.ban(
                    actor=role, actor_id=actor_id, user_id=user_id, reason=value or "web panel"
                )
            case "unban":
                await container.admin.unban(actor=role, actor_id=actor_id, user_id=user_id)
            case "mute":
                await container.admin.mute(
                    actor=role, actor_id=actor_id, user_id=user_id, minutes=int(value or 60)
                )
            case "unmute":
                await container.admin.unmute(actor=role, actor_id=actor_id, user_id=user_id)
            case "grant_premium":
                await container.admin.grant_subscription(
                    actor=role,
                    actor_id=actor_id,
                    user_id=user_id,
                    tier=SubscriptionTier.PREMIUM,
                    days=int(value or 30),
                )
            case "grant_vip":
                await container.admin.grant_subscription(
                    actor=role,
                    actor_id=actor_id,
                    user_id=user_id,
                    tier=SubscriptionTier.VIP,
                    days=int(value or 30),
                )
            case "revoke":
                await container.admin.revoke_subscription(
                    actor=role, actor_id=actor_id, user_id=user_id
                )
            case "coins":
                await container.admin.adjust_balance(
                    actor=role,
                    actor_id=actor_id,
                    user_id=user_id,
                    amount=int(value or 0),
                    comment="web panel",
                )
    except MediaBotError as exc:
        log.warning("panel action {} failed: {}", action, exc.message)
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request, status: str = "") -> Any:
    try:
        context = _context(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    status_filter = PaymentStatus(status) if status else None
    async with container.uow_factory() as uow:
        payments = await uow.payments.list_recent(status=status_filter, limit=50)
    return templates.TemplateResponse(
        request,
        "payments.html",
        {**context, "payments": payments, "status": status},
    )


@router.get("/promos", response_class=HTMLResponse)
async def promos_page(request: Request) -> Any:
    try:
        context = _context(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    promos = await container.admin.list_promos(limit=100)
    return templates.TemplateResponse(
        request,
        "promos.html",
        {**context, "promos": promos, "promo_types": [item.value for item in PromoType]},
    )


@router.post("/promos")
async def create_promo(
    request: Request,
    promo_type: Annotated[str, Form()],
    value: Annotated[int, Form()],
    code: Annotated[str, Form()] = "",
    max_activations: Annotated[str, Form()] = "",
    expires_in_days: Annotated[str, Form()] = "",
) -> Any:
    try:
        role = _role_of(request)
        claims = _session(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    try:
        await container.admin.create_promo(
            actor=role,
            actor_id=int(str(claims.get("sub") or 0)),
            promo_type=PromoType(promo_type),
            value=value,
            code=code or None,
            max_activations=int(max_activations) if max_activations.isdigit() else None,
            expires_in_days=int(expires_in_days) if expires_in_days.isdigit() else None,
        )
    except MediaBotError as exc:
        log.warning("promo creation failed: {}", exc.message)
    return RedirectResponse("/admin/promos", status_code=303)


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request) -> Any:
    try:
        context = _context(request)
    except MediaBotError:
        return _redirect_to_login()

    container = _container(request)
    snapshot = await container.admin.queue_snapshot(limit=50)
    errors = await container.admin.error_log(limit=20)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {**context, "snapshot": snapshot, "errors": errors},
    )


@router.post("/queue/clear")
async def clear_queue(request: Request) -> Any:
    try:
        role = _role_of(request)
        claims = _session(request)
    except MediaBotError:
        return _redirect_to_login()
    container = _container(request)
    try:
        await container.admin.clear_queue(actor=role, actor_id=int(str(claims.get("sub") or 0)))
    except MediaBotError as exc:
        log.warning("queue clear rejected: {}", exc.message)
    return RedirectResponse("/admin/queue", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> Any:
    try:
        context = _context(request)
    except MediaBotError:
        return _redirect_to_login()
    container = _container(request)
    entries = await container.admin.audit_log(limit=100)
    return templates.TemplateResponse(request, "logs.html", {**context, "entries": entries})


# --------------------------------------------------------------------------- #
# Inline chart helpers (no JavaScript, CSP-safe)
# --------------------------------------------------------------------------- #
def _sparkline(values: list[int], *, width: int = 620, height: int = 140) -> dict[str, Any]:
    """Build the polyline points of a sparkline chart."""
    if not values:
        return {"points": "", "max": 0, "values": []}
    peak = max(values) or 1
    step = width / max(len(values) - 1, 1)
    points = " ".join(
        f"{index * step:.1f},{height - (value / peak) * (height - 20):.1f}"
        for index, value in enumerate(values)
    )
    return {"points": points, "max": peak, "values": values, "width": width, "height": height}


def _bars(values: list[int]) -> list[dict[str, Any]]:
    """Normalise a 24-hour histogram into percentage heights."""
    peak = max(values or [0]) or 1
    return [
        {"hour": hour, "value": value, "percent": round(value / peak * 100, 1)}
        for hour, value in enumerate(values or [0] * 24)
    ]
