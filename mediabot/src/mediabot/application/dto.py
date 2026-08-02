"""Data transfer objects exchanged between the application and presentation layers.

DTOs keep ORM objects out of handlers and API responses: the presentation layer
receives plain, already-computed data and cannot accidentally trigger a lazy
load or leak an internal column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mediabot.domain.enums import (
    AudioQuality,
    DownloadStatus,
    Language,
    MediaKind,
    Platform,
    SubscriptionTier,
    VideoQuality,
)
from mediabot.domain.value_objects import MediaInfo, TierPolicy, UsageSnapshot


@dataclass(frozen=True, slots=True)
class UserContext:
    """Everything a handler needs to know about the caller in one object."""

    user_id: int
    language: Language
    tier: SubscriptionTier
    is_admin: bool
    is_banned: bool
    is_muted: bool
    banned_until: datetime | None
    muted_until: datetime | None
    balance: int
    policy: TierPolicy
    usage: UsageSnapshot
    referral_code: str
    ads_enabled: bool

    @property
    def remaining_downloads(self) -> int | None:
        return self.usage.remaining(self.policy)


@dataclass(frozen=True, slots=True)
class MediaPreview:
    """Resolved media ready to be presented with format buttons."""

    info: MediaInfo
    platform: Platform
    allowed_video_qualities: tuple[VideoQuality, ...]
    allowed_audio_qualities: tuple[AudioQuality, ...]
    estimated_sizes: dict[str, int | None]
    metadata_only: bool
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadTicket:
    """Result of accepting a download request."""

    download_id: int
    status: DownloadStatus
    queued: bool
    position: int | None = None
    estimated_wait_seconds: int | None = None
    cached_file_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadView:
    """A download rendered for history/API consumption."""

    id: int
    url: str
    title: str
    platform: Platform
    kind: MediaKind
    status: DownloadStatus
    quality: str | None
    file_format: str | None
    file_size: int
    duration_seconds: int | None
    created_at: datetime
    telegram_file_id: str | None
    is_favorite: bool = False


@dataclass(frozen=True, slots=True)
class UserStatistics:
    """Aggregated profile statistics."""

    user_id: int
    total_downloads: int
    video_downloads: int
    audio_downloads: int
    total_bytes: int
    total_media_seconds: int
    registered_at: datetime
    days_with_us: int
    tier: SubscriptionTier
    subscription_expires_at: datetime | None
    balance: int
    coins_earned: int
    coins_spent: int
    referrals: int
    achievements: int
    rank: int
    daily_streak: int
    favorite_platform: str | None


@dataclass(frozen=True, slots=True)
class DashboardMetrics:
    """Numbers behind the analytics dashboard."""

    dau: int
    wau: int
    mau: int
    new_users_today: int
    new_users_week: int
    total_users: int
    active_subscriptions: int
    subscriptions_by_tier: dict[str, int]
    downloads_today: int
    downloads_week: int
    downloads_failed_today: int
    bytes_today: int
    average_file_size: int
    average_job_ms: int
    revenue_today: int
    revenue_month: int
    queue_size: int
    active_jobs: int
    by_platform: dict[str, int]
    by_hour: list[int]
    by_day: dict[str, int]
    top_errors: list[tuple[str, int]]
    cpu_percent: float
    memory_percent: float
    disk_used_percent: float


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    """Outcome of scheduling a broadcast."""

    broadcast_id: str
    recipients: int


@dataclass(frozen=True, slots=True)
class PromoRedemption:
    """Result of a successful promo redemption."""

    code: str
    summary: str
    coins: int = 0
    premium_days: int = 0
    vip_days: int = 0
    lifetime: bool = False
    extra_downloads: int = 0
    discount_percent: int = 0
    discount_fixed: int = 0


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Operational view of the queue for the admin panel."""

    waiting: int
    running: int
    capacity: int
    entries: list[dict[str, object]] = field(default_factory=list)
