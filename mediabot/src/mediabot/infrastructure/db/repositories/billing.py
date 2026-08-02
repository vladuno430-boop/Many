"""Repositories for subscriptions, payments and promo codes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, update

from mediabot.domain.enums import (
    PaymentProvider,
    PaymentStatus,
    PromoStatus,
    PromoType,
    SubscriptionStatus,
    SubscriptionTier,
)
from mediabot.infrastructure.db.models.billing import (
    Payment,
    PromoActivation,
    PromoCode,
    Subscription,
)
from mediabot.infrastructure.db.repositories.base import BaseRepository


class SubscriptionRepository(BaseRepository[Subscription]):
    """Subscription periods."""

    model = Subscription

    async def active_for_user(self, user_id: int) -> Subscription | None:
        """Highest-ranked active subscription of a user (``None`` for free)."""
        now = datetime.now(UTC)
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                (Subscription.expires_at.is_(None)) | (Subscription.expires_at > now),
            )
            .order_by(Subscription.expires_at.desc().nulls_first())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        if not rows:
            return None
        return max(rows, key=lambda sub: SubscriptionTier(sub.tier).rank)

    async def history_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Subscription]:
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def expiring_between(
        self,
        start: datetime,
        end: datetime,
    ) -> Sequence[Subscription]:
        """Subscriptions that end inside the window — used for reminders."""
        stmt = select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at >= start,
            Subscription.expires_at < end,
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def expire_due(self, now: datetime | None = None) -> Sequence[int]:
        """Mark elapsed subscriptions as expired and return the affected users."""
        now = now or datetime.now(UTC)
        stmt = select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at <= now,
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        for subscription in rows:
            subscription.status = SubscriptionStatus.EXPIRED
        await self.session.flush()
        return [row.user_id for row in rows]

    async def count_active(self) -> int:
        now = datetime.now(UTC)
        stmt = (
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                (Subscription.expires_at.is_(None)) | (Subscription.expires_at > now),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_by_tier(self) -> dict[str, int]:
        now = datetime.now(UTC)
        stmt = (
            select(Subscription.tier, func.count())
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                (Subscription.expires_at.is_(None)) | (Subscription.expires_at > now),
            )
            .group_by(Subscription.tier)
        )
        rows = (await self.session.execute(stmt)).all()
        return {SubscriptionTier(row[0]).value: int(row[1]) for row in rows}


class PaymentRepository(BaseRepository[Payment]):
    """Payment attempts across every provider."""

    model = Payment

    async def by_payload(self, payload: str) -> Payment | None:
        return await self.get_by(invoice_payload=payload)

    async def by_external_id(self, provider: PaymentProvider, external_id: str) -> Payment | None:
        return await self.get_by(provider=provider, external_id=external_id)

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Payment]:
        stmt = (
            select(Payment)
            .where(Payment.user_id == user_id)
            .order_by(Payment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_recent(
        self,
        *,
        status: PaymentStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Payment]:
        stmt: Select[tuple[Payment]] = select(Payment)
        if status is not None:
            stmt = stmt.where(Payment.status == status)
        stmt = stmt.order_by(Payment.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def revenue_between(self, start: datetime, end: datetime) -> tuple[int, int]:
        """``(revenue, payment_count)`` for successful payments in the window."""
        stmt = select(func.coalesce(func.sum(Payment.amount), 0), func.count()).where(
            Payment.status == PaymentStatus.PAID,
            Payment.paid_at >= start,
            Payment.paid_at < end,
        )
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), int(row[1])

    async def mark_paid(self, payment_id: int, external_id: str | None = None) -> None:
        values: dict[str, object] = {
            "status": PaymentStatus.PAID,
            "paid_at": datetime.now(UTC),
        }
        if external_id:
            values["external_id"] = external_id
        await self.session.execute(update(Payment).where(Payment.id == payment_id).values(**values))


class PromoCodeRepository(BaseRepository[PromoCode]):
    """Promo codes."""

    model = PromoCode

    async def by_code(self, code: str) -> PromoCode | None:
        return await self.get_by(code=code.strip().upper())

    async def list_filtered(
        self,
        *,
        status: PromoStatus | None = None,
        promo_type: PromoType | None = None,
        campaign: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[PromoCode]:
        stmt: Select[tuple[PromoCode]] = select(PromoCode)
        if status is not None:
            stmt = stmt.where(PromoCode.status == status)
        if promo_type is not None:
            stmt = stmt.where(PromoCode.promo_type == promo_type)
        if campaign:
            stmt = stmt.where(PromoCode.campaign == campaign)
        stmt = stmt.order_by(PromoCode.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def increment_usage(self, promo_id: int) -> None:
        await self.session.execute(
            update(PromoCode)
            .where(PromoCode.id == promo_id)
            .values(activations_used=PromoCode.activations_used + 1)
        )

    async def expire_due(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        result = await self.session.execute(
            update(PromoCode)
            .where(
                PromoCode.status == PromoStatus.ACTIVE,
                PromoCode.expires_at.is_not(None),
                PromoCode.expires_at <= now,
            )
            .values(status=PromoStatus.EXPIRED)
        )
        return int(result.rowcount or 0)

    async def usage_stats(self, promo_id: int) -> dict[str, int]:
        stmt = select(
            func.count(),
            func.coalesce(func.sum(PromoActivation.coins_granted), 0),
            func.coalesce(func.sum(PromoActivation.days_granted), 0),
        ).where(PromoActivation.promo_id == promo_id)
        row = (await self.session.execute(stmt)).one()
        return {
            "activations": int(row[0]),
            "coins_granted": int(row[1]),
            "days_granted": int(row[2]),
        }


class PromoActivationRepository(BaseRepository[PromoActivation]):
    """Promo redemption audit trail."""

    model = PromoActivation

    async def count_for_user(self, promo_id: int, user_id: int) -> int:
        return await self.count(promo_id=promo_id, user_id=user_id)

    async def list_for_promo(
        self,
        promo_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[PromoActivation]:
        stmt = (
            select(PromoActivation)
            .where(PromoActivation.promo_id == promo_id)
            .order_by(PromoActivation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[PromoActivation]:
        stmt = (
            select(PromoActivation)
            .where(PromoActivation.user_id == user_id)
            .order_by(PromoActivation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()
