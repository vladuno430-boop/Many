"""Integration tests for the persistence layer (SQLite + real SQLAlchemy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediabot.domain.enums import (
    DownloadStatus,
    Language,
    MediaKind,
    Platform,
    PromoStatus,
    PromoType,
    SubscriptionStatus,
    SubscriptionTier,
    TransactionReason,
    TransactionType,
    UserStatus,
)

pytestmark = pytest.mark.integration


class TestUserRepository:
    async def test_creates_a_user_with_a_wallet(self, uow_factory) -> None:
        async with uow_factory() as uow:
            user = await uow.users.create_user(
                user_id=1, referral_code="REF1", username="alice", language=Language.RU
            )
            await uow.commit()
            assert user.wallet is not None
            assert user.wallet.balance == 0
            assert user.language is Language.RU

    async def test_search_matches_id_and_username(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=42, referral_code="REF42", username="bob")
            await uow.commit()

            by_username = await uow.users.search("bob")
            by_id = await uow.users.search("42")
            assert [user.id for user in by_username] == [42]
            assert [user.id for user in by_id] == [42]

    async def test_counters_are_incremented_atomically(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=7, referral_code="REF7")
            await uow.commit()
            await uow.users.increment_counters(7, downloads=1, video_downloads=1, size_bytes=500)
            await uow.users.increment_counters(7, downloads=1, audio_downloads=1, size_bytes=250)
            await uow.commit()

            user = await uow.users.get(7)
            assert user is not None
            assert user.total_downloads == 2
            assert user.total_video_downloads == 1
            assert user.total_audio_downloads == 1
            assert user.total_bytes == 750

    async def test_broadcast_targets_respect_opt_out(self, uow_factory) -> None:
        async with uow_factory() as uow:
            opted_in = await uow.users.create_user(user_id=1, referral_code="R1")
            opted_out = await uow.users.create_user(user_id=2, referral_code="R2")
            opted_out.promo_notifications_enabled = False
            banned = await uow.users.create_user(user_id=3, referral_code="R3")
            banned.status = UserStatus.BANNED
            await uow.commit()

            targets = await uow.users.broadcast_targets()
            assert set(targets) == {opted_in.id}

    async def test_rank_reflects_download_counts(self, uow_factory) -> None:
        async with uow_factory() as uow:
            for index, downloads in enumerate([5, 50, 500], start=1):
                user = await uow.users.create_user(user_id=index, referral_code=f"R{index}")
                user.total_downloads = downloads
            await uow.commit()
            assert await uow.users.rank_of(3) == 1
            assert await uow.users.rank_of(1) == 3


class TestWalletRepository:
    async def test_credit_and_debit_keep_the_ledger_consistent(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=1, referral_code="R1")
            await uow.commit()

            await uow.wallets.apply(
                user_id=1,
                amount=100,
                transaction_type=TransactionType.CREDIT,
                reason=TransactionReason.DAILY_BONUS,
            )
            transaction = await uow.wallets.apply(
                user_id=1,
                amount=30,
                transaction_type=TransactionType.DEBIT,
                reason=TransactionReason.SPEND_DOWNLOADS,
            )
            await uow.commit()

            wallet = await uow.wallets.for_user(1)
            assert wallet is not None
            assert wallet.balance == 70
            assert wallet.total_earned == 100
            assert wallet.total_spent == 30
            assert transaction.balance_after == 70

            statement = await uow.wallets.transactions(1)
            assert len(statement) == 2


class TestDownloadRepository:
    async def _user(self, uow) -> None:
        await uow.users.create_user(user_id=1, referral_code="R1")
        await uow.commit()

    async def test_quota_counting_ignores_failed_jobs(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await self._user(uow)
            for status in (
                DownloadStatus.COMPLETED,
                DownloadStatus.QUEUED,
                DownloadStatus.FAILED,
                DownloadStatus.CANCELLED,
            ):
                await uow.downloads.create(
                    user_id=1,
                    url="https://youtube.com/watch?v=1",
                    platform=Platform.YOUTUBE,
                    kind=MediaKind.VIDEO,
                    status=status,
                    target_format="mp4",
                )
            await uow.commit()

            since = datetime.now(UTC) - timedelta(hours=1)
            assert await uow.downloads.count_today(1, since=since) == 2

    async def test_cached_lookup_requires_a_file_id(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await self._user(uow)
            await uow.downloads.create(
                user_id=1,
                url="https://youtube.com/watch?v=1",
                platform=Platform.YOUTUBE,
                kind=MediaKind.VIDEO,
                status=DownloadStatus.COMPLETED,
                target_format="mp4",
                cache_key="key-1",
            )
            await uow.commit()
            assert await uow.downloads.find_cached("key-1") is None

            await uow.downloads.create(
                user_id=1,
                url="https://youtube.com/watch?v=1",
                platform=Platform.YOUTUBE,
                kind=MediaKind.VIDEO,
                status=DownloadStatus.COMPLETED,
                target_format="mp4",
                cache_key="key-1",
                telegram_file_id="AgAC-file-id",
            )
            await uow.commit()
            cached = await uow.downloads.find_cached("key-1")
            assert cached is not None
            assert cached.telegram_file_id == "AgAC-file-id"

    async def test_platform_and_hour_aggregation(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await self._user(uow)
            for platform in (Platform.YOUTUBE, Platform.YOUTUBE, Platform.TIKTOK):
                await uow.downloads.create(
                    user_id=1,
                    url="https://x.tld/1",
                    platform=platform,
                    kind=MediaKind.VIDEO,
                    status=DownloadStatus.COMPLETED,
                    target_format="mp4",
                )
            await uow.commit()

            start = datetime.now(UTC) - timedelta(hours=1)
            end = datetime.now(UTC) + timedelta(hours=1)
            assert await uow.downloads.by_platform(start, end) == {"youtube": 2, "tiktok": 1}
            assert sum(await uow.downloads.by_hour(start, end)) == 3


class TestQueueRepository:
    async def test_ordering_and_clearing(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=1, referral_code="R1")
            await uow.commit()
            for priority in (0, 30, 10):
                download = await uow.downloads.create(
                    user_id=1,
                    url="https://x.tld/1",
                    platform=Platform.YOUTUBE,
                    kind=MediaKind.VIDEO,
                    target_format="mp4",
                )
                await uow.queue.enqueue(
                    download_id=download.id,
                    user_id=1,
                    priority=priority,
                    estimated_seconds=60,
                )
            await uow.commit()

            pending = await uow.queue.pending()
            assert [entry.priority for entry in pending] == [30, 10, 0]
            assert await uow.queue.size() == 3

            removed = await uow.queue.clear()
            await uow.commit()
            assert removed == 3
            assert await uow.queue.size() == 0


class TestBillingRepositories:
    async def test_active_subscription_is_the_highest_tier(self, uow_factory) -> None:
        now = datetime.now(UTC)
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=1, referral_code="R1")
            await uow.subscriptions.create(
                user_id=1,
                tier=SubscriptionTier.PREMIUM,
                status=SubscriptionStatus.ACTIVE,
                starts_at=now,
                expires_at=now + timedelta(days=30),
            )
            await uow.subscriptions.create(
                user_id=1,
                tier=SubscriptionTier.VIP,
                status=SubscriptionStatus.ACTIVE,
                starts_at=now,
                expires_at=now + timedelta(days=7),
            )
            await uow.commit()

            active = await uow.subscriptions.active_for_user(1)
            assert active is not None
            assert active.tier is SubscriptionTier.VIP
            assert await uow.subscriptions.count_active() == 2

    async def test_expire_due_returns_affected_users(self, uow_factory) -> None:
        now = datetime.now(UTC)
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=1, referral_code="R1")
            await uow.subscriptions.create(
                user_id=1,
                tier=SubscriptionTier.PREMIUM,
                status=SubscriptionStatus.ACTIVE,
                starts_at=now - timedelta(days=40),
                expires_at=now - timedelta(days=1),
            )
            await uow.commit()

            affected = await uow.subscriptions.expire_due()
            await uow.commit()
            assert list(affected) == [1]
            assert await uow.subscriptions.active_for_user(1) is None

    async def test_promo_usage_statistics(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.users.create_user(user_id=1, referral_code="R1")
            promo = await uow.promos.create(
                code="WELCOME",
                promo_type=PromoType.COINS,
                status=PromoStatus.ACTIVE,
                value=100,
                per_user_limit=1,
            )
            await uow.promo_activations.create(
                promo_id=promo.id, user_id=1, code="WELCOME", coins_granted=100
            )
            await uow.promos.increment_usage(promo.id)
            await uow.commit()

            stats = await uow.promos.usage_stats(promo.id)
            assert stats == {"activations": 1, "coins_granted": 100, "days_granted": 0}
            assert await uow.promo_activations.count_for_user(promo.id, 1) == 1


class TestSystemRepositories:
    async def test_settings_upsert(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.settings.set_value("maintenance", True, category="ops")
            await uow.commit()
            assert await uow.settings.get_value("maintenance") is True

            await uow.settings.set_value("maintenance", False)
            await uow.commit()
            assert await uow.settings.get_value("maintenance") is False

    async def test_languages_are_seeded_once(self, uow_factory) -> None:
        async with uow_factory() as uow:
            await uow.languages.ensure_defaults()
            await uow.commit()
            await uow.languages.ensure_defaults()
            await uow.commit()
            assert len(await uow.languages.enabled()) == len(Language)

    async def test_audit_log_is_recorded(self, uow_factory) -> None:
        from mediabot.domain.enums import AuditAction

        async with uow_factory() as uow:
            await uow.audit.record(
                action=AuditAction.USER_BAN,
                actor_id=1,
                actor_name="root",
                target_type="user",
                target_id=99,
                summary="spam",
            )
            await uow.commit()
            entries = await uow.audit.list_recent()
            assert len(entries) == 1
            assert entries[0].action is AuditAction.USER_BAN
            assert entries[0].target_id == "99"
