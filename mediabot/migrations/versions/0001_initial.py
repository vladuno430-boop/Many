"""Initial schema.

Creates every table of the MediaBot domain: users and their limits/wallets,
downloads with the persistent queue and history, favourites, subscriptions,
payments, promo codes and activations, referrals, achievements, notifications,
staff accounts, settings, audit and error logs, plus the daily analytics table.

Enum columns are stored as VARCHAR with a CHECK constraint (``native_enum`` is
disabled) so adding a new enum member never requires an ``ALTER TYPE`` and a
table rewrite — an important property for zero-downtime deployments.

Revision ID: 0001_initial
Revises:
Created: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the whole schema."""
    op.create_table(
        "admins",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "support",
                "moderator",
                "admin",
                "owner",
                name="admin_role_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_ip", sa.String(length=64), nullable=True),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("totp_secret", sa.String(length=255), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id", name="uq_admins_telegram_id"),
        sa.UniqueConstraint("username", name="uq_admins_username"),
    )
    op.create_index("ix_admins_created_at", "admins", ["created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_name", sa.String(length=64), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "user_ban",
                "user_unban",
                "user_mute",
                "user_unmute",
                "grant_subscription",
                "revoke_subscription",
                "update_limits",
                "create_promo",
                "update_promo",
                "disable_promo",
                "broadcast",
                "queue_clear",
                "queue_cancel_job",
                "adjust_balance",
                "login",
                "login_failed",
                "settings_update",
                name="audit_action_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action_created", "audit_logs", ["action", "created_at"])
    op.create_index("ix_audit_logs_actor_created", "audit_logs", ["actor_id", "created_at"])
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_target_id", "audit_logs", ["target_id"])

    op.create_table(
        "error_logs",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("download_id", sa.BigInteger(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_error_logs_code_created", "error_logs", ["code", "created_at"])
    op.create_index("ix_error_logs_created_at", "error_logs", ["created_at"])
    op.create_index("ix_error_logs_download_id", "error_logs", ["download_id"])
    op.create_index("ix_error_logs_user_id", "error_logs", ["user_id"])

    op.create_table(
        "languages",
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("native_name", sa.String(length=64), nullable=False),
        sa.Column("flag", sa.String(length=8), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("translation_progress", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_languages_code"),
    )
    op.create_index("ix_languages_created_at", "languages", ["created_at"])

    op.create_table(
        "promo_codes",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column(
            "promo_type",
            sa.Enum(
                "percent_discount",
                "fixed_discount",
                "premium_days",
                "vip_days",
                "lifetime",
                "coins",
                "limit_boost",
                name="promo_type_enum",
                native_enum=False,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "disabled",
                "expired",
                "exhausted",
                name="promo_status_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("max_activations", sa.Integer(), nullable=True),
        sa.Column("activations_used", sa.Integer(), nullable=False),
        sa.Column("per_user_limit", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "min_tier",
            sa.Enum(
                "free",
                "premium",
                "vip",
                "lifetime",
                name="subscription_tier_enum",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("new_users_only", sa.Boolean(), nullable=False),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("campaign", sa.String(length=64), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_promo_codes_code"),
    )
    op.create_index("ix_promo_codes_campaign", "promo_codes", ["campaign"])
    op.create_index("ix_promo_codes_created_at", "promo_codes", ["created_at"])
    op.create_index("ix_promo_codes_status_expires", "promo_codes", ["status", "expires_at"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("updated_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_settings_key"),
    )
    op.create_index("ix_settings_created_at", "settings", ["created_at"])

    op.create_table(
        "statistics",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("new_users", sa.Integer(), nullable=False),
        sa.Column("active_users", sa.Integer(), nullable=False),
        sa.Column("monthly_active_users", sa.Integer(), nullable=False),
        sa.Column("downloads_total", sa.Integer(), nullable=False),
        sa.Column("downloads_video", sa.Integer(), nullable=False),
        sa.Column("downloads_audio", sa.Integer(), nullable=False),
        sa.Column("downloads_failed", sa.Integer(), nullable=False),
        sa.Column("bytes_total", sa.BigInteger(), nullable=False),
        sa.Column("average_file_size", sa.BigInteger(), nullable=False),
        sa.Column("average_duration_ms", sa.Integer(), nullable=False),
        sa.Column("active_subscriptions", sa.Integer(), nullable=False),
        sa.Column("revenue", sa.BigInteger(), nullable=False),
        sa.Column("payments_count", sa.Integer(), nullable=False),
        sa.Column("coins_issued", sa.BigInteger(), nullable=False),
        sa.Column("coins_spent", sa.BigInteger(), nullable=False),
        sa.Column("by_platform", sa.JSON(), nullable=False),
        sa.Column("by_hour", sa.JSON(), nullable=False),
        sa.Column("server_load", sa.Float(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", name="uq_statistics_day"),
    )
    op.create_index("ix_statistics_created_at", "statistics", ["created_at"])
    op.create_index("ix_statistics_day", "statistics", ["day"])

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column(
            "language",
            sa.Enum(
                "ru", "en", "de", "es", "fr", name="language_enum", native_enum=False, length=8
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "muted",
                "banned",
                "deleted",
                name="user_status_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "tier",
            sa.Enum(
                "free",
                "premium",
                "vip",
                "lifetime",
                name="subscription_tier_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_premium_telegram", sa.Boolean(), nullable=False),
        sa.Column("banned_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ban_reason", sa.String(length=255), nullable=True),
        sa.Column("violations", sa.Integer(), nullable=False),
        sa.Column("referral_code", sa.String(length=16), nullable=False),
        sa.Column(
            "referrer_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("referral_count", sa.Integer(), nullable=False),
        sa.Column("daily_streak", sa.Integer(), nullable=False),
        sa.Column("max_daily_streak", sa.Integer(), nullable=False),
        sa.Column("last_daily_bonus_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_downloads", sa.Integer(), nullable=False),
        sa.Column("total_video_downloads", sa.Integer(), nullable=False),
        sa.Column("total_audio_downloads", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_seconds_media", sa.BigInteger(), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False),
        sa.Column("promo_notifications_enabled", sa.Boolean(), nullable=False),
        sa.Column("ads_disabled_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("default_video_quality", sa.String(length=16), nullable=True),
        sa.Column("default_audio_quality", sa.String(length=16), nullable=True),
        sa.Column("default_video_format", sa.String(length=8), nullable=True),
        sa.Column("default_audio_format", sa.String(length=8), nullable=True),
        sa.Column("auto_download", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captcha_passed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone_offset_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referral_code", name="uq_users_referral_code"),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"])
    op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"])
    op.create_index("ix_users_referrer_id", "users", ["referrer_id"])
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index("ix_users_status_last_seen", "users", ["status", "last_seen_at"])
    op.create_index("ix_users_tier", "users", ["tier"])
    op.create_index("ix_users_username", "users", ["username"])

    op.create_table(
        "achievements",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("reward_coins", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("notified", sa.Boolean(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "code", name="uq_achievements_user_id_code"),
    )
    op.create_index("ix_achievements_code", "achievements", ["code"])
    op.create_index("ix_achievements_created_at", "achievements", ["created_at"])

    op.create_table(
        "downloads",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=True),
        sa.Column(
            "platform",
            sa.Enum(
                "youtube",
                "youtube_shorts",
                "youtube_music",
                "tiktok",
                "rutube",
                "vk_video",
                "vk_clips",
                "instagram",
                "instagram_reels",
                "facebook",
                "twitter",
                "vimeo",
                "dailymotion",
                "twitch",
                "soundcloud",
                "mixcloud",
                "bandcamp",
                "bilibili",
                "pinterest",
                "reddit",
                "telegram",
                "odnoklassniki",
                "yandex_music",
                "spotify",
                "deezer",
                "likee",
                "kuaishou",
                "niconico",
                "streamable",
                "imgur",
                "tumblr",
                "linkedin",
                "coub",
                "generic",
                name="platform_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "video",
                "audio",
                "image",
                "document",
                name="media_kind_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "queued",
                "downloading",
                "processing",
                "uploading",
                "completed",
                "failed",
                "cancelled",
                "expired",
                name="download_status_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("video_quality", sa.String(length=16), nullable=True),
        sa.Column("audio_quality", sa.String(length=16), nullable=True),
        sa.Column("target_format", sa.String(length=8), nullable=False),
        sa.Column("cache_key", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("uploader", sa.String(length=255), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.String(length=64), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=255), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("worker_name", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("status_message_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_downloads_cache_key", "downloads", ["cache_key"])
    op.create_index("ix_downloads_created_at", "downloads", ["created_at"])
    op.create_index("ix_downloads_error_code", "downloads", ["error_code"])
    op.create_index("ix_downloads_platform_created", "downloads", ["platform", "created_at"])
    op.create_index("ix_downloads_source_id", "downloads", ["source_id"])
    op.create_index("ix_downloads_status_created", "downloads", ["status", "created_at"])
    op.create_index("ix_downloads_task_id", "downloads", ["task_id"])
    op.create_index("ix_downloads_telegram_file_id", "downloads", ["telegram_file_id"])
    op.create_index("ix_downloads_user_created", "downloads", ["user_id", "created_at"])

    op.create_table(
        "favorite_collections",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("icon", sa.String(length=8), nullable=False),
        sa.Column("items_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_favorite_collections_user_id_name"),
    )
    op.create_index("ix_favorite_collections_created_at", "favorite_collections", ["created_at"])

    op.create_table(
        "notifications",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.Enum(
                "download_ready",
                "download_failed",
                "queue_position",
                "subscription_expiring",
                "subscription_expired",
                "promotion",
                "promo_code",
                "system_update",
                "achievement_unlocked",
                "daily_bonus_ready",
                "broadcast",
                name="notification_type_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                "skipped",
                name="notification_status_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=128), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("broadcast_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_broadcast_id", "notifications", ["broadcast_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index(
        "ix_notifications_status_scheduled", "notifications", ["status", "scheduled_at"]
    )
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])

    op.create_table(
        "payments",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Enum(
                "telegram_stars",
                "card",
                "crypto",
                "balance",
                name="payment_provider_enum",
                native_enum=False,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "paid",
                "failed",
                "refunded",
                "cancelled",
                name="payment_status_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column(
            "tier",
            sa.Enum(
                "free",
                "premium",
                "vip",
                "lifetime",
                name="subscription_tier_enum",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("invoice_payload", sa.String(length=128), nullable=True),
        sa.Column("promo_code", sa.String(length=32), nullable=True),
        sa.Column("discount_amount", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("provider_payload", sa.Text(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", "provider", name="uq_payments_external_id_provider"),
    )
    op.create_index("ix_payments_created_at", "payments", ["created_at"])
    op.create_index("ix_payments_invoice_payload", "payments", ["invoice_payload"])
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])
    op.create_index("ix_payments_user_created", "payments", ["user_id", "created_at"])

    op.create_table(
        "referrals",
        sa.Column(
            "inviter_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invitee_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("coins_awarded", sa.Integer(), nullable=False),
        sa.Column("premium_days_awarded", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invitee_id", name="uq_referrals_invitee_id"),
    )
    op.create_index("ix_referrals_created_at", "referrals", ["created_at"])
    op.create_index("ix_referrals_inviter_created", "referrals", ["inviter_id", "created_at"])

    op.create_table(
        "user_limits",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extra_daily_downloads", sa.Integer(), nullable=False),
        sa.Column("extra_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("daily_downloads_absolute", sa.Integer(), nullable=True),
        sa.Column("max_file_size_absolute", sa.BigInteger(), nullable=True),
        sa.Column("max_duration_absolute", sa.Integer(), nullable=True),
        sa.Column("max_concurrent_absolute", sa.Integer(), nullable=True),
        sa.Column("unlimited", sa.Boolean(), nullable=False),
        sa.Column("ads_disabled", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_limits_user_id"),
    )
    op.create_index("ix_user_limits_created_at", "user_limits", ["created_at"])
    op.create_index("ix_user_limits_expires_at", "user_limits", ["expires_at"])

    op.create_table(
        "wallets",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("total_earned", sa.BigInteger(), nullable=False),
        sa.Column("total_spent", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
    )
    op.create_index("ix_wallets_created_at", "wallets", ["created_at"])

    op.create_table(
        "download_history",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "download_id",
            sa.BigInteger(),
            sa.ForeignKey("downloads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "youtube",
                "youtube_shorts",
                "youtube_music",
                "tiktok",
                "rutube",
                "vk_video",
                "vk_clips",
                "instagram",
                "instagram_reels",
                "facebook",
                "twitter",
                "vimeo",
                "dailymotion",
                "twitch",
                "soundcloud",
                "mixcloud",
                "bandcamp",
                "bilibili",
                "pinterest",
                "reddit",
                "telegram",
                "odnoklassniki",
                "yandex_music",
                "spotify",
                "deezer",
                "likee",
                "kuaishou",
                "niconico",
                "streamable",
                "imgur",
                "tumblr",
                "linkedin",
                "coub",
                "generic",
                name="platform_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "video",
                "audio",
                "image",
                "document",
                name="media_kind_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("uploader", sa.String(length=255), nullable=True),
        sa.Column("quality", sa.String(length=16), nullable=True),
        sa.Column("file_format", sa.String(length=8), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=255), nullable=True),
        sa.Column("is_favorite", sa.Boolean(), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_download_history_created_at", "download_history", ["created_at"])
    op.create_index("ix_history_title", "download_history", ["title"])
    op.create_index("ix_history_user_created", "download_history", ["user_id", "created_at"])
    op.create_index("ix_history_user_kind", "download_history", ["user_id", "kind"])

    op.create_table(
        "download_queue",
        sa.Column(
            "download_id",
            sa.BigInteger(),
            sa.ForeignKey("downloads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("estimated_seconds", sa.Integer(), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("download_id", name="uq_download_queue_download_id"),
    )
    op.create_index("ix_download_queue_created_at", "download_queue", ["created_at"])
    op.create_index("ix_download_queue_enqueued_at", "download_queue", ["enqueued_at"])
    op.create_index(
        "ix_download_queue_priority_enqueued", "download_queue", ["priority", "enqueued_at"]
    )
    op.create_index("ix_download_queue_user_id", "download_queue", ["user_id"])

    op.create_table(
        "favorites",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "collection_id",
            sa.BigInteger(),
            sa.ForeignKey("favorite_collections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "youtube",
                "youtube_shorts",
                "youtube_music",
                "tiktok",
                "rutube",
                "vk_video",
                "vk_clips",
                "instagram",
                "instagram_reels",
                "facebook",
                "twitter",
                "vimeo",
                "dailymotion",
                "twitch",
                "soundcloud",
                "mixcloud",
                "bandcamp",
                "bilibili",
                "pinterest",
                "reddit",
                "telegram",
                "odnoklassniki",
                "yandex_music",
                "spotify",
                "deezer",
                "likee",
                "kuaishou",
                "niconico",
                "streamable",
                "imgur",
                "tumblr",
                "linkedin",
                "coub",
                "generic",
                name="platform_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "url", name="uq_favorites_user_id_url"),
    )
    op.create_index("ix_favorites_created_at", "favorites", ["created_at"])
    op.create_index("ix_favorites_user_collection", "favorites", ["user_id", "collection_id"])

    op.create_table(
        "promo_activations",
        sa.Column(
            "promo_id",
            sa.BigInteger(),
            sa.ForeignKey("promo_codes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("reward_summary", sa.String(length=255), nullable=False),
        sa.Column("coins_granted", sa.Integer(), nullable=False),
        sa.Column("days_granted", sa.Integer(), nullable=False),
        sa.Column(
            "payment_id",
            sa.BigInteger(),
            sa.ForeignKey("payments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_activations_created_at", "promo_activations", ["created_at"])
    op.create_index("ix_promo_activations_promo_user", "promo_activations", ["promo_id", "user_id"])
    op.create_index("ix_promo_activations_user_id", "promo_activations", ["user_id"])

    op.create_table(
        "subscriptions",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tier",
            sa.Enum(
                "free",
                "premium",
                "vip",
                "lifetime",
                name="subscription_tier_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "expired",
                "cancelled",
                "pending",
                name="subscription_status_enum",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "payment_id",
            sa.BigInteger(),
            sa.ForeignKey("payments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("granted_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_created_at", "subscriptions", ["created_at"])
    op.create_index("ix_subscriptions_expires_at", "subscriptions", ["expires_at"])
    op.create_index("ix_subscriptions_user_status", "subscriptions", ["user_id", "status"])

    op.create_table(
        "transactions",
        sa.Column(
            "wallet_id",
            sa.BigInteger(),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.Enum("credit", "debit", name="transaction_type_enum", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum(
                "referral_bonus",
                "referral_join",
                "daily_bonus",
                "promo_code",
                "achievement",
                "task_reward",
                "purchase",
                "refund",
                "spend_premium",
                "spend_downloads",
                "spend_limit_boost",
                "spend_no_ads",
                "admin_adjustment",
                name="transaction_reason_enum",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("comment", sa.String(length=255), nullable=True),
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_index("ix_transactions_reason", "transactions", ["reason"])
    op.create_index("ix_transactions_reference", "transactions", ["reference"])
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"])
    op.create_index("ix_transactions_wallet_created", "transactions", ["wallet_id", "created_at"])


def downgrade() -> None:
    """Drop the whole schema (destructive — used only for a clean rollback)."""
    op.drop_index("ix_transactions_created_at", table_name="transactions")
    op.drop_index("ix_transactions_reason", table_name="transactions")
    op.drop_index("ix_transactions_reference", table_name="transactions")
    op.drop_index("ix_transactions_user_id", table_name="transactions")
    op.drop_index("ix_transactions_wallet_created", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("ix_subscriptions_created_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_expires_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_status", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_promo_activations_created_at", table_name="promo_activations")
    op.drop_index("ix_promo_activations_promo_user", table_name="promo_activations")
    op.drop_index("ix_promo_activations_user_id", table_name="promo_activations")
    op.drop_table("promo_activations")
    op.drop_index("ix_favorites_created_at", table_name="favorites")
    op.drop_index("ix_favorites_user_collection", table_name="favorites")
    op.drop_table("favorites")
    op.drop_index("ix_download_queue_created_at", table_name="download_queue")
    op.drop_index("ix_download_queue_enqueued_at", table_name="download_queue")
    op.drop_index("ix_download_queue_priority_enqueued", table_name="download_queue")
    op.drop_index("ix_download_queue_user_id", table_name="download_queue")
    op.drop_table("download_queue")
    op.drop_index("ix_download_history_created_at", table_name="download_history")
    op.drop_index("ix_history_title", table_name="download_history")
    op.drop_index("ix_history_user_created", table_name="download_history")
    op.drop_index("ix_history_user_kind", table_name="download_history")
    op.drop_table("download_history")
    op.drop_index("ix_wallets_created_at", table_name="wallets")
    op.drop_table("wallets")
    op.drop_index("ix_user_limits_created_at", table_name="user_limits")
    op.drop_index("ix_user_limits_expires_at", table_name="user_limits")
    op.drop_table("user_limits")
    op.drop_index("ix_referrals_created_at", table_name="referrals")
    op.drop_index("ix_referrals_inviter_created", table_name="referrals")
    op.drop_table("referrals")
    op.drop_index("ix_payments_created_at", table_name="payments")
    op.drop_index("ix_payments_invoice_payload", table_name="payments")
    op.drop_index("ix_payments_status_created", table_name="payments")
    op.drop_index("ix_payments_user_created", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_notifications_broadcast_id", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_status_scheduled", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_favorite_collections_created_at", table_name="favorite_collections")
    op.drop_table("favorite_collections")
    op.drop_index("ix_downloads_cache_key", table_name="downloads")
    op.drop_index("ix_downloads_created_at", table_name="downloads")
    op.drop_index("ix_downloads_error_code", table_name="downloads")
    op.drop_index("ix_downloads_platform_created", table_name="downloads")
    op.drop_index("ix_downloads_source_id", table_name="downloads")
    op.drop_index("ix_downloads_status_created", table_name="downloads")
    op.drop_index("ix_downloads_task_id", table_name="downloads")
    op.drop_index("ix_downloads_telegram_file_id", table_name="downloads")
    op.drop_index("ix_downloads_user_created", table_name="downloads")
    op.drop_table("downloads")
    op.drop_index("ix_achievements_code", table_name="achievements")
    op.drop_index("ix_achievements_created_at", table_name="achievements")
    op.drop_table("achievements")
    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_index("ix_users_last_seen_at", table_name="users")
    op.drop_index("ix_users_referrer_id", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_index("ix_users_status_last_seen", table_name="users")
    op.drop_index("ix_users_tier", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_statistics_created_at", table_name="statistics")
    op.drop_index("ix_statistics_day", table_name="statistics")
    op.drop_table("statistics")
    op.drop_index("ix_settings_created_at", table_name="settings")
    op.drop_table("settings")
    op.drop_index("ix_promo_codes_campaign", table_name="promo_codes")
    op.drop_index("ix_promo_codes_created_at", table_name="promo_codes")
    op.drop_index("ix_promo_codes_status_expires", table_name="promo_codes")
    op.drop_table("promo_codes")
    op.drop_index("ix_languages_created_at", table_name="languages")
    op.drop_table("languages")
    op.drop_index("ix_error_logs_code_created", table_name="error_logs")
    op.drop_index("ix_error_logs_created_at", table_name="error_logs")
    op.drop_index("ix_error_logs_download_id", table_name="error_logs")
    op.drop_index("ix_error_logs_user_id", table_name="error_logs")
    op.drop_table("error_logs")
    op.drop_index("ix_audit_logs_action_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_target_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_admins_created_at", table_name="admins")
    op.drop_table("admins")
