"""User-centric ORM models: users, limits, wallets, transactions, referrals."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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
    Language,
    SubscriptionTier,
    TransactionReason,
    TransactionType,
    UserStatus,
)
from mediabot.infrastructure.db.base import (
    Base,
    IntPKMixin,
    TimestampMixin,
    UtcDateTime,
    enum_values,
)

if TYPE_CHECKING:
    from mediabot.infrastructure.db.models.download import Download, Favorite
    from mediabot.infrastructure.db.models.payment import Payment
    from mediabot.infrastructure.db.models.subscription import Subscription


class User(Base, TimestampMixin):
    """A Telegram user.

    The Telegram user id doubles as the primary key: it is globally unique,
    stable and lets every other table join without an extra lookup.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_status_last_seen", "status", "last_seen_at"),
        Index("ix_users_referrer_id", "referrer_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[Language] = mapped_column(
        SAEnum(
            Language, name="language_enum", native_enum=False, values_callable=enum_values, length=8
        ),
        default=Language.EN,
        nullable=False,
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(
            UserStatus,
            name="user_status_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=UserStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    tier: Mapped[SubscriptionTier] = mapped_column(
        SAEnum(
            SubscriptionTier,
            name="subscription_tier_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=SubscriptionTier.FREE,
        nullable=False,
        index=True,
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_premium_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- moderation -----------------------------------------------------
    banned_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    muted_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ban_reason: Mapped[str | None] = mapped_column(String(255))
    violations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- referral -------------------------------------------------------
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    referrer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    referral_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- gamification ---------------------------------------------------
    daily_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_daily_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_daily_bonus_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- denormalised counters (kept in sync by the stats service) -------
    total_downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_video_downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_audio_downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_seconds_media: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # --- preferences ----------------------------------------------------
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    promo_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ads_disabled_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    default_video_quality: Mapped[str | None] = mapped_column(String(16))
    default_audio_quality: Mapped[str | None] = mapped_column(String(16))
    default_video_format: Mapped[str | None] = mapped_column(String(8))
    default_audio_format: Mapped[str | None] = mapped_column(String(8))
    auto_download: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- activity -------------------------------------------------------
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    captcha_passed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    timezone_offset_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- relationships --------------------------------------------------
    limits: Mapped[UserLimit | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    wallet: Mapped[Wallet | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )
    downloads: Mapped[list[Download]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )
    favorites: Mapped[list[Favorite]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )
    achievements: Mapped[list[UserAchievement]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )

    @property
    def full_name(self) -> str:
        parts = [self.first_name or "", self.last_name or ""]
        name = " ".join(part for part in parts if part).strip()
        return name or (self.username or f"user_{self.id}")

    @property
    def mention(self) -> str:
        return f"@{self.username}" if self.username else self.full_name


class UserLimit(Base, IntPKMixin, TimestampMixin):
    """Per-user overrides applied on top of the tier policy.

    Rows exist only for users that actually received an override (admin grant,
    promo boost or coin-shop purchase), keeping the table small.
    """

    __tablename__ = "user_limits"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_limits_user_id"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    extra_daily_downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    daily_downloads_absolute: Mapped[int | None] = mapped_column(Integer)
    max_file_size_absolute: Mapped[int | None] = mapped_column(BigInteger)
    max_duration_absolute: Mapped[int | None] = mapped_column(Integer)
    max_concurrent_absolute: Mapped[int | None] = mapped_column(Integer)
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ads_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    note: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="limits")


class Wallet(Base, IntPKMixin, TimestampMixin):
    """Internal coin balance of a user.

    ``balance`` is authoritative and always mutated together with a
    :class:`Transaction` row inside one database transaction, so the ledger and
    the balance can never diverge.
    """

    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("user_id", name="uq_wallets_user_id"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_earned: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_spent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="wallet")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan", lazy="noload"
    )


class Transaction(Base, IntPKMixin, TimestampMixin):
    """Immutable coin ledger entry."""

    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_wallet_created", "wallet_id", "created_at"),)

    wallet_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[TransactionType] = mapped_column(
        SAEnum(
            TransactionType,
            name="transaction_type_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        nullable=False,
    )
    reason: Mapped[TransactionReason] = mapped_column(
        SAEnum(
            TransactionReason,
            name="transaction_reason_enum",
            native_enum=False,
            values_callable=enum_values,
            length=32,
        ),
        nullable=False,
        index=True,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(255))
    reference: Mapped[str | None] = mapped_column(String(64), index=True)

    wallet: Mapped[Wallet] = relationship(back_populates="transactions")


class Referral(Base, IntPKMixin, TimestampMixin):
    """A confirmed invitation link between two users."""

    __tablename__ = "referrals"
    __table_args__ = (
        UniqueConstraint("invitee_id", name="uq_referrals_invitee_id"),
        Index("ix_referrals_inviter_created", "inviter_id", "created_at"),
    )

    inviter_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    invitee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    coins_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    premium_days_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))


class UserAchievement(Base, IntPKMixin, TimestampMixin):
    """Achievement unlocked by a user (one row per unlock)."""

    __tablename__ = "achievements"
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_achievements_user_id_code"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="achievements")


class SupportedLanguage(Base, IntPKMixin, TimestampMixin):
    """Registry of interface languages.

    Storing them in the database (in addition to the :class:`Language` enum)
    lets the admin panel enable/disable a locale without a redeployment.
    """

    __tablename__ = "languages"
    __table_args__ = (UniqueConstraint("code", name="uq_languages_code"),)

    code: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    native_name: Mapped[str] = mapped_column(String(64), nullable=False)
    flag: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    translation_progress: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
