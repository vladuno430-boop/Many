"""Coins, daily bonuses, referrals, promo redemption and achievements."""

from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from mediabot.application.dto import PromoRedemption
from mediabot.core.config import Settings
from mediabot.core.exceptions import (
    ConflictError,
    InsufficientFundsError,
    NotFoundError,
    ValidationError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import (
    AchievementCode,
    NotificationType,
    PromoStatus,
    PromoType,
    SubscriptionTier,
    TransactionReason,
    TransactionType,
)
from mediabot.domain.services.economy import (
    AchievementService as DomainAchievementService,
)
from mediabot.domain.services.economy import (
    DailyBonusResult,
    EconomyService,
)
from mediabot.domain.services.promo import PromoService, PromoSnapshot
from mediabot.domain.value_objects import AchievementDefinition
from mediabot.infrastructure.cache.redis_cache import CacheService
from mediabot.infrastructure.db.models.billing import PromoCode
from mediabot.infrastructure.db.session import UnitOfWorkFactory

log = get_logger(LogChannel.APP, component="economy")


class WalletService:
    """Coin balance operations and the coin shop."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        cache: CacheService,
        economy: EconomyService,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._cache = cache
        self._economy = economy
        self._settings = settings

    async def balance(self, user_id: int) -> int:
        async with self._uow_factory() as uow:
            wallet = await uow.wallets.for_user(user_id)
            return wallet.balance if wallet else 0

    async def credit(
        self,
        user_id: int,
        amount: int,
        reason: TransactionReason,
        *,
        comment: str | None = None,
        reference: str | None = None,
    ) -> int:
        """Add coins and return the new balance."""
        if amount <= 0:
            raise ValidationError("Credit amount must be positive")
        async with self._uow_factory.transaction() as uow:
            transaction = await uow.wallets.apply(
                user_id=user_id,
                amount=amount,
                transaction_type=TransactionType.CREDIT,
                reason=reason,
                comment=comment,
                reference=reference,
            )
            balance = transaction.balance_after
        await self._cache.invalidate_user(user_id)
        return balance

    async def debit(
        self,
        user_id: int,
        amount: int,
        reason: TransactionReason,
        *,
        comment: str | None = None,
        reference: str | None = None,
    ) -> int:
        """Spend coins; raises when the balance is insufficient."""
        if amount <= 0:
            raise ValidationError("Debit amount must be positive")
        async with self._uow_factory.transaction() as uow:
            wallet = await uow.wallets.get_or_create(user_id)
            if wallet.balance < amount:
                raise InsufficientFundsError(
                    "Not enough coins", required=amount, balance=wallet.balance
                )
            transaction = await uow.wallets.apply(
                user_id=user_id,
                amount=amount,
                transaction_type=TransactionType.DEBIT,
                reason=reason,
                comment=comment,
                reference=reference,
            )
            balance = transaction.balance_after
        await self._cache.invalidate_user(user_id)
        return balance

    async def purchase(self, user_id: int, item: str) -> str:
        """Buy a coin-shop item and apply its effect.

        Returns a short summary key that the presentation layer translates.
        """
        purchase = self._economy.resolve_purchase(item)
        if purchase is None:
            raise NotFoundError(f"Unknown shop item: {item}")

        await self.debit(
            user_id,
            purchase.price,
            TransactionReason.SPEND_PREMIUM if purchase.tier else TransactionReason.SPEND_DOWNLOADS,
            comment=f"shop:{item}",
            reference=item,
        )

        async with self._uow_factory.transaction() as uow:
            if purchase.extra_downloads or purchase.extra_file_size_bytes:
                row = await uow.limits.for_user(user_id)
                extra_downloads = purchase.extra_downloads + (
                    row.extra_daily_downloads if row else 0
                )
                extra_size = purchase.extra_file_size_bytes + (
                    row.extra_file_size_bytes if row else 0
                )
                await uow.limits.upsert(
                    user_id,
                    extra_daily_downloads=extra_downloads,
                    extra_file_size_bytes=extra_size,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    note=f"shop:{item}",
                )
            if purchase.no_ads_days:
                user = await uow.users.get(user_id)
                if user is not None:
                    base = max(user.ads_disabled_until or datetime.now(UTC), datetime.now(UTC))
                    user.ads_disabled_until = base + timedelta(days=purchase.no_ads_days)
        await self._cache.invalidate_user(user_id)
        return item

    async def statement(
        self, user_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        async with self._uow_factory() as uow:
            rows = await uow.wallets.transactions(user_id, limit=limit, offset=offset)
            return [
                {
                    "id": row.id,
                    "type": row.type.value,
                    "reason": row.reason.value,
                    "amount": row.amount,
                    "balance_after": row.balance_after,
                    "comment": row.comment,
                    "created_at": row.created_at,
                }
                for row in rows
            ]


class DailyBonusService:
    """Daily reward with streaks and a reward calendar."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        wallet: WalletService,
        economy: EconomyService,
    ) -> None:
        self._uow_factory = uow_factory
        self._wallet = wallet
        self._economy = economy

    async def claim(self, user_id: int) -> DailyBonusResult:
        """Claim today's bonus (idempotent within a calendar day)."""
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            result = self._economy.claim_daily_bonus(
                last_claim_at=user.last_daily_bonus_at,
                current_streak=user.daily_streak,
            )
        if not result.granted:
            return result

        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:  # pragma: no cover - deleted mid-flight
                raise NotFoundError(f"User {user_id} not found")
            user.daily_streak = result.streak
            user.max_daily_streak = max(user.max_daily_streak, result.streak)
            user.last_daily_bonus_at = datetime.now(UTC)
        await self._wallet.credit(
            user_id,
            result.coins,
            TransactionReason.DAILY_BONUS,
            comment=f"streak:{result.streak}",
        )
        return result

    def calendar(self, streak: int) -> tuple[tuple[int, int, bool], ...]:
        return self._economy.daily_calendar(streak)


