"""Statistics: per-user profiles and the operator analytics dashboard."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from mediabot.application.dto import DashboardMetrics, UserStatistics
from mediabot.core.config import Settings
from mediabot.core.exceptions import NotFoundError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import DownloadStatus, MediaKind, SubscriptionTier
from mediabot.infrastructure.db.session import UnitOfWorkFactory
from mediabot.infrastructure.metrics import (
    ACTIVE_JOBS,
    ACTIVE_USERS,
    QUEUE_SIZE,
    collect_system_health,
)

log = get_logger(LogChannel.APP, component="analytics")


class StatisticsService:
    """Reads for the profile screen, the admin panel and the REST API."""

    def __init__(self, uow_factory: UnitOfWorkFactory, settings: Settings) -> None:
        self._uow_factory = uow_factory
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Per-user
    # ------------------------------------------------------------------ #
    async def user_statistics(self, user_id: int) -> UserStatistics:
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            wallet = await uow.wallets.for_user(user_id)
            subscription = await uow.subscriptions.active_for_user(user_id)
            achievements = await uow.achievements.count(user_id=user_id)
            rank = await uow.users.rank_of(user_id)
            platforms = await uow.downloads.by_platform(
                user.created_at.replace(tzinfo=UTC), datetime.now(UTC)
            )
            registered_at = user.created_at
            if registered_at.tzinfo is None:
                registered_at = registered_at.replace(tzinfo=UTC)

        favorite_platform = max(platforms, key=lambda key: platforms[key]) if platforms else None
        return UserStatistics(
            user_id=user_id,
            total_downloads=user.total_downloads,
            video_downloads=user.total_video_downloads,
            audio_downloads=user.total_audio_downloads,
            total_bytes=user.total_bytes,
            total_media_seconds=user.total_seconds_media,
            registered_at=registered_at,
            days_with_us=max((datetime.now(UTC) - registered_at).days, 0),
            tier=SubscriptionTier(user.tier),
            subscription_expires_at=subscription.expires_at if subscription else None,
            balance=wallet.balance if wallet else 0,
            coins_earned=wallet.total_earned if wallet else 0,
            coins_spent=wallet.total_spent if wallet else 0,
            referrals=user.referral_count,
            achievements=achievements,
            rank=rank,
            daily_streak=user.daily_streak,
            favorite_platform=favorite_platform,
        )

    # ------------------------------------------------------------------ #
    # Operator dashboard
    # ------------------------------------------------------------------ #
    async def dashboard(self) -> DashboardMetrics:
        """Live operational metrics (used by the web panel and ``/api/stats``)."""
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        month_start = now - timedelta(days=30)

        async with self._uow_factory() as uow:
            dau = await uow.users.active_since(now - timedelta(days=1))
            wau = await uow.users.active_since(week_start)
            mau = await uow.users.active_since(month_start)
            new_today = await uow.users.new_since(day_start)
            new_week = await uow.users.new_since(week_start)
            total_users = await uow.users.count()
            active_subscriptions = await uow.subscriptions.count_active()
            by_tier = await uow.subscriptions.count_by_tier()

            downloads_today = await uow.downloads.count_between(day_start, now)
            downloads_week = await uow.downloads.count_between(week_start, now)
            failed_today = await uow.downloads.count_between(
                day_start, now, status=DownloadStatus.FAILED
            )
            bytes_today = await uow.downloads.bytes_between(day_start, now)
            average_size = await uow.downloads.average_size(week_start, now)
            average_ms = await uow.downloads.average_duration_ms(week_start, now)
            by_platform = await uow.downloads.by_platform(week_start, now)
            by_hour = await uow.downloads.by_hour(day_start, now)
            by_day = await uow.downloads.daily_counts(month_start, now)
            top_errors = list(await uow.downloads.top_errors(week_start))

            revenue_today, _ = await uow.payments.revenue_between(day_start, now)
            revenue_month, _ = await uow.payments.revenue_between(month_start, now)

            queue_size = await uow.queue.size()
            active_jobs = await uow.downloads.count_running()

        health = collect_system_health(str(self._settings.app.storage_dir))
        QUEUE_SIZE.set(queue_size)
        ACTIVE_JOBS.set(active_jobs)
        ACTIVE_USERS.set(dau)

        return DashboardMetrics(
            dau=dau,
            wau=wau,
            mau=mau,
            new_users_today=new_today,
            new_users_week=new_week,
            total_users=total_users,
            active_subscriptions=active_subscriptions,
            subscriptions_by_tier=by_tier,
            downloads_today=downloads_today,
            downloads_week=downloads_week,
            downloads_failed_today=failed_today,
            bytes_today=bytes_today,
            average_file_size=average_size,
            average_job_ms=average_ms,
            revenue_today=revenue_today,
            revenue_month=revenue_month,
            queue_size=queue_size,
            active_jobs=active_jobs,
            by_platform=by_platform,
            by_hour=by_hour,
            by_day=by_day,
            top_errors=top_errors,
            cpu_percent=health.cpu_percent,
            memory_percent=health.memory_percent,
            disk_used_percent=health.disk_used_percent,
        )

    # ------------------------------------------------------------------ #
    # Nightly aggregation
    # ------------------------------------------------------------------ #
    async def aggregate_day(self, day: date | None = None) -> None:
        """Materialise one :class:`DailyStatistic` row.

        Runs hourly for the current day (so the dashboard is fresh) and covers
        the previous day right after midnight.
        """
        target = day or datetime.now(UTC).date()
        start = datetime.combine(target, datetime.min.time(), UTC)
        end = start + timedelta(days=1)

        async with self._uow_factory.transaction() as uow:
            downloads_total = await uow.downloads.count_between(start, end)
            downloads_video = await uow.downloads.count_between(start, end, kind=MediaKind.VIDEO)
            downloads_audio = await uow.downloads.count_between(start, end, kind=MediaKind.AUDIO)
            failed = await uow.downloads.count_between(start, end, status=DownloadStatus.FAILED)
            revenue, payments_count = await uow.payments.revenue_between(start, end)
            issued, spent = await uow.wallets.coins_flow(start)
            health = collect_system_health(str(self._settings.app.storage_dir))

            await uow.statistics.upsert(
                target,
                new_users=await uow.users.new_since(start),
                active_users=await uow.users.active_since(start),
                monthly_active_users=await uow.users.active_since(end - timedelta(days=30)),
                downloads_total=downloads_total,
                downloads_video=downloads_video,
                downloads_audio=downloads_audio,
                downloads_failed=failed,
                bytes_total=await uow.downloads.bytes_between(start, end),
                average_file_size=await uow.downloads.average_size(start, end),
                average_duration_ms=await uow.downloads.average_duration_ms(start, end),
                active_subscriptions=await uow.subscriptions.count_active(),
                revenue=revenue,
                payments_count=payments_count,
                coins_issued=issued,
                coins_spent=spent,
                by_platform=await uow.downloads.by_platform(start, end),
                by_hour=await uow.downloads.by_hour(start, end),
                server_load=health.cpu_percent,
            )
        log.info("aggregated statistics for {}", target.isoformat())

    async def history(self, *, days: int = 30) -> list[dict[str, object]]:
        """Daily rows for the dashboard charts."""
        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)
        async with self._uow_factory() as uow:
            rows = await uow.statistics.range(start, end)
        return [
            {
                "day": row.day.isoformat(),
                "new_users": row.new_users,
                "active_users": row.active_users,
                "downloads": row.downloads_total,
                "downloads_failed": row.downloads_failed,
                "bytes": row.bytes_total,
                "revenue": row.revenue,
            }
            for row in rows
        ]
