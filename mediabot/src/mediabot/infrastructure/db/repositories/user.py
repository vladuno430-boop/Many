"""Repositories for users, limits, wallets, referrals, achievements, languages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select, update

from mediabot.domain.enums import (
    Language,
    SubscriptionTier,
    TransactionReason,
    TransactionType,
    UserStatus,
)
from mediabot.infrastructure.db.models.user import (
    Referral,
    SupportedLanguage,
    Transaction,
    User,
    UserAchievement,
    UserLimit,
    Wallet,
)
from mediabot.infrastructure.db.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """Persistence for :class:`User` plus the queries the bot needs on hot paths."""

    model = User

    async def get_by_username(self, username: str) -> User | None:
        return await self.get_by(username=username.lstrip("@"))

    async def get_by_referral_code(self, code: str) -> User | None:
        return await self.get_by(referral_code=code.upper())

    async def create_user(
        self,
        *,
        user_id: int,
        referral_code: str,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language: Language = Language.EN,
        referrer_id: int | None = None,
        is_premium_telegram: bool = False,
    ) -> User:
        """Insert a user together with its wallet (every user owns a wallet)."""
        user = User(
            id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language=language,
            referral_code=referral_code,
            referrer_id=referrer_id,
            is_premium_telegram=is_premium_telegram,
            last_seen_at=datetime.now(UTC),
        )
        user.wallet = Wallet(user_id=user_id, balance=0)
        self.session.add(user)
        await self.session.flush()
        return user

    async def touch(self, user_id: int) -> None:
        """Update ``last_seen_at`` without loading the row."""
        await self.session.execute(
            update(User).where(User.id == user_id).values(last_seen_at=datetime.now(UTC))
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> Sequence[User]:
        """Search by id, username or name — used by the admin panel."""
        term = f"%{query.strip().lstrip('@').lower()}%"
        conditions: list[ColumnElement[bool]] = [
            func.lower(User.username).like(term),
            func.lower(User.first_name).like(term),
            func.lower(User.last_name).like(term),
        ]
        if query.strip().isdigit():
            conditions.append(User.id == int(query.strip()))
        stmt = (
            select(User)
            .where(or_(*conditions))
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_filtered(
        self,
        *,
        status: UserStatus | None = None,
        tier: SubscriptionTier | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[User]:
        stmt = select(User)
        if status is not None:
            stmt = stmt.where(User.status == status)
        if tier is not None:
            stmt = stmt.where(User.tier == tier)
        stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count_filtered(
        self,
        *,
        status: UserStatus | None = None,
        tier: SubscriptionTier | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(User)
        if status is not None:
            stmt = stmt.where(User.status == status)
        if tier is not None:
            stmt = stmt.where(User.tier == tier)
        return int((await self.session.execute(stmt)).scalar_one())

    async def active_since(self, since: datetime) -> int:
        """Number of users seen after ``since`` (DAU/MAU building block)."""
        stmt = select(func.count()).select_from(User).where(User.last_seen_at >= since)
        return int((await self.session.execute(stmt)).scalar_one())

    async def new_since(self, since: datetime) -> int:
        stmt = select(func.count()).select_from(User).where(User.created_at >= since)
        return int((await self.session.execute(stmt)).scalar_one())

    async def broadcast_targets(
        self,
        *,
        tier: SubscriptionTier | None = None,
        language: Language | None = None,
        only_active_days: int | None = None,
        promo_opt_in: bool = True,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[int]:
        """Ids eligible for a broadcast, honouring per-user opt-outs."""
        stmt = select(User.id).where(User.status == UserStatus.ACTIVE)
        if promo_opt_in:
            stmt = stmt.where(User.promo_notifications_enabled.is_(True))
        stmt = stmt.where(User.notifications_enabled.is_(True))
        if tier is not None:
            stmt = stmt.where(User.tier == tier)
        if language is not None:
            stmt = stmt.where(User.language == language)
        if only_active_days:
            since = datetime.now(UTC) - timedelta(days=only_active_days)
            stmt = stmt.where(User.last_seen_at >= since)
        stmt = stmt.order_by(User.id).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def increment_counters(
        self,
        user_id: int,
        *,
        downloads: int = 0,
        video_downloads: int = 0,
        audio_downloads: int = 0,
        size_bytes: int = 0,
        media_seconds: int = 0,
    ) -> None:
        """Atomically bump the denormalised counters used by the profile screen."""
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                total_downloads=User.total_downloads + downloads,
                total_video_downloads=User.total_video_downloads + video_downloads,
                total_audio_downloads=User.total_audio_downloads + audio_downloads,
                total_bytes=User.total_bytes + size_bytes,
                total_seconds_media=User.total_seconds_media + media_seconds,
            )
        )

    async def leaderboard(self, limit: int = 10) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.status == UserStatus.ACTIVE)
            .order_by(User.total_downloads.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def rank_of(self, user_id: int) -> int:
        """1-based position of ``user_id`` in the download leaderboard."""
        subquery = select(User.total_downloads).where(User.id == user_id).scalar_subquery()
        stmt = select(func.count()).select_from(User).where(User.total_downloads > subquery)
        return int((await self.session.execute(stmt)).scalar_one()) + 1


class UserLimitRepository(BaseRepository[UserLimit]):
    """Per-user limit overrides."""

    model = UserLimit

    async def for_user(self, user_id: int) -> UserLimit | None:
        return await self.get_by(user_id=user_id)

    async def upsert(self, user_id: int, **values: Any) -> UserLimit:
        existing = await self.for_user(user_id)
        if existing is None:
            return await self.create(user_id=user_id, **values)
        for key, value in values.items():
            setattr(existing, key, value)
        await self.session.flush()
        return existing

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Reset overrides whose grant period elapsed."""
        now = now or datetime.now(UTC)
        stmt = (
            update(UserLimit)
            .where(UserLimit.expires_at.is_not(None), UserLimit.expires_at <= now)
            .values(
                extra_daily_downloads=0,
                extra_file_size_bytes=0,
                daily_downloads_absolute=None,
                max_file_size_absolute=None,
                max_duration_absolute=None,
                max_concurrent_absolute=None,
                unlimited=False,
                ads_disabled=False,
                expires_at=None,
            )
        )
        return int((await self.session.execute(stmt)).rowcount or 0)