class ReferralService:
    """Referral programme: link generation and reward payout."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        wallet: WalletService,
        economy: EconomyService,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._wallet = wallet
        self._economy = economy
        self._settings = settings

    def link_for(self, referral_code: str) -> str:
        username = self._settings.telegram.bot_username.lstrip("@")
        return f"https://t.me/{username}?start={referral_code}"

    async def register(self, invitee_id: int) -> bool:
        """Pay out the referral bonus for a freshly registered invitee.

        Idempotent: a second call for the same invitee is a no-op, so a retried
        ``/start`` can never double-pay.
        """
        async with self._uow_factory.transaction() as uow:
            invitee = await uow.users.get(invitee_id)
            if invitee is None or invitee.referrer_id is None:
                return False
            if await uow.referrals.get_for_invitee(invitee_id) is not None:
                return False

            inviter_id = int(invitee.referrer_id)
            inviter = await uow.users.get(inviter_id)
            if inviter is None:
                return False

            inviter.referral_count += 1
            reward = self._economy.referral_reward(inviter.referral_count)
            await uow.referrals.create(
                inviter_id=inviter_id,
                invitee_id=invitee_id,
                coins_awarded=reward.inviter_coins,
                premium_days_awarded=reward.inviter_premium_days,
                source="start_payload",
            )
            await uow.notifications.enqueue(
                user_id=inviter_id,
                notification_type=NotificationType.SYSTEM_UPDATE,
                body="referral_joined",
                payload={
                    "invitee_id": invitee_id,
                    "coins": reward.inviter_coins,
                    "premium_days": reward.inviter_premium_days,
                },
            )

        await self._wallet.credit(
            inviter_id,
            reward.inviter_coins,
            TransactionReason.REFERRAL_BONUS,
            comment=f"invitee:{invitee_id}",
        )
        await self._wallet.credit(
            invitee_id,
            reward.invitee_coins,
            TransactionReason.REFERRAL_JOIN,
            comment=f"inviter:{inviter_id}",
        )
        log.info("referral registered inviter={} invitee={}", inviter_id, invitee_id)
        return True

    async def stats(self, user_id: int) -> dict[str, int]:
        async with self._uow_factory() as uow:
            total = await uow.referrals.count_for_inviter(user_id)
            rows = await uow.referrals.list_for_inviter(user_id, limit=1000)
            coins = sum(row.coins_awarded for row in rows)
            premium_days = sum(row.premium_days_awarded for row in rows)
        return {"total": total, "coins": coins, "premium_days": premium_days}


class PromoRedemptionService:
    """Validate and redeem promo codes, then apply their rewards."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        promo_service: PromoService,
        wallet: WalletService,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._promos = promo_service
        self._wallet = wallet
        self._settings = settings

    async def redeem(self, user_id: int, raw_code: str) -> PromoRedemption:
        """Redeem ``raw_code`` for ``user_id``.

        Subscription rewards are *not* applied here — the caller passes the
        result to :class:`SubscriptionService`, keeping this service free of a
        circular dependency.
        """
        code = self._promos.normalize(raw_code)
        if not code or len(code) > 32:
            raise ValidationError("Invalid promo code")

        async with self._uow_factory.transaction() as uow:
            promo = await uow.promos.by_code(code)
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")

            snapshot = self._to_snapshot(promo) if promo is not None else None
            activations = (
                await uow.promo_activations.count_for_user(promo.id, user_id) if promo else 0
            )
            user_is_new = (datetime.now(UTC) - user.created_at.replace(tzinfo=UTC)) < timedelta(
                days=3
            )
            self._promos.validate(
                snapshot,
                user_activations=activations,
                user_tier=SubscriptionTier(user.tier),
                user_is_new=user_is_new,
            )
            assert promo is not None and snapshot is not None  # validated above

            reward = self._promos.reward_for(snapshot)
            summary = self._summarize(snapshot.promo_type, snapshot.value)
            await uow.promo_activations.create(
                promo_id=promo.id,
                user_id=user_id,
                code=code,
                reward_summary=summary,
                coins_granted=reward.coins,
                days_granted=reward.premium_days + reward.vip_days,
            )
            promo.activations_used += 1
            promo.status = self._promos.next_status(
                replace(snapshot, activations_used=promo.activations_used)
            )

            if reward.extra_daily_downloads:
                row = await uow.limits.for_user(user_id)
                await uow.limits.upsert(
                    user_id,
                    extra_daily_downloads=(row.extra_daily_downloads if row else 0)
                    + reward.extra_daily_downloads,
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                    note=f"promo:{code}",
                )

        if reward.coins:
            await self._wallet.credit(
                user_id,
                reward.coins,
                TransactionReason.PROMO_CODE,
                comment=f"promo:{code}",
                reference=code,
            )

        log.info("promo redeemed user={} code={} type={}", user_id, code, snapshot.promo_type.value)
        return PromoRedemption(
            code=code,
            summary=summary,
            coins=reward.coins,
            premium_days=reward.premium_days,
            vip_days=reward.vip_days,
            lifetime=reward.lifetime,
            extra_downloads=reward.extra_daily_downloads,
            discount_percent=reward.percent_discount,
            discount_fixed=reward.fixed_discount,
        )

    async def preview_discount(self, code: str, price: int, user_id: int) -> tuple[int, str | None]:
        """Return ``(discounted_price, code)`` without consuming an activation."""
        normalized = self._promos.normalize(code)
        async with self._uow_factory() as uow:
            promo = await uow.promos.by_code(normalized)
            user = await uow.users.get(user_id)
            if promo is None or user is None:
                return price, None
            snapshot = self._to_snapshot(promo)
            activations = await uow.promo_activations.count_for_user(promo.id, user_id)
            try:
                self._promos.validate(
                    snapshot,
                    user_activations=activations,
                    user_tier=SubscriptionTier(user.tier),
                    user_is_new=False,
                )
            except Exception:
                return price, None
        reward = self._promos.reward_for(snapshot)
        if not reward.is_discount:
            return price, None
        return self._promos.apply_discount(price, reward), normalized

    @staticmethod
    def _to_snapshot(promo: PromoCode) -> PromoSnapshot:
        return PromoSnapshot(
            code=promo.code,
            promo_type=PromoType(promo.promo_type),
            status=PromoStatus(promo.status),
            value=promo.value,
            max_activations=promo.max_activations,
            activations_used=promo.activations_used,
            per_user_limit=promo.per_user_limit,
            starts_at=promo.starts_at,
            expires_at=promo.expires_at,
            min_tier=SubscriptionTier(promo.min_tier) if promo.min_tier else None,
            new_users_only=promo.new_users_only,
        )

    @staticmethod
    def _summarize(promo_type: PromoType, value: int) -> str:
        return {
            PromoType.COINS: f"+{value} coins",
            PromoType.PREMIUM_DAYS: f"+{value} premium days",
            PromoType.VIP_DAYS: f"+{value} VIP days",
            PromoType.LIFETIME: "lifetime access",
            PromoType.PERCENT_DISCOUNT: f"-{value}%",
            PromoType.FIXED_DISCOUNT: f"-{value}",
            PromoType.LIMIT_BOOST: f"+{value} downloads/day",
        }[promo_type]


