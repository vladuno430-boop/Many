"""Integration tests for the application services (no network, no broker)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediabot.core.exceptions import (
    InsufficientFundsError,
    PromoCodeAlreadyUsedError,
    UserBannedError,
)
from mediabot.domain.enums import (
    Language,
    PromoStatus,
    PromoType,
    SubscriptionTier,
    TransactionReason,
    UserStatus,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def wallet_service(uow_factory, cache, economy_service, settings):
    from mediabot.application.services.economy_service import WalletService

    return WalletService(uow_factory, cache, economy_service, settings)


@pytest.fixture
async def subscription_service(uow_factory, cache):
    from mediabot.application.services.user_service import SubscriptionService

    return SubscriptionService(uow_factory, cache)


@pytest.fixture
async def referral_service(uow_factory, wallet_service, economy_service, settings):
    from mediabot.application.services.economy_service import ReferralService

    return ReferralService(uow_factory, wallet_service, economy_service, settings)


@pytest.fixture
async def promo_redemption(uow_factory, promo_service, wallet_service, settings):
    from mediabot.application.services.economy_service import PromoRedemptionService

    return PromoRedemptionService(uow_factory, promo_service, wallet_service, settings)


class TestUserService:
    async def test_registration_is_idempotent(self, user_service) -> None:
        first = await user_service.get_or_create(user_id=1, username="a", language_code="ru")
        second = await user_service.get_or_create(user_id=1, username="a2", language_code="ru")
        assert first.id == second.id
        assert second.username == "a2"

    async def test_language_is_parsed_from_telegram(self, user_service) -> None:
        user = await user_service.get_or_create(user_id=2, language_code="de-DE")
        assert user.language is Language.DE

    async def test_unknown_language_falls_back_to_english(self, user_service) -> None:
        user = await user_service.get_or_create(user_id=3, language_code="zz")
        assert user.language is Language.EN

    async def test_root_admins_are_promoted(self, user_service) -> None:
        user = await user_service.get_or_create(user_id=1)  # id 1 is a root admin in tests
        assert user.is_admin

    async def test_referral_payload_links_the_inviter(self, user_service) -> None:
        inviter = await user_service.get_or_create(user_id=100)
        invitee = await user_service.get_or_create(user_id=200, referral_code=inviter.referral_code)
        assert invitee.referrer_id == inviter.id

    async def test_self_invite_is_ignored(self, user_service) -> None:
        user = await user_service.get_or_create(user_id=300)
        again = await user_service.get_or_create(user_id=301, referral_code=user.referral_code)
        assert again.referrer_id == 300

    async def test_banned_user_is_rejected(self, user_service, uow_factory) -> None:
        await user_service.get_or_create(user_id=400)
        async with uow_factory.transaction() as uow:
            user = await uow.users.get(400)
            user.status = UserStatus.BANNED
            user.ban_reason = "spam"

        async with uow_factory() as uow:
            user = await uow.users.get(400)
        with pytest.raises(UserBannedError):
            await user_service.ensure_not_restricted(user)

    async def test_expired_ban_is_lifted_automatically(self, user_service, uow_factory) -> None:
        await user_service.get_or_create(user_id=401)
        async with uow_factory.transaction() as uow:
            user = await uow.users.get(401)
            user.status = UserStatus.BANNED
            user.banned_until = datetime.now(UTC) - timedelta(hours=1)

        async with uow_factory() as uow:
            user = await uow.users.get(401)
        await user_service.ensure_not_restricted(user)

        async with uow_factory() as uow:
            refreshed = await uow.users.get(401)
        assert refreshed.status is UserStatus.ACTIVE

    async def test_context_reflects_tier_and_usage(self, user_service) -> None:
        await user_service.get_or_create(user_id=500)
        context = await user_service.build_context(500)
        assert context.tier is SubscriptionTier.FREE
        assert context.policy.daily_downloads == 10
        assert context.usage.downloads_today == 0
        assert context.remaining_downloads == 10

    async def test_preferences_are_persisted(self, user_service) -> None:
        await user_service.get_or_create(user_id=600)
        await user_service.update_preferences(
            600, default_video_format="mkv", notifications_enabled=False, unknown_field="x"
        )
        user = await user_service.get(600)
        assert user.default_video_format == "mkv"
        assert user.notifications_enabled is False


class TestSubscriptionService:
    async def test_grant_upgrades_the_user_tier(self, user_service, subscription_service) -> None:
        await user_service.get_or_create(user_id=10)
        await subscription_service.grant(user_id=10, tier=SubscriptionTier.PREMIUM, days=30)
        user = await user_service.get(10)
        assert user.tier is SubscriptionTier.PREMIUM

    async def test_extending_the_same_tier_moves_the_expiry(
        self, user_service, subscription_service
    ) -> None:
        await user_service.get_or_create(user_id=11)
        first = await subscription_service.grant(user_id=11, tier=SubscriptionTier.PREMIUM, days=10)
        first_expiry = first.expires_at
        second = await subscription_service.grant(
            user_id=11, tier=SubscriptionTier.PREMIUM, days=10
        )
        assert second.id == first.id
        assert second.expires_at > first_expiry

    async def test_lifetime_has_no_expiry(self, user_service, subscription_service) -> None:
        await user_service.get_or_create(user_id=12)
        subscription = await subscription_service.grant(
            user_id=12, tier=SubscriptionTier.LIFETIME, days=0
        )
        assert subscription.expires_at is None
        assert subscription.is_lifetime

    async def test_revoke_drops_back_to_free(self, user_service, subscription_service) -> None:
        await user_service.get_or_create(user_id=13)
        await subscription_service.grant(user_id=13, tier=SubscriptionTier.VIP, days=30)
        await subscription_service.revoke(13)
        user = await user_service.get(13)
        assert user.tier is SubscriptionTier.FREE

    async def test_expiry_downgrades_the_user(
        self, user_service, subscription_service, uow_factory
    ) -> None:
        await user_service.get_or_create(user_id=14)
        subscription = await subscription_service.grant(
            user_id=14, tier=SubscriptionTier.PREMIUM, days=1
        )
        async with uow_factory.transaction() as uow:
            row = await uow.subscriptions.get(subscription.id)
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        affected = await subscription_service.expire_due()
        assert affected == [14]
        user = await user_service.get(14)
        assert user.tier is SubscriptionTier.FREE


class TestWalletService:
    async def test_credit_then_debit(self, user_service, wallet_service) -> None:
        await user_service.get_or_create(user_id=20)
        assert await wallet_service.credit(20, 100, TransactionReason.DAILY_BONUS) == 100
        assert await wallet_service.debit(20, 40, TransactionReason.SPEND_DOWNLOADS) == 60
        assert await wallet_service.balance(20) == 60

    async def test_debit_beyond_the_balance_is_refused(self, user_service, wallet_service) -> None:
        await user_service.get_or_create(user_id=21)
        with pytest.raises(InsufficientFundsError):
            await wallet_service.debit(21, 10, TransactionReason.SPEND_DOWNLOADS)
        assert await wallet_service.balance(21) == 0

    async def test_shop_purchase_grants_extra_downloads(
        self, user_service, wallet_service, uow_factory
    ) -> None:
        await user_service.get_or_create(user_id=22)
        await wallet_service.credit(22, 1000, TransactionReason.ADMIN_ADJUSTMENT)
        await wallet_service.purchase(22, "downloads_5")

        async with uow_factory() as uow:
            limits = await uow.limits.for_user(22)
        assert limits is not None
        assert limits.extra_daily_downloads == 5
        assert await wallet_service.balance(22) == 940

    async def test_statement_lists_operations(self, user_service, wallet_service) -> None:
        await user_service.get_or_create(user_id=23)
        await wallet_service.credit(23, 50, TransactionReason.REFERRAL_BONUS)
        statement = await wallet_service.statement(23)
        assert statement[0]["amount"] == 50
        assert statement[0]["reason"] == TransactionReason.REFERRAL_BONUS.value


class TestDailyBonus:
    @pytest.fixture
    async def daily(self, uow_factory, wallet_service, economy_service):
        from mediabot.application.services.economy_service import DailyBonusService

        return DailyBonusService(uow_factory, wallet_service, economy_service)

    async def test_first_claim_pays_out(self, user_service, daily, wallet_service) -> None:
        await user_service.get_or_create(user_id=30)
        result = await daily.claim(30)
        assert result.granted
        assert await wallet_service.balance(30) == result.coins

    async def test_second_claim_same_day_is_refused(self, user_service, daily) -> None:
        await user_service.get_or_create(user_id=31)
        await daily.claim(31)
        second = await daily.claim(31)
        assert not second.granted


class TestReferralService:
    async def test_payout_is_idempotent(
        self, user_service, referral_service, wallet_service
    ) -> None:
        inviter = await user_service.get_or_create(user_id=40)
        await user_service.get_or_create(user_id=41, referral_code=inviter.referral_code)

        assert await referral_service.register(41) is True
        assert await referral_service.register(41) is False

        assert await wallet_service.balance(40) == 50
        assert await wallet_service.balance(41) == 25
        stats = await referral_service.stats(40)
        assert stats["total"] == 1

    async def test_user_without_inviter_is_a_noop(self, user_service, referral_service) -> None:
        await user_service.get_or_create(user_id=42)
        assert await referral_service.register(42) is False

    def test_link_uses_the_bot_username(self, referral_service) -> None:
        assert referral_service.link_for("REF123") == "https://t.me/test_bot?start=REF123"


class TestPromoRedemption:
    async def _create_promo(self, uow_factory, **kwargs) -> None:
        payload = {
            "code": "FREE100",
            "promo_type": PromoType.COINS,
            "status": PromoStatus.ACTIVE,
            "value": 100,
            "per_user_limit": 1,
            "max_activations": 5,
        }
        payload.update(kwargs)
        async with uow_factory.transaction() as uow:
            await uow.promos.create(**payload)

    async def test_coins_are_credited(
        self, user_service, uow_factory, promo_redemption, wallet_service
    ) -> None:
        await user_service.get_or_create(user_id=50)
        await self._create_promo(uow_factory)

        redemption = await promo_redemption.redeem(50, "free100")
        assert redemption.coins == 100
        assert await wallet_service.balance(50) == 100

    async def test_a_code_cannot_be_used_twice(
        self, user_service, uow_factory, promo_redemption
    ) -> None:
        await user_service.get_or_create(user_id=51)
        await self._create_promo(uow_factory)
        await promo_redemption.redeem(51, "FREE100")
        with pytest.raises(PromoCodeAlreadyUsedError):
            await promo_redemption.redeem(51, "FREE100")

    async def test_limit_boost_creates_an_override(
        self, user_service, uow_factory, promo_redemption
    ) -> None:
        await user_service.get_or_create(user_id=52)
        await self._create_promo(
            uow_factory, code="BOOST", promo_type=PromoType.LIMIT_BOOST, value=25
        )
        await promo_redemption.redeem(52, "BOOST")

        async with uow_factory() as uow:
            limits = await uow.limits.for_user(52)
        assert limits is not None
        assert limits.extra_daily_downloads == 25

    async def test_activation_counter_is_incremented(
        self, user_service, uow_factory, promo_redemption
    ) -> None:
        await user_service.get_or_create(user_id=53)
        await self._create_promo(uow_factory, code="ONCE", max_activations=1)
        await promo_redemption.redeem(53, "ONCE")

        async with uow_factory() as uow:
            promo = await uow.promos.by_code("ONCE")
        assert promo.activations_used == 1
        assert promo.status is PromoStatus.EXHAUSTED

    async def test_discount_preview_does_not_consume_the_code(
        self, user_service, uow_factory, promo_redemption
    ) -> None:
        await user_service.get_or_create(user_id=54)
        await self._create_promo(
            uow_factory, code="SALE", promo_type=PromoType.PERCENT_DISCOUNT, value=50
        )
        price, code = await promo_redemption.preview_discount("SALE", 200, 54)
        assert (price, code) == (100, "SALE")

        async with uow_factory() as uow:
            promo = await uow.promos.by_code("SALE")
        assert promo.activations_used == 0


class TestAchievementsService:
    @pytest.fixture
    async def achievements(self, uow_factory, wallet_service):
        from mediabot.application.services.economy_service import AchievementsService
        from mediabot.domain.services.economy import AchievementService

        return AchievementsService(uow_factory, wallet_service, AchievementService())

    async def test_unlock_pays_the_reward_once(
        self, user_service, uow_factory, achievements, wallet_service
    ) -> None:
        await user_service.get_or_create(user_id=60)
        async with uow_factory.transaction() as uow:
            user = await uow.users.get(60)
            user.total_downloads = 10

        unlocked = await achievements.evaluate(60)
        codes = {definition.code for definition in unlocked}
        assert {"first_download", "downloads_10"} <= codes
        balance = await wallet_service.balance(60)

        assert await achievements.evaluate(60) == []
        assert await wallet_service.balance(60) == balance


class TestSecurityService:
    @pytest.fixture
    async def security(self, rate_limiter, cache, uow_factory, settings):
        from mediabot.application.services.security_service import SecurityService

        return SecurityService(
            limiter=rate_limiter, cache=cache, uow_factory=uow_factory, settings=settings
        )

    async def test_flood_control_blocks_a_burst(self, user_service, security) -> None:
        from mediabot.core.exceptions import FloodDetectedError

        await user_service.get_or_create(user_id=70)
        with pytest.raises(FloodDetectedError):
            for _ in range(20):
                await security.check(70)

    async def test_captcha_round_trip(self, user_service, security, uow_factory) -> None:
        await user_service.get_or_create(user_id=71)
        challenge = await security.issue_captcha(71)
        assert challenge.answer in challenge.options
        assert not await security.verify_captcha(71, challenge.answer + 1)

        challenge = await security.issue_captcha(71)
        assert await security.verify_captcha(71, challenge.answer)

        async with uow_factory() as uow:
            user = await uow.users.get(71)
        assert user.violations == 0
        assert user.captcha_passed_at is not None

    def test_automation_heuristics(self, security) -> None:
        assert security.looks_automated(is_bot=True, username=None, message_interval_ms=None)
        assert security.looks_automated(is_bot=False, username="x", message_interval_ms=50)
        assert not security.looks_automated(
            is_bot=False, username="human", message_interval_ms=1500
        )
