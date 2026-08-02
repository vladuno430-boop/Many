"""Quota and limit arithmetic — pure domain logic, no I/O.

The :class:`LimitService` merges three sources of truth into one decision:

1. the static :class:`~mediabot.domain.value_objects.TierPolicy` of the user's
   subscription tier;
2. per-user overrides (granted by admins, promo codes or the coin shop);
3. the current :class:`~mediabot.domain.value_objects.UsageSnapshot`.

Keeping it pure means every branch is unit-testable without a database, and the
same code answers "may I?" in the bot, the REST API and the worker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mediabot.core.exceptions import (
    DailyQuotaExceededError,
    DurationTooLongError,
    FileTooLargeError,
    LimitExceededError,
    PlatformNotAllowedError,
    QualityNotAllowedError,
)
from mediabot.domain.enums import (
    AudioQuality,
    MediaKind,
    Platform,
    SubscriptionTier,
    VideoQuality,
)
from mediabot.domain.policies import get_tier_policy
from mediabot.domain.value_objects import TierPolicy, UsageSnapshot


@dataclass(frozen=True, slots=True)
class LimitOverrides:
    """Per-user deltas applied on top of the tier policy.

    ``None`` fields mean "no override".  Additive fields (``extra_*``) stack
    with the tier value; absolute fields replace it.
    """

    extra_daily_downloads: int = 0
    extra_file_size_bytes: int = 0
    daily_downloads_absolute: int | None = None
    max_file_size_absolute: int | None = None
    max_duration_absolute: int | None = None
    max_concurrent_absolute: int | None = None
    unlimited: bool = False
    ads_disabled: bool = False

    @property
    def is_empty(self) -> bool:
        return self == LimitOverrides()


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """Result of a limit check for a concrete request."""

    allowed: bool
    reason: str | None = None
    remaining_downloads: int | None = None

    def raise_for_status(self) -> None:
        """Raise the matching domain exception when the request is denied."""
        if self.allowed:
            return
        mapping: dict[str, type[LimitExceededError]] = {
            "daily_quota": DailyQuotaExceededError,
            "file_size": FileTooLargeError,
            "duration": DurationTooLongError,
            "quality": QualityNotAllowedError,
            "platform": PlatformNotAllowedError,
            "concurrency": LimitExceededError,
        }
        error_cls = mapping.get(self.reason or "", LimitExceededError)
        raise error_cls()


class LimitService:
    """Compute effective limits and validate download requests against them."""

    def effective_policy(
        self,
        tier: SubscriptionTier,
        overrides: LimitOverrides | None = None,
    ) -> TierPolicy:
        """Merge ``overrides`` into the static policy of ``tier``."""
        policy = get_tier_policy(tier)
        if overrides is None or overrides.is_empty:
            return policy

        if overrides.unlimited:
            return replace(
                policy,
                daily_downloads=None,
                max_duration_seconds=None,
                speed_limit_bytes_per_sec=None,
                allowed_platforms=None,
                ads_enabled=False,
                max_file_size_bytes=overrides.max_file_size_absolute or policy.max_file_size_bytes,
            )

        daily = policy.daily_downloads
        if overrides.daily_downloads_absolute is not None:
            daily = overrides.daily_downloads_absolute
        elif daily is not None and overrides.extra_daily_downloads:
            daily += overrides.extra_daily_downloads

        max_size = policy.max_file_size_bytes
        if overrides.max_file_size_absolute is not None:
            max_size = overrides.max_file_size_absolute
        elif max_size is not None and overrides.extra_file_size_bytes:
            max_size += overrides.extra_file_size_bytes

        return replace(
            policy,
            daily_downloads=daily,
            max_file_size_bytes=max_size,
            max_duration_seconds=(
                overrides.max_duration_absolute
                if overrides.max_duration_absolute is not None
                else policy.max_duration_seconds
            ),
            max_concurrent_jobs=(
                overrides.max_concurrent_absolute
                if overrides.max_concurrent_absolute is not None
                else policy.max_concurrent_jobs
            ),
            ads_enabled=policy.ads_enabled and not overrides.ads_disabled,
        )

    def check_quota(self, policy: TierPolicy, usage: UsageSnapshot) -> LimitDecision:
        """Verify the daily download quota and the concurrency budget."""
        if policy.daily_downloads is not None and usage.downloads_today >= policy.daily_downloads:
            return LimitDecision(False, "daily_quota", remaining_downloads=0)
        if usage.active_jobs >= policy.max_concurrent_jobs:
            return LimitDecision(False, "concurrency", remaining_downloads=usage.remaining(policy))
        return LimitDecision(True, remaining_downloads=usage.remaining(policy))

    def check_platform(self, policy: TierPolicy, platform: Platform) -> LimitDecision:
        """Verify that the tier may download from ``platform``."""
        if not policy.allows_platform(platform):
            return LimitDecision(False, "platform")
        return LimitDecision(True)

    def check_media(
        self,
        policy: TierPolicy,
        *,
        duration_seconds: int | None,
        estimated_size: int | None,
    ) -> LimitDecision:
        """Verify duration and (estimated) size against the tier policy."""
        if (
            policy.max_duration_seconds is not None
            and duration_seconds is not None
            and duration_seconds > policy.max_duration_seconds
        ):
            return LimitDecision(False, "duration")
        if (
            policy.max_file_size_bytes is not None
            and estimated_size is not None
            and estimated_size > policy.max_file_size_bytes
        ):
            return LimitDecision(False, "file_size")
        return LimitDecision(True)

    def check_quality(
        self,
        policy: TierPolicy,
        *,
        kind: MediaKind,
        video_quality: VideoQuality | None = None,
        audio_quality: AudioQuality | None = None,
    ) -> LimitDecision:
        """Verify that the requested quality is unlocked for the tier."""
        if (
            kind is MediaKind.VIDEO
            and video_quality is not None
            and not policy.allows_video_quality(video_quality)
        ):
            return LimitDecision(False, "quality")
        if (
            kind is MediaKind.AUDIO
            and audio_quality is not None
            and not policy.allows_audio_quality(audio_quality)
        ):
            return LimitDecision(False, "quality")
        return LimitDecision(True)

    def check_request(
        self,
        policy: TierPolicy,
        usage: UsageSnapshot,
        *,
        platform: Platform,
        kind: MediaKind,
        duration_seconds: int | None = None,
        estimated_size: int | None = None,
        video_quality: VideoQuality | None = None,
        audio_quality: AudioQuality | None = None,
    ) -> LimitDecision:
        """Run every check in the cheapest-first order and return the verdict."""
        for decision in (
            self.check_quota(policy, usage),
            self.check_platform(policy, platform),
            self.check_quality(
                policy,
                kind=kind,
                video_quality=video_quality,
                audio_quality=audio_quality,
            ),
            self.check_media(
                policy,
                duration_seconds=duration_seconds,
                estimated_size=estimated_size,
            ),
        ):
            if not decision.allowed:
                return decision
        return LimitDecision(True, remaining_downloads=usage.remaining(policy))

    def allowed_video_qualities(self, policy: TierPolicy) -> tuple[VideoQuality, ...]:
        """Video qualities the tier may pick, in ascending order."""
        return tuple(
            quality
            for quality in (
                VideoQuality.P144,
                VideoQuality.P240,
                VideoQuality.P360,
                VideoQuality.P480,
                VideoQuality.P720,
                VideoQuality.P1080,
                VideoQuality.P1440,
                VideoQuality.P2160,
                VideoQuality.BEST,
            )
            if policy.allows_video_quality(quality)
        )

    def allowed_audio_qualities(self, policy: TierPolicy) -> tuple[AudioQuality, ...]:
        """Audio bitrates the tier may pick, in ascending order."""
        return tuple(quality for quality in AudioQuality if policy.allows_audio_quality(quality))
