"""System ORM models: admins, audit log, notifications, settings, statistics."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Float,
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
from sqlalchemy.orm import Mapped, mapped_column

from mediabot.domain.enums import (
    AdminRole,
    AuditAction,
    NotificationStatus,
    NotificationType,
)
from mediabot.infrastructure.db.base import (
    Base,
    IntPKMixin,
    TimestampMixin,
    UtcDateTime,
    enum_values,
)


class Admin(Base, IntPKMixin, TimestampMixin):
    """A staff account for the bot admin panel and the web panel.

    ``telegram_id`` links the account to a Telegram user (bot-side admin
    commands); ``username``/``password_hash`` authenticate the web panel.
    """

    __tablename__ = "admins"
    __table_args__ = (
        UniqueConstraint("username", name="uq_admins_username"),
        UniqueConstraint("telegram_id", name="uq_admins_telegram_id"),
    )

    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    role: Mapped[AdminRole] = mapped_column(
        SAEnum(
            AdminRole,
            name="admin_role_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=AdminRole.SUPPORT,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    totp_secret: Mapped[str | None] = mapped_column(String(255))


class AuditLog(Base, IntPKMixin, TimestampMixin):
    """Append-only record of every privileged action."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
    )

    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        SAEnum(
            AuditAction,
            name="audit_action_enum",
            native_enum=False,
            values_callable=enum_values,
            length=32,
        ),
        nullable=False,
    )
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class Notification(Base, IntPKMixin, TimestampMixin):
    """An outbound message queued for a user.

    Notifications are persisted before delivery so that a crash never loses a
    broadcast and retries are idempotent.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_status_scheduled", "status", "scheduled_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[NotificationType] = mapped_column(
        SAEnum(
            NotificationType,
            name="notification_type_enum",
            native_enum=False,
            values_callable=enum_values,
            length=32,
        ),
        nullable=False,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(
            NotificationStatus,
            name="notification_status_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=NotificationStatus.PENDING,
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(255))
    broadcast_id: Mapped[str | None] = mapped_column(String(64), index=True)


class Setting(Base, IntPKMixin, TimestampMixin):
    """Runtime-editable key/value settings (feature flags, texts, toggles).

    Values are JSON so a setting can hold a scalar, a list or an object without
    schema changes.  The cache layer keeps them in Redis with a short TTL.
    """

    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)


class DailyStatistic(Base, IntPKMixin, TimestampMixin):
    """Pre-aggregated daily metrics powering the analytics dashboard.

    Recomputing DAU/MAU on the fly over a large ``downloads`` table is
    expensive, so a nightly APScheduler job materialises one row per day.
    """

    __tablename__ = "statistics"
    __table_args__ = (UniqueConstraint("day", name="uq_statistics_day"),)

    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    new_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_active_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloads_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloads_video: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloads_audio: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloads_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    average_file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    average_duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_subscriptions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revenue: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    payments_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coins_issued: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    coins_spent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: ``{"youtube": 1234, "tiktok": 567, ...}``
    by_platform: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    #: 24 buckets, index = UTC hour.
    by_hour: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    server_load: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class ErrorLog(Base, IntPKMixin, TimestampMixin):
    """Structured error records surfaced in the admin panel.

    Complements the file/stdout logs: operators need a searchable, paginated
    view of recent failures without SSH access to the host.
    """

    __tablename__ = "error_logs"
    __table_args__ = (Index("ix_error_logs_code_created", "code", "created_at"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(String(32), default="bot", nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    download_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    url: Mapped[str | None] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
