"""User, subscription and statistics endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mediabot.core.exceptions import NotFoundError
from mediabot.domain.enums import AdminRole
from mediabot.domain.policies import get_tier_policy
from mediabot.presentation.api.dependencies import (
    ActorDep,
    AdminDep,
    ContainerDep,
    require_role,
)
from mediabot.presentation.api.schemas import (
    DashboardResponse,
    GrantSubscriptionRequest,
    LimitsResponse,
    SubscriptionResponse,
    UserListResponse,
    UserResponse,
    UserStatisticsResponse,
)

router = APIRouter(tags=["users"])


def _to_user_response(user: object, balance: int) -> UserResponse:
    return UserResponse(
        id=user.id,  # type: ignore[attr-defined]
        username=user.username,  # type: ignore[attr-defined]
        full_name=user.full_name,  # type: ignore[attr-defined]
        language=user.language.value,  # type: ignore[attr-defined]
        tier=user.tier,  # type: ignore[attr-defined]
        status=user.status.value,  # type: ignore[attr-defined]
        balance=balance,
        total_downloads=user.total_downloads,  # type: ignore[attr-defined]
        total_bytes=user.total_bytes,  # type: ignore[attr-defined]
        referrals=user.referral_count,  # type: ignore[attr-defined]
        created_at=user.created_at,  # type: ignore[attr-defined]
        last_seen_at=user.last_seen_at,  # type: ignore[attr-defined]
    )


@router.get("/users", response_model=UserListResponse)
async def list_users(
    container: ContainerDep,
    _role: AdminDep,
    query: Annotated[str, Query(max_length=64)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListResponse:
    """Search or list users (staff only)."""
    users = await container.admin.find_users(query, limit=limit, offset=offset)
    items: list[UserResponse] = []
    async with container.uow_factory() as uow:
        total = await uow.users.count()
        for user in users:
            wallet = await uow.wallets.for_user(user.id)
            items.append(_to_user_response(user, wallet.balance if wallet else 0))
    return UserListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, container: ContainerDep, _role: AdminDep) -> UserResponse:
    async with container.uow_factory() as uow:
        user = await uow.users.get(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} not found")
        wallet = await uow.wallets.for_user(user_id)
    return _to_user_response(user, wallet.balance if wallet else 0)


@router.get("/users/{user_id}/limits", response_model=LimitsResponse)
async def get_limits(user_id: int, container: ContainerDep, _role: AdminDep) -> LimitsResponse:
    """Effective limits of a user, including admin/promo overrides."""
    context = await container.users.build_context(user_id)
    policy = context.policy
    return LimitsResponse(
        tier=context.tier,
        daily_downloads=policy.daily_downloads,
        used_today=context.usage.downloads_today,
        remaining=context.remaining_downloads,
        max_file_size_bytes=policy.max_file_size_bytes,
        max_duration_seconds=policy.max_duration_seconds,
        max_concurrent_jobs=policy.max_concurrent_jobs,
        max_video_quality=policy.max_video_quality.value,
        max_audio_quality=policy.max_audio_quality.value,
    )


@router.get("/users/{user_id}/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    user_id: int,
    container: ContainerDep,
    _role: AdminDep,
) -> SubscriptionResponse:
    """Check whether a user currently holds a paid subscription."""
    subscription = await container.subscriptions.current(user_id)
    context = await container.users.build_context(user_id)
    policy = get_tier_policy(context.tier)
    return SubscriptionResponse(
        tier=context.tier,
        active=subscription is not None,
        expires_at=subscription.expires_at if subscription else None,
        auto_renew=bool(subscription.auto_renew) if subscription else False,
        features=list(policy.features),
    )


@router.post(
    "/users/{user_id}/subscription",
    response_model=SubscriptionResponse,
    dependencies=[Depends(require_role(AdminRole.ADMIN))],
)
async def grant_subscription(
    user_id: int,
    payload: GrantSubscriptionRequest,
    container: ContainerDep,
    role: AdminDep,
    actor_id: ActorDep,
) -> SubscriptionResponse:
    """Grant or extend a subscription (admin only)."""
    await container.admin.grant_subscription(
        actor=role,
        actor_id=actor_id,
        user_id=user_id,
        tier=payload.tier,
        days=payload.days,
    )
    subscription = await container.subscriptions.current(user_id)
    policy = get_tier_policy(payload.tier)
    return SubscriptionResponse(
        tier=payload.tier,
        active=subscription is not None,
        expires_at=subscription.expires_at if subscription else None,
        auto_renew=False,
        features=list(policy.features),
    )


@router.get("/users/{user_id}/statistics", response_model=UserStatisticsResponse)
async def user_statistics(
    user_id: int,
    container: ContainerDep,
    _role: AdminDep,
) -> UserStatisticsResponse:
    stats = await container.statistics.user_statistics(user_id)
    return UserStatisticsResponse(
        user_id=stats.user_id,
        total_downloads=stats.total_downloads,
        video_downloads=stats.video_downloads,
        audio_downloads=stats.audio_downloads,
        total_bytes=stats.total_bytes,
        days_with_us=stats.days_with_us,
        tier=stats.tier,
        balance=stats.balance,
        referrals=stats.referrals,
        achievements=stats.achievements,
        rank=stats.rank,
        daily_streak=stats.daily_streak,
        favorite_platform=stats.favorite_platform,
    )


@router.get("/statistics/dashboard", response_model=DashboardResponse)
async def dashboard(container: ContainerDep, _role: AdminDep) -> DashboardResponse:
    """Aggregated operational metrics."""
    metrics = await container.statistics.dashboard()
    return DashboardResponse(
        dau=metrics.dau,
        wau=metrics.wau,
        mau=metrics.mau,
        new_users_today=metrics.new_users_today,
        total_users=metrics.total_users,
        active_subscriptions=metrics.active_subscriptions,
        subscriptions_by_tier=metrics.subscriptions_by_tier,
        downloads_today=metrics.downloads_today,
        downloads_week=metrics.downloads_week,
        downloads_failed_today=metrics.downloads_failed_today,
        bytes_today=metrics.bytes_today,
        average_file_size=metrics.average_file_size,
        revenue_today=metrics.revenue_today,
        revenue_month=metrics.revenue_month,
        queue_size=metrics.queue_size,
        active_jobs=metrics.active_jobs,
        by_platform=metrics.by_platform,
        by_hour=metrics.by_hour,
        cpu_percent=metrics.cpu_percent,
        memory_percent=metrics.memory_percent,
        disk_used_percent=metrics.disk_used_percent,
    )


@router.get("/statistics/history")
async def statistics_history(
    container: ContainerDep,
    _role: AdminDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[dict[str, object]]:
    """Daily aggregates powering the dashboard charts."""
    return await container.statistics.history(days=days)
