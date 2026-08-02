"""User and subscription orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mediabot.application.dto import UserContext
from mediabot.core.config import Settings
from mediabot.core.exceptions import NotFoundError, UserBannedError, UserMutedError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import (
    Language,
    SubscriptionStatus,
    SubscriptionTier,
    UserStatus,
)
from mediabot.domain.services.economy import EconomyService
from mediabot.domain.services.limits import LimitOverrides, LimitService
from mediabot.domain.value_objects import UsageSnapshot
from mediabot.infrastructure.cache.redis_cache import CacheService
from mediabot.infrastructure.db.models.billing import Subscription
from mediabot.infrastructure.db.models.user import User
from mediabot.infrastructure.db.session import UnitOfWork, UnitOfWorkFactory

log = get_logger(LogChannel.APP, component="user_service")


class UserService:
    """Registration, profile access and moderation state of end users."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        cache: CacheService,
        settings: Settings,
        limit_service: LimitService,
        economy: EconomyService,
    ) -> None:
        self._uow_factory = uow_factory
        self._cache = cache
        self._settings = settings
        self._limits = limit_service
        self._economy = economy

    async def get_or_create(
        self,
        *,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_premium_telegram: bool = False,
        referral_code: str | None = None,
    ) -> User:
        """Return the user, creating it (and its wallet) on first contact.

        Also refreshes the mutable Telegram profile fields, so a renamed user
        stays searchable in the admin panel.
        """
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                referrer_id = await self._resolve_referrer(uow, referral_code, user_id)
                user = await uow.users.create_user(
                    user_id=user_id,
                    referral_code=self._economy.referral_code(user_id),
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    language=Language.parse(language_code),
                    referrer_id=referrer_id,
                    is_premium_telegram=is_premium_telegram,
                )
                if user_id in self._settings.telegram.root_admin_ids:
                    user.is_admin = True
                log.info("registered user id={} referrer={}", user_id, referrer_id)
            else:
                user.username = username
                user.first_name = first_name
                user.last_name = last_name
                user.is_premium_telegram = is_premium_telegram
                user.last_seen_at = datetime.now(UTC)
            await uow.commit()
            await self._cache.invalidate_user(user_id)
            return user

    async def _resolve_referrer(
        self,
        uow: UnitOfWork,
        referral_code: str | None,
        user_id: int,
    ) -> int | None:
        """Translate a start payload into an inviter id (self-invites ignored)."""
        if not referral_code:
            return None
        inviter = await uow.users.get_by_referral_code(referral_code.strip().upper())
        if inviter is None or inviter.id == user_id:
            return None
        return int(inviter.id)

    async def get(self, user_id: int) -> User:
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            return user

    async def set_language(self, user_id: int, language: Language) -> None:
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            user.language = language
        await self._cache.invalidate_user(user_id)

    async def update_preferences(self, user_id: int, **preferences: object) -> None:
        """Persist default quality/format and notification preferences."""
        allowed = {
            "default_video_quality",
            "default_audio_quality",
            "default_video_format",
            "default_audio_format",
            "auto_download",
            "notifications_enabled",
            "promo_notifications_enabled",
        }
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            for key, value in preferences.items():
                if key in allowed:
                    setattr(user, key, value)
        await self._cache.invalidate_user(user_id)

    async def ensure_not_restricted(self, user: User) -> None:
        """Raise when the user is banned or currently muted."""
        now = datetime.now(UTC)
        if user.status is UserStatus.BANNED:
            if user.banned_until and user.banned_until <= now:
                await self._clear_restriction(user.id, unban=True)
            else:
                raise UserBannedError(user.ban_reason or "Account is banned")
        if user.status is UserStatus.MUTED:
            if user.muted_until and user.muted_until <= now:
                await self._clear_restriction(user.id, unban=False)
            else:
                raise UserMutedError("Account is muted")

    async def _clear_restriction(self, user_id: int, *, unban: bool) -> None:
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                return
            user.status = UserStatus.ACTIVE
            if unban:
                user.banned_until = None
                user.ban_reason = None
            else:
                user.muted_until = None
        await self._cache.invalidate_user(user_id)

    async def build_context(self, user_id: int) -> UserContext:
        """Assemble the :class:`UserContext` used by every bot handler."""
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")

            overrides = await self._load_overrides(uow, user_id)
            policy = self._limits.effective_policy(SubscriptionTier(user.tier), overrides)

            window_start = self._window_start()
            usage = UsageSnapshot(
                downloads_today=await uow.downloads.count_today(user_id, since=window_start),
                bytes_today=await uow.downloads.bytes_today(user_id, since=window_start),
                active_jobs=await uow.downloads.count_active(user_id),
                queued_jobs=await uow.queue.user_queued(user_id),
                window_started_at=window_start,
            )
            wallet = await uow.wallets.for_user(user_id)
            now = datetime.now(UTC)
            ads_enabled = policy.ads_enabled and not (
                user.ads_disabled_until and user.ads_disabled_until > now
            )
            return UserContext(
                user_id=user_id,
                language=Language(user.language),
                tier=SubscriptionTier(user.tier),
                is_admin=bool(user.is_admin) or user_id in self._settings.telegram.root_admin_ids,
                is_banned=user.status is UserStatus.BANNED,
                is_muted=user.status is UserStatus.MUTED,
                banned_until=user.banned_until,
                muted_until=user.muted_until,
                balance=wallet.balance if wallet else 0,
                policy=policy,
                usage=usage,
                referral_code=user.referral_code,
                ads_enabled=ads_enabled,
            )

    async def _load_overrides(self, uow: UnitOfWork, user_id: int) -> LimitOverrides:
        row = await uow.limits.for_user(user_id)
        if row is None:
            return LimitOverrides()
        if row.expires_at and row.expires_at <= datetime.now(UTC):
            return LimitOverrides()
        return LimitOverrides(
            extra_daily_downloads=row.extra_daily_downloads,
            extra_file_size_bytes=row.extra_file_size_bytes,
            daily_downloads_absolute=row.daily_downloads_absolute,
            max_file_size_absolute=row.max_file_size_absolute,
            max_duration_absolute=row.max_duration_absolute,
            max_concurrent_absolute=row.max_concurrent_absolute,
            unlimited=row.unlimited,
            ads_disabled=row.ads_disabled,
        )

    @staticmethod
    def _window_start() -> datetime:
        """Quota windows are UTC calendar days."""
        now = datetime.now(UTC)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)