class AchievementsService:
    """Unlock achievements and pay out their rewards."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        wallet: WalletService,
        domain_service: DomainAchievementService,
    ) -> None:
        self._uow_factory = uow_factory
        self._wallet = wallet
        self._domain = domain_service

    async def metrics_for(self, user_id: int) -> dict[str, int]:
        """Collect every metric an achievement can be based on."""
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            favorites = await uow.favorites.count(user_id=user_id)
            return {
                "downloads": user.total_downloads,
                "video_downloads": user.total_video_downloads,
                "audio_downloads": user.total_audio_downloads,
                "daily_streak": user.max_daily_streak,
                "referrals": user.referral_count,
                "favorites": favorites,
                "premium_days": 1 if SubscriptionTier(user.tier).is_paid else 0,
                "vip_days": 1 if SubscriptionTier(user.tier) >= SubscriptionTier.VIP else 0,
                "night_downloads": 0,
                "languages_used": 1,
            }

    async def evaluate(self, user_id: int) -> list[AchievementDefinition]:
        """Unlock everything the user has earned; returns the new unlocks."""
        metrics = await self.metrics_for(user_id)
        async with self._uow_factory() as uow:
            unlocked_codes = await uow.achievements.codes_for_user(user_id)
        new_unlocks = self._domain.evaluate(metrics, unlocked_codes)
        if not new_unlocks:
            return []

        async with self._uow_factory.transaction() as uow:
            for definition in new_unlocks:
                await uow.achievements.unlock(user_id, definition.code, definition.reward_coins)
                await uow.notifications.enqueue(
                    user_id=user_id,
                    notification_type=NotificationType.ACHIEVEMENT_UNLOCKED,
                    body=definition.code,
                    payload={"code": definition.code, "coins": definition.reward_coins},
                )
        for definition in new_unlocks:
            if definition.reward_coins:
                await self._wallet.credit(
                    user_id,
                    definition.reward_coins,
                    TransactionReason.ACHIEVEMENT,
                    comment=definition.code,
                    reference=definition.code,
                )
        log.info("user={} unlocked {} achievements", user_id, len(new_unlocks))
        return list(new_unlocks)

    async def progress(self, user_id: int) -> list[tuple[AchievementDefinition, int, bool]]:
        """Every achievement with the user's progress and unlock state."""
        metrics = await self.metrics_for(user_id)
        async with self._uow_factory() as uow:
            unlocked = await uow.achievements.codes_for_user(user_id)
        return [
            (definition, value, definition.code in unlocked)
            for definition, value in self._domain.progress(metrics)
        ]

    def definition(self, code: AchievementCode | str) -> AchievementDefinition | None:
        return self._domain.definition(code)


def generate_broadcast_id() -> str:
    """Random identifier grouping the notifications of one broadcast."""
    return f"bc_{secrets.token_hex(8)}"


__all__ = [
    "AchievementsService",
    "ConflictError",
    "DailyBonusService",
    "PromoRedemptionService",
    "ReferralService",
    "WalletService",
    "generate_broadcast_id",
]
