"""Pydantic schemas for the REST API (request validation + response shaping)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from mediabot.domain.enums import (
    AudioQuality,
    MediaKind,
    PromoType,
    SubscriptionTier,
    VideoQuality,
)


class TokenResponse(BaseModel):
    """JWT bundle returned by ``/api/v1/auth/login``."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class UserResponse(BaseModel):
    id: int
    username: str | None
    full_name: str
    language: str
    tier: SubscriptionTier
    status: str
    balance: int
    total_downloads: int
    total_bytes: int
    referrals: int
    created_at: datetime
    last_seen_at: datetime | None


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class LimitsResponse(BaseModel):
    tier: SubscriptionTier
    daily_downloads: int | None
    used_today: int
    remaining: int | None
    max_file_size_bytes: int | None
    max_duration_seconds: int | None
    max_concurrent_jobs: int
    max_video_quality: str
    max_audio_quality: str


class SubscriptionResponse(BaseModel):
    tier: SubscriptionTier
    active: bool
    expires_at: datetime | None
    auto_renew: bool
    features: list[str]


class UserStatisticsResponse(BaseModel):
    user_id: int
    total_downloads: int
    video_downloads: int
    audio_downloads: int
    total_bytes: int
    days_with_us: int
    tier: SubscriptionTier
    balance: int
    referrals: int
    achievements: int
    rank: int
    daily_streak: int
    favorite_platform: str | None


class DashboardResponse(BaseModel):
    dau: int
    wau: int
    mau: int
    new_users_today: int
    total_users: int
    active_subscriptions: int
    subscriptions_by_tier: dict[str, int]
    downloads_today: int
    downloads_week: int
    downloads_failed_today: int
    bytes_today: int
    average_file_size: int
    revenue_today: int
    revenue_month: int
    queue_size: int
    active_jobs: int
    by_platform: dict[str, int]
    by_hour: list[int]
    cpu_percent: float
    memory_percent: float
    disk_used_percent: float


class DownloadRequestSchema(BaseModel):
    """Body of ``POST /api/v1/downloads``."""

    user_id: int
    url: str = Field(min_length=8, max_length=2048)
    kind: MediaKind = MediaKind.VIDEO
    video_quality: VideoQuality | None = VideoQuality.P720
    audio_quality: AudioQuality | None = None
    container: str | None = Field(default=None, max_length=8)

    @field_validator("url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("Only http(s) URLs are accepted")
        return value


class DownloadResponse(BaseModel):
    download_id: int
    status: str
    queued: bool
    position: int | None = None
    estimated_wait_seconds: int | None = None
    cached: bool = False


class DownloadItem(BaseModel):
    id: int
    url: str
    title: str
    platform: str
    kind: MediaKind
    status: str
    quality: str | None
    file_size: int
    duration_seconds: int | None
    created_at: datetime


class HistoryResponse(BaseModel):
    items: list[DownloadItem]
    total: int
    page: int
    page_size: int


class MediaInfoResponse(BaseModel):
    url: str
    platform: str
    title: str
    uploader: str | None
    duration: int | None
    thumbnail: str | None
    is_live: bool
    qualities: list[str]
    estimated_sizes: dict[str, int | None]


class PromoCreateRequest(BaseModel):
    promo_type: PromoType
    value: int = Field(ge=0, le=1_000_000)
    code: str | None = Field(default=None, max_length=32)
    max_activations: int | None = Field(default=None, ge=1)
    per_user_limit: int = Field(default=1, ge=1, le=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    min_tier: SubscriptionTier | None = None
    new_users_only: bool = False
    campaign: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=255)


class PromoResponse(BaseModel):
    code: str
    promo_type: PromoType
    status: str
    value: int
    activations_used: int
    max_activations: int | None
    expires_at: datetime | None
    campaign: str | None


class PromoRedeemRequest(BaseModel):
    user_id: int
    code: str = Field(min_length=3, max_length=32)


class PromoRedeemResponse(BaseModel):
    code: str
    summary: str
    coins: int
    premium_days: int
    vip_days: int


class GrantSubscriptionRequest(BaseModel):
    user_id: int
    tier: SubscriptionTier
    days: int = Field(ge=1, le=36500)


class HealthResponse(BaseModel):
    status: str
    version: str
    database: bool
    redis: bool
    ffmpeg: bool
    queue_size: int
    active_jobs: int
    disk_free_bytes: int
    memory_percent: float