class SubscriptionService:
    """Grants, extensions and expiry of subscriptions."""

    def __init__(self, uow_factory: UnitOfWorkFactory, cache: CacheService) -> None:
        self._uow_factory = uow_factory
        self._cache = cache

    async def current(self, user_id: int) -> Subscription | None:
        async with self._uow_factory() as uow:
            return await uow.subscriptions.active_for_user(user_id)

    async def grant(
        self,
        *,
        user_id: int,
        tier: SubscriptionTier,
        days: int,
        source: str = "purchase",
        payment_id: int | None = None,
        admin_id: int | None = None,
        note: str | None = None,
    ) -> Subscription:
        """Grant or extend a subscription.

        Extending the *same* tier pushes the existing expiry forward instead of
        creating overlapping periods; upgrading to a higher tier starts a new
        period immediately (the old one keeps running underneath and simply
        stops mattering, because the effective tier is the highest active one).
        """
        now = datetime.now(UTC)
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")

            existing = await uow.subscriptions.active_for_user(user_id)
            lifetime = tier is SubscriptionTier.LIFETIME
            expires_at = None if lifetime else now + timedelta(days=max(days, 1))

            if (
                existing is not None
                and SubscriptionTier(existing.tier) is tier
                and existing.expires_at is not None
                and not lifetime
            ):
                base = max(existing.expires_at, now)
                existing.expires_at = base + timedelta(days=max(days, 1))
                subscription = existing
            else:
                subscription = await uow.subscriptions.create(
                    user_id=user_id,
                    tier=tier,
                    status=SubscriptionStatus.ACTIVE,
                    starts_at=now,
                    expires_at=expires_at,
                    source=source,
                    payment_id=payment_id,
                    granted_by_admin_id=admin_id,
                    note=note,
                )

            if SubscriptionTier(user.tier).rank < tier.rank:
                user.tier = tier
            log.info(
                "subscription granted user={} tier={} days={} source={}",
                user_id,
                tier.value,
                days,
                source,
            )
        await self._cache.invalidate_user(user_id)
        return subscription

    async def revoke(self, user_id: int, *, admin_id: int | None = None) -> None:
        """Cancel every active subscription and drop the user back to free."""
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            for subscription in await uow.subscriptions.history_for_user(user_id, limit=100):
                if subscription.status is SubscriptionStatus.ACTIVE:
                    subscription.status = SubscriptionStatus.CANCELLED
                    subscription.note = f"revoked by admin {admin_id}" if admin_id else "revoked"
            user.tier = SubscriptionTier.FREE
        await self._cache.invalidate_user(user_id)

    async def expire_due(self) -> list[int]:
        """Expire elapsed subscriptions and downgrade the affected users."""
        async with self._uow_factory.transaction() as uow:
            user_ids = list(await uow.subscriptions.expire_due())
            for user_id in user_ids:
                remaining = await uow.subscriptions.active_for_user(user_id)
                user = await uow.users.get(user_id)
                if user is None:
                    continue
                user.tier = (
                    SubscriptionTier(remaining.tier)
                    if remaining is not None
                    else SubscriptionTier.FREE
                )
        for user_id in user_ids:
            await self._cache.invalidate_user(user_id)
        if user_ids:
            log.info("expired {} subscriptions", len(user_ids))
        return user_ids

    async def expiring_soon(self, *, within_hours: int = 24) -> list[Subscription]:
        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            rows = await uow.subscriptions.expiring_between(
                now, now + timedelta(hours=within_hours)
            )
            return list(rows)
