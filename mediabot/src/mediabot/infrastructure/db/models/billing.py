"""Billing ORM models: subscriptions, payments, promo codes and activations."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mediabot.domain.enums import (
    PaymentProvider,
    PaymentStatus,
    PromoStatus,
    PromoType,
    SubscriptionStatus,
    SubscriptionTier,
)
from mediabot.infrastructure.db.base import (
    Base,
    IntPKMixin,
    TimestampMixin,
    UtcDateTime,
    enum_values,
)

if TYPE_CHECKING:
    from mediabot.infrastructure.db.models.user import User


class Subscription(Base, IntPKMixin, TimestampMixin):
    """A granted subscription period.

    History is preserved: every purchase/extension/grant creates a new row, and
    the *current* tier of a user is the highest-ranked active row (mirrored on
    ``users.tier`` for fast filtering).
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_user_status", "user_id", "status"),
        Index("ix_subscriptions_expires_at", "expires_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tier: Mapped[SubscriptionTier] = mapped_column(
        SAEnum(
            SubscriptionTier,
            name="subscription_tier_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        nullable=False,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(
            SubscriptionStatus,
            name="subscription_status_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="purchase", nullable=False)
    payment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("payments.id", ondelete="SET NULL")
    )
    granted_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="subscriptions")

    @property
    def is_lifetime(self) -> bool:
        return self.expires_at is None


class Payment(Base, IntPKMixin, TimestampMixin):
    """A payment attempt through any provider."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("external_id", "provider", name="uq_payments_external_id_provider"),
        Index("ix_payments_user_created", "user_id", "created_at"),
        Index("ix_payments_status_created", "status", "created_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        SAEnum(
            PaymentProvider,
            name="payment_provider_enum",
            native_enum=False,
            values_callable=enum_values,
            length=24,
        ),
        nullable=False,
    )
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(
            PaymentStatus,
            name="payment_status_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=PaymentStatus.PENDING,
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    tier: Mapped[SubscriptionTier | None] = mapped_column(
        SAEnum(
            SubscriptionTier,
            name="subscription_tier_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        )
    )
    days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    invoice_payload: Mapped[str | None] = mapped_column(String(128), index=True)
    promo_code: Mapped[str | None] = mapped_column(String(32))
    discount_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    refunded_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    #: Provider payloads may contain secrets — store them encrypted.
    provider_payload: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="payments")


class PromoCode(Base, IntPKMixin, TimestampMixin):
    """A redeemable promo code."""

    __tablename__ = "promo_codes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_promo_codes_code"),
        Index("ix_promo_codes_status_expires", "status", "expires_at"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    promo_type: Mapped[PromoType] = mapped_column(
        SAEnum(
            PromoType,
            name="promo_type_enum",
            native_enum=False,
            values_callable=enum_values,
            length=24,
        ),
        nullable=False,
    )
    status: Mapped[PromoStatus] = mapped_column(
        SAEnum(
            PromoStatus,
            name="promo_status_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=PromoStatus.ACTIVE,
        nullable=False,
    )
    value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    #: ``None`` means an unlimited number of activations.
    max_activations: Mapped[int | None] = mapped_column(Integer)
    activations_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    min_tier: Mapped[SubscriptionTier | None] = mapped_column(
        SAEnum(
            SubscriptionTier,
            name="subscription_tier_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        )
    )
    new_users_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    campaign: Mapped[str | None] = mapped_column(String(64), index=True)

    activations: Mapped[list[PromoActivation]] = relationship(
        back_populates="promo", cascade="all, delete-orphan", lazy="noload"
    )

    @property
    def is_unlimited(self) -> bool:
        return self.max_activations is None

    @property
    def remaining_activations(self) -> int | None:
        if self.max_activations is None:
            return None
        return max(self.max_activations - self.activations_used, 0)


class PromoActivation(Base, IntPKMixin, TimestampMixin):
    """Audit record of a promo redemption (also enforces per-user limits)."""

    __tablename__ = "promo_activations"
    __table_args__ = (Index("ix_promo_activations_promo_user", "promo_id", "user_id"),)

    promo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    reward_summary: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    coins_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    days_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("payments.id", ondelete="SET NULL")
    )

    promo: Mapped[PromoCode] = relationship(back_populates="activations")
