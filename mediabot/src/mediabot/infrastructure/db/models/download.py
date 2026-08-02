"""Download-related ORM models: jobs, queue entries, history and favorites."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mediabot.domain.enums import (
    DownloadStatus,
    MediaKind,
    Platform,
    QueuePriority,
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


class Download(Base, IntPKMixin, TimestampMixin):
    """A single download job — the central entity of the system.

    The row is created the moment a user confirms a format and lives through
    the whole life-cycle (queued → downloading → processing → uploading →
    completed/failed).  Terminal rows are kept for history and analytics.
    """

    __tablename__ = "downloads"
    __table_args__ = (
        Index("ix_downloads_user_created", "user_id", "created_at"),
        Index("ix_downloads_status_created", "status", "created_at"),
        Index("ix_downloads_platform_created", "platform", "created_at"),
        Index("ix_downloads_cache_key", "cache_key"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), index=True)
    platform: Mapped[Platform] = mapped_column(
        SAEnum(
            Platform,
            name="platform_enum",
            native_enum=False,
            values_callable=enum_values,
            length=32,
        ),
        nullable=False,
    )
    kind: Mapped[MediaKind] = mapped_column(
        SAEnum(
            MediaKind,
            name="media_kind_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        nullable=False,
    )
    status: Mapped[DownloadStatus] = mapped_column(
        SAEnum(
            DownloadStatus,
            name="download_status_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        default=DownloadStatus.PENDING,
        nullable=False,
    )

    # --- requested parameters -------------------------------------------
    video_quality: Mapped[str | None] = mapped_column(String(16))
    audio_quality: Mapped[str | None] = mapped_column(String(16))
    target_format: Mapped[str] = mapped_column(String(8), default="mp4", nullable=False)
    cache_key: Mapped[str | None] = mapped_column(String(128))

    # --- metadata snapshot ----------------------------------------------
    title: Mapped[str | None] = mapped_column(String(512))
    uploader: Mapped[str | None] = mapped_column(String(255))
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    # --- results ---------------------------------------------------------
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(64))
    telegram_file_id: Mapped[str | None] = mapped_column(String(255), index=True)
    checksum: Mapped[str | None] = mapped_column(String(64))

    # --- execution details ------------------------------------------------
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    worker_name: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # --- delivery ---------------------------------------------------------
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="downloads")
    queue_entry: Mapped[DownloadQueueEntry | None] = relationship(
        back_populates="download", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_finished(self) -> bool:
        return DownloadStatus(self.status).is_terminal


class DownloadQueueEntry(Base, IntPKMixin, TimestampMixin):
    """A job waiting for a free worker.

    The queue lives in PostgreSQL (not only in Redis) so that positions
    survive a broker restart and the admin panel can inspect/clear it.
    """

    __tablename__ = "download_queue"
    __table_args__ = (
        UniqueConstraint("download_id", name="uq_download_queue_download_id"),
        Index("ix_download_queue_priority_enqueued", "priority", "enqueued_at"),
    )

    download_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("downloads.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=int(QueuePriority.LOW), nullable=False)
    estimated_seconds: Mapped[int] = mapped_column(Integer, default=45, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    locked_by: Mapped[str | None] = mapped_column(String(64))

    download: Mapped[Download] = relationship(back_populates="queue_entry")


class DownloadHistory(Base, IntPKMixin, TimestampMixin):
    """User-facing, denormalised history entry.

    Separate from :class:`Download` on purpose: history rows survive job
    clean-up, are cheap to search/filter (all display fields are inlined) and
    can be pruned per tier without touching operational data.
    """

    __tablename__ = "download_history"
    __table_args__ = (
        Index("ix_history_user_created", "user_id", "created_at"),
        Index("ix_history_user_kind", "user_id", "kind"),
        Index("ix_history_title", "title"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    download_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("downloads.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[Platform] = mapped_column(
        SAEnum(
            Platform,
            name="platform_enum",
            native_enum=False,
            values_callable=enum_values,
            length=32,
        ),
        nullable=False,
    )
    kind: Mapped[MediaKind] = mapped_column(
        SAEnum(
            MediaKind,
            name="media_kind_enum",
            native_enum=False,
            values_callable=enum_values,
            length=16,
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    uploader: Mapped[str | None] = mapped_column(String(255))
    quality: Mapped[str | None] = mapped_column(String(16))
    file_format: Mapped[str | None] = mapped_column(String(8))
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    repeat_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class FavoriteCollection(Base, IntPKMixin, TimestampMixin):
    """A named folder that groups favourites."""

    __tablename__ = "favorite_collections"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_favorite_collections_user_id_name"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    icon: Mapped[str] = mapped_column(String(8), default="📁", nullable=False)
    items_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    items: Mapped[list[Favorite]] = relationship(
        back_populates="collection", cascade="all, delete-orphan", lazy="noload"
    )


class Favorite(Base, IntPKMixin, TimestampMixin):
    """A saved link, optionally attached to a collection."""

    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_favorites_user_id_url"),
        Index("ix_favorites_user_collection", "user_id", "collection_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    collection_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("favorite_collections.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    platform: Mapped[Platform] = mapped_column(
        SAEnum(
            Platform,
            name="platform_enum",
            native_enum=False,
            values_callable=enum_values,
            length=32,
        ),
        default=Platform.GENERIC,
        nullable=False,
    )
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="favorites")
    collection: Mapped[FavoriteCollection | None] = relationship(back_populates="items")
