"""Dependency injection container.

Architecture note
-----------------
A single composition root wires every concrete implementation together.  Each
entry point (bot, API, worker, scheduler) builds one container and pulls the
services it needs; nothing else in the code base constructs its own
dependencies, so swapping an adapter (e.g. a different payment gateway or an
in-memory dispatcher in tests) is a one-line change here.

Instances are created lazily and memoised: importing the container is cheap and
a worker never opens a Telegram session it does not use.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any, Self, cast

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from redis.asyncio import Redis

from mediabot.application.services.admin_service import AdminService
from mediabot.application.services.analytics_service import StatisticsService
from mediabot.application.services.download_service import (
    DownloadService,
    MediaDelivery,
    ProgressSink,
)
from mediabot.application.services.economy_service import (
    AchievementsService,
    DailyBonusService,
    PromoRedemptionService,
    ReferralService,
    WalletService,
)
from mediabot.application.services.engagement_service import (
    FavoriteService,
    HistoryService,
    NotificationService,
)
from mediabot.application.services.media_service import MediaService
from mediabot.application.services.payment_service import PaymentService
from mediabot.application.services.security_service import SecurityService
from mediabot.application.services.user_service import SubscriptionService, UserService
from mediabot.core.config import Settings, get_settings
from mediabot.core.logging import configure_logging
from mediabot.core.security import JWTService, TokenEncryptor
from mediabot.domain.services.economy import (
    AchievementService as DomainAchievementService,
)
from mediabot.domain.services.economy import (
    EconomyService,
)
from mediabot.domain.services.format_selection import FormatSelector
from mediabot.domain.services.limits import LimitService
from mediabot.domain.services.promo import PromoService
from mediabot.domain.services.queueing import QueueService
from mediabot.infrastructure.cache.memory import InMemoryProgressPublisher, InMemoryRedis
from mediabot.infrastructure.cache.progress import ProgressPublisher
from mediabot.infrastructure.cache.redis_cache import (
    CacheService,
    DistributedLock,
    RateLimiter,
    create_redis,
)
from mediabot.infrastructure.db.session import (
    UnitOfWorkFactory,
    create_engine,
    create_session_factory,
)
from mediabot.infrastructure.downloader.ffmpeg import FFmpegService
from mediabot.infrastructure.downloader.ytdlp_adapter import YtDlpAdapter
from mediabot.infrastructure.payments.providers import PaymentGatewayRegistry
from mediabot.infrastructure.queue.dispatcher import TaskDispatcher
from mediabot.infrastructure.queue.inline import InlineDispatcher
from mediabot.infrastructure.storage import StorageService


class Container:
    """Composition root holding every singleton of the process."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dispatcher: TaskDispatcher | None = None,
        delivery: MediaDelivery | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        configure_logging(self.settings.observability)
        self.settings.app.ensure_directories()
        self._dispatcher_override = dispatcher
        self._delivery: MediaDelivery | None = delivery
        self._bot: Bot | None = None

    # ------------------------------------------------------------------ #
    # Infrastructure
    # ------------------------------------------------------------------ #
    @cached_property
    def engine(self) -> Any:
        return create_engine(self.settings.db)

    @cached_property
    def uow_factory(self) -> UnitOfWorkFactory:
        return UnitOfWorkFactory(create_session_factory(self.engine))

    @cached_property
    def redis(self) -> Redis:
        """Redis client, or its in-process stand-in in standalone mode."""
        if not self._use_redis:
            return cast(Redis, InMemoryRedis())
        return create_redis(self.settings.redis)

    @property
    def _use_redis(self) -> bool:
        return self.settings.redis.enabled and not self.settings.app.is_standalone

    @cached_property
    def cache(self) -> CacheService:
        return CacheService(self.redis)

    @cached_property
    def rate_limiter(self) -> RateLimiter:
        return RateLimiter(self.redis)

    @cached_property
    def lock(self) -> DistributedLock:
        return DistributedLock(self.redis)

    @cached_property
    def progress_publisher(self) -> ProgressSink:
        """Sink the downloader writes progress to.

        Distributed mode uses a synchronous Redis client because the worker is
        a different process; standalone keeps the snapshots in memory, which
        also avoids a second Redis connection on a phone.
        """
        if not self._use_redis:
            return InMemoryProgressPublisher()
        return ProgressPublisher(self.settings.redis)

    async def progress_snapshot(self, download_id: int) -> dict[str, Any] | None:
        """Latest progress of a job, whichever sink is configured."""
        publisher = self.progress_publisher
        if isinstance(publisher, InMemoryProgressPublisher):
            return publisher.snapshot(download_id)
        return await self.cache.get_progress(download_id)

    @cached_property
    def storage(self) -> StorageService:
        service = StorageService(self.settings.app)
        service.ensure()
        return service

    @cached_property
    def ytdlp(self) -> YtDlpAdapter:
        return YtDlpAdapter(self.settings.downloader, self.settings.app)

    @cached_property
    def ffmpeg(self) -> FFmpegService:
        return FFmpegService(self.settings.downloader)

    @cached_property
    def dispatcher(self) -> TaskDispatcher:
        """Celery in production, an in-process runner in standalone mode."""
        if self._dispatcher_override is not None:
            return self._dispatcher_override
        if self.settings.app.is_standalone:
            inline = InlineDispatcher(
                max_concurrent_jobs=self.settings.downloader.max_concurrent_jobs
            )
            inline.bind(
                download_runner=self._run_download_inline,
                notification_runner=self._send_notification_inline,
                broadcast_runner=self._send_broadcast_inline,
            )
            return inline
        from mediabot.infrastructure.queue.celery_app import celery_app
        from mediabot.infrastructure.queue.celery_dispatcher import CeleryDispatcher

        return CeleryDispatcher(celery_app)

    # -- inline runners used by the standalone dispatcher ----------------- #
    async def _run_download_inline(self, download_id: int) -> None:
        await self.downloads.execute(download_id, worker_name="inline")

    async def _send_notification_inline(self, notification_id: int) -> bool:
        from mediabot.infrastructure.notifications.telegram import NotificationSender

        async with self.uow_factory() as uow:
            notification = await uow.notifications.get(notification_id)
            user = await uow.users.get(notification.user_id) if notification else None
        if notification is None:
            return False
        if user is None or not user.notifications_enabled:
            await self.notifications.mark_skipped(notification_id, "notifications disabled")
            return False

        delivered = await NotificationSender(self.bot(), self.uow_factory).send(notification)
        if delivered:
            await self.notifications.mark_sent(notification_id)
        else:
            await self.notifications.mark_failed(notification_id, "delivery refused")
        return delivered

    async def _send_broadcast_inline(self, broadcast_id: str) -> int:
        delivered = 0
        while True:
            pending = await self.notifications.pending_for_broadcast(broadcast_id, limit=100)
            if not pending:
                return delivered
            for notification in pending:
                if await self._send_notification_inline(notification.id):
                    delivered += 1

    @cached_property
    def gateways(self) -> PaymentGatewayRegistry:
        return PaymentGatewayRegistry(self.settings.payments)

    @cached_property
    def jwt(self) -> JWTService:
        return JWTService(self.settings.security)

    @cached_property
    def encryptor(self) -> TokenEncryptor:
        return TokenEncryptor(self.settings.security)

    # ------------------------------------------------------------------ #
    # Domain services (stateless, pure)
    # ------------------------------------------------------------------ #
    @cached_property
    def limit_service(self) -> LimitService:
        return LimitService()

    @cached_property
    def queue_service(self) -> QueueService:
        return QueueService()

    @cached_property
    def format_selector(self) -> FormatSelector:
        return FormatSelector()

    @cached_property
    def economy(self) -> EconomyService:
        return EconomyService(self.settings.economy)

    @cached_property
    def promo_domain(self) -> PromoService:
        return PromoService()

    @cached_property
    def achievement_domain(self) -> DomainAchievementService:
        return DomainAchievementService()

    # ------------------------------------------------------------------ #
    # Application services
    # ------------------------------------------------------------------ #
    @cached_property
    def users(self) -> UserService:
        return UserService(
            self.uow_factory,
            self.cache,
            self.settings,
            self.limit_service,
            self.economy,
        )

    @cached_property
    def subscriptions(self) -> SubscriptionService:
        return SubscriptionService(self.uow_factory, self.cache)

    @cached_property
    def media(self) -> MediaService:
        return MediaService(self.ytdlp, self.cache, self.settings, self.limit_service)

    @cached_property
    def downloads(self) -> DownloadService:
        return DownloadService(
            uow_factory=self.uow_factory,
            adapter=self.ytdlp,
            ffmpeg=self.ffmpeg,
            storage=self.storage,
            cache=self.cache,
            dispatcher=self.dispatcher,
            settings=self.settings,
            limit_service=self.limit_service,
            queue_service=self.queue_service,
            format_selector=self.format_selector,
            delivery=self._delivery,
            progress_publisher=self.progress_publisher,
        )

    @cached_property
    def wallet(self) -> WalletService:
        return WalletService(self.uow_factory, self.cache, self.economy, self.settings)

    @cached_property
    def daily_bonus(self) -> DailyBonusService:
        return DailyBonusService(self.uow_factory, self.wallet, self.economy)

    @cached_property
    def referrals(self) -> ReferralService:
        return ReferralService(self.uow_factory, self.wallet, self.economy, self.settings)

    @cached_property
    def promos(self) -> PromoRedemptionService:
        return PromoRedemptionService(
            self.uow_factory, self.promo_domain, self.wallet, self.settings
        )

    @cached_property
    def achievements(self) -> AchievementsService:
        return AchievementsService(self.uow_factory, self.wallet, self.achievement_domain)

    @cached_property
    def history(self) -> HistoryService:
        return HistoryService(self.uow_factory)

    @cached_property
    def favorites(self) -> FavoriteService:
        return FavoriteService(self.uow_factory)

    @cached_property
    def notifications(self) -> NotificationService:
        return NotificationService(self.uow_factory, self.dispatcher)

    @cached_property
    def payments(self) -> PaymentService:
        return PaymentService(
            uow_factory=self.uow_factory,
            gateways=self.gateways,
            subscriptions=self.subscriptions,
            wallet=self.wallet,
            promos=self.promos,
            encryptor=self.encryptor,
            settings=self.settings,
        )

    @cached_property
    def statistics(self) -> StatisticsService:
        return StatisticsService(self.uow_factory, self.settings)

    @cached_property
    def admin(self) -> AdminService:
        return AdminService(
            uow_factory=self.uow_factory,
            subscriptions=self.subscriptions,
            promo_service=self.promo_domain,
            dispatcher=self.dispatcher,
            settings=self.settings,
        )

    @cached_property
    def security(self) -> SecurityService:
        return SecurityService(
            limiter=self.rate_limiter,
            cache=self.cache,
            uow_factory=self.uow_factory,
            settings=self.settings,
        )

    # ------------------------------------------------------------------ #
    # Telegram
    # ------------------------------------------------------------------ #
    def bot(self) -> Bot:
        """Lazily create the shared :class:`aiogram.Bot` instance."""
        if self._bot is None:
            token = self.settings.telegram.bot_token.get_secret_value()
            if not token:
                raise RuntimeError("TELEGRAM__BOT_TOKEN is not configured")
            kwargs: dict[str, Any] = {
                "token": token,
                "default": DefaultBotProperties(parse_mode=ParseMode.HTML),
            }
            if self.settings.telegram.api_server:
                from aiogram.client.session.aiohttp import (
                    AiohttpSession,
                )
                from aiogram.client.telegram import TelegramAPIServer

                kwargs["session"] = AiohttpSession(
                    api=TelegramAPIServer.from_base(self.settings.telegram.api_server)
                )
            self._bot = Bot(**kwargs)
        return self._bot

    def set_delivery(self, delivery: MediaDelivery) -> None:
        """Attach the Telegram delivery adapter (used by the worker entry point)."""
        self._delivery = delivery
        self.__dict__.pop("downloads", None)  # rebuild with the new collaborator

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.shutdown()

    async def shutdown(self) -> None:
        """Close every resource that holds a socket or a file handle."""
        if self._bot is not None:
            await self._bot.session.close()
            self._bot = None
        if "redis" in self.__dict__:
            await self.redis.aclose()
        if "progress_publisher" in self.__dict__:
            self.progress_publisher.close()
        dispatcher = self.__dict__.get("dispatcher")
        if isinstance(dispatcher, InlineDispatcher):
            await dispatcher.drain()
        if "engine" in self.__dict__:
            await self.engine.dispose()


def build_container(**kwargs: Any) -> Container:
    """Factory used by entry points and tests."""
    return Container(**kwargs)