class WalletRepository(BaseRepository[Wallet]):
    """Coin wallet with an append-only ledger."""

    model = Wallet

    async def for_user(self, user_id: int) -> Wallet | None:
        return await self.get_by(user_id=user_id)

    async def get_or_create(self, user_id: int) -> Wallet:
        wallet = await self.for_user(user_id)
        if wallet is None:
            wallet = await self.create(user_id=user_id, balance=0)
        return wallet

    async def apply(
        self,
        *,
        user_id: int,
        amount: int,
        transaction_type: TransactionType,
        reason: TransactionReason,
        comment: str | None = None,
        reference: str | None = None,
    ) -> Transaction:
        """Move coins and write the matching ledger entry in one transaction.

        ``amount`` is always positive; ``transaction_type`` decides the sign.
        The caller is responsible for checking the balance beforehand — the
        wallet service does exactly that.
        """
        wallet = await self.get_or_create(user_id)
        delta = amount if transaction_type is TransactionType.CREDIT else -amount
        wallet.balance += delta
        if transaction_type is TransactionType.CREDIT:
            wallet.total_earned += amount
        else:
            wallet.total_spent += amount
        transaction = Transaction(
            wallet_id=wallet.id,
            user_id=user_id,
            type=transaction_type,
            reason=reason,
            amount=amount,
            balance_after=wallet.balance,
            comment=comment,
            reference=reference,
        )
        self.session.add(transaction)
        await self.session.flush()
        return transaction

    async def transactions(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Transaction]:
        stmt = (
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def coins_flow(self, since: datetime) -> tuple[int, int]:
        """``(issued, spent)`` coins since ``since`` — used by the analytics job."""
        stmt = (
            select(Transaction.type, func.coalesce(func.sum(Transaction.amount), 0))
            .where(Transaction.created_at >= since)
            .group_by(Transaction.type)
        )
        rows = (await self.session.execute(stmt)).all()
        issued = sum(int(total) for kind, total in rows if kind is TransactionType.CREDIT)
        spent = sum(int(total) for kind, total in rows if kind is TransactionType.DEBIT)
        return issued, spent


class ReferralRepository(BaseRepository[Referral]):
    """Invitation graph."""

    model = Referral

    async def get_for_invitee(self, invitee_id: int) -> Referral | None:
        return await self.get_by(invitee_id=invitee_id)

    async def count_for_inviter(self, inviter_id: int) -> int:
        return await self.count(inviter_id=inviter_id)

    async def list_for_inviter(
        self,
        inviter_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Referral]:
        stmt = (
            select(Referral)
            .where(Referral.inviter_id == inviter_id)
            .order_by(Referral.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def top_inviters(self, limit: int = 10) -> Sequence[tuple[int, int]]:
        stmt = (
            select(Referral.inviter_id, func.count().label("total"))
            .group_by(Referral.inviter_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(int(row[0]), int(row[1])) for row in (await self.session.execute(stmt)).all()]


class AchievementRepository(BaseRepository[UserAchievement]):
    """Unlocked achievements."""

    model = UserAchievement

    async def codes_for_user(self, user_id: int) -> set[str]:
        stmt = select(UserAchievement.code).where(UserAchievement.user_id == user_id)
        return {str(code) for code in (await self.session.execute(stmt)).scalars().all()}

    async def list_for_user(self, user_id: int) -> Sequence[UserAchievement]:
        stmt = (
            select(UserAchievement)
            .where(UserAchievement.user_id == user_id)
            .order_by(UserAchievement.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def unlock(self, user_id: int, code: str, reward_coins: int) -> UserAchievement:
        return await self.create(user_id=user_id, code=code, reward_coins=reward_coins)


class LanguageRepository(BaseRepository[SupportedLanguage]):
    """Registry of enabled interface locales."""

    model = SupportedLanguage

    async def enabled(self) -> Sequence[SupportedLanguage]:
        stmt = (
            select(SupportedLanguage)
            .where(SupportedLanguage.is_enabled.is_(True))
            .order_by(SupportedLanguage.code)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def ensure_defaults(self) -> None:
        """Seed the table from the :class:`Language` enum (idempotent)."""
        existing = {
            str(code)
            for code in (await self.session.execute(select(SupportedLanguage.code))).scalars().all()
        }
        for language in Language:
            if language.value in existing:
                continue
            self.session.add(
                SupportedLanguage(
                    code=language.value,
                    name=language.name.title(),
                    native_name=language.display_name,
                    flag=language.flag,
                    is_enabled=True,
                    is_default=language is Language.default(),
                )
            )
        await self.session.flush()
