"""Unit tests for the quota/limit engine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mediabot.core.exceptions import (
    DailyQuotaExceededError,
    FileTooLargeError,
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
from mediabot.domain.policies import MB, get_tier_policy
from mediabot.domain.services.limits import LimitOverrides, LimitService
from mediabot.domain.value_objects import UsageSnapshot

pytestmark = pytest.mark.unit


def usage(**kwargs: int) -> UsageSnapshot:
    defaults = {"downloads_today": 0, "bytes_today": 0, "active_jobs": 0, "queued_jobs": 0}
    defaults.update(kwargs)
    return UsageSnapshot(**defaults, window_started_at=datetime.now(UTC))  # type: ignore[arg-type]


class TestEffectivePolicy:
    def test_returns_static_policy_without_overrides(self, limit_service: LimitService) -> None:
        policy = limit_service.effective_policy(SubscriptionTier.FREE)
        assert policy is get_tier_policy(SubscriptionTier.FREE)

    def test_additive_overrides_stack_on_top_of_the_tier(self, limit_service: LimitService) -> None:
        policy = limit_service.effective_policy(
            SubscriptionTier.FREE,
            LimitOverrides(extra_daily_downloads=5, extra_file_size_bytes=100 * MB),
        )
        assert policy.daily_downloads == 15
        assert policy.max_file_size_bytes == 350 * MB

    def test_absolute_override_replaces_the_tier_value(self, limit_service: LimitService) -> None:
        policy = limit_service.effective_policy(
            SubscriptionTier.FREE,
            LimitOverrides(daily_downloads_absolute=3, extra_daily_downloads=99),
        )
        assert policy.daily_downloads == 3

    def test_unlimited_override_removes_every_ceiling(self, limit_service: LimitService) -> None:
        policy = limit_service.effective_policy(
            SubscriptionTier.FREE, LimitOverrides(unlimited=True)
        )
        assert policy.daily_downloads is None
        assert policy.max_duration_seconds is None
        assert policy.allowed_platforms is None
        assert policy.ads_enabled is False


class TestQuota:
    def test_allows_a_request_within_quota(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_quota(policy, usage(downloads_today=3))
        assert decision.allowed
        assert decision.remaining_downloads == 7

    def test_rejects_when_the_daily_quota_is_spent(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_quota(policy, usage(downloads_today=10))
        assert not decision.allowed
        assert decision.reason == "daily_quota"
        with pytest.raises(DailyQuotaExceededError):
            decision.raise_for_status()

    def test_rejects_when_concurrency_is_saturated(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_quota(policy, usage(active_jobs=1))
        assert not decision.allowed
        assert decision.reason == "concurrency"

    def test_lifetime_tier_has_no_daily_quota(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.LIFETIME)
        decision = limit_service.check_quota(policy, usage(downloads_today=10_000))
        assert decision.allowed
        assert decision.remaining_downloads is None


class TestPlatformAndQuality:
    def test_free_tier_cannot_use_a_restricted_platform(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_platform(policy, Platform.BILIBILI)
        assert not decision.allowed
        with pytest.raises(PlatformNotAllowedError):
            decision.raise_for_status()

    def test_paid_tiers_unlock_every_platform(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.PREMIUM)
        assert limit_service.check_platform(policy, Platform.BILIBILI).allowed

    @pytest.mark.parametrize(
        ("tier", "quality", "expected"),
        [
            (SubscriptionTier.FREE, VideoQuality.P720, True),
            (SubscriptionTier.FREE, VideoQuality.P1080, False),
            (SubscriptionTier.PREMIUM, VideoQuality.P1080, True),
            (SubscriptionTier.PREMIUM, VideoQuality.P2160, False),
            (SubscriptionTier.VIP, VideoQuality.P2160, True),
            (SubscriptionTier.LIFETIME, VideoQuality.BEST, True),
        ],
    )
    def test_video_quality_ceilings(
        self,
        limit_service: LimitService,
        tier: SubscriptionTier,
        quality: VideoQuality,
        expected: bool,
    ) -> None:
        policy = get_tier_policy(tier)
        decision = limit_service.check_quality(policy, kind=MediaKind.VIDEO, video_quality=quality)
        assert decision.allowed is expected

    def test_lossless_audio_requires_vip(self, limit_service: LimitService) -> None:
        free = get_tier_policy(SubscriptionTier.FREE)
        vip = get_tier_policy(SubscriptionTier.VIP)
        assert not limit_service.check_quality(
            free, kind=MediaKind.AUDIO, audio_quality=AudioQuality.LOSSLESS
        ).allowed
        assert limit_service.check_quality(
            vip, kind=MediaKind.AUDIO, audio_quality=AudioQuality.LOSSLESS
        ).allowed

    def test_audio_only_video_quality_is_always_allowed(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        assert policy.allows_video_quality(VideoQuality.AUDIO_ONLY)


class TestMediaConstraints:
    def test_rejects_a_file_above_the_tier_limit(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_media(policy, duration_seconds=60, estimated_size=400 * MB)
        assert decision.reason == "file_size"
        with pytest.raises(FileTooLargeError):
            decision.raise_for_status()

    def test_rejects_media_longer_than_the_tier_allows(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_media(policy, duration_seconds=3600, estimated_size=10 * MB)
        assert decision.reason == "duration"

    def test_unknown_size_and_duration_are_permitted(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        assert limit_service.check_media(policy, duration_seconds=None, estimated_size=None).allowed


class TestFullRequestCheck:
    def test_checks_run_cheapest_first(self, limit_service: LimitService) -> None:
        """A spent quota short-circuits before the platform check."""
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_request(
            policy,
            usage(downloads_today=10),
            platform=Platform.BILIBILI,
            kind=MediaKind.VIDEO,
            video_quality=VideoQuality.P2160,
        )
        assert decision.reason == "daily_quota"

    def test_happy_path(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.PREMIUM)
        decision = limit_service.check_request(
            policy,
            usage(downloads_today=1),
            platform=Platform.YOUTUBE,
            kind=MediaKind.VIDEO,
            duration_seconds=600,
            estimated_size=200 * MB,
            video_quality=VideoQuality.P1080,
        )
        assert decision.allowed
        assert decision.remaining_downloads == 99

    def test_quality_violation_raises_the_matching_error(self, limit_service: LimitService) -> None:
        policy = get_tier_policy(SubscriptionTier.FREE)
        decision = limit_service.check_request(
            policy,
            usage(),
            platform=Platform.YOUTUBE,
            kind=MediaKind.VIDEO,
            video_quality=VideoQuality.P2160,
        )
        with pytest.raises(QualityNotAllowedError):
            decision.raise_for_status()


class TestAllowedChoices:
    def test_free_tier_quality_menu_stops_at_720p(self, limit_service: LimitService) -> None:
        qualities = limit_service.allowed_video_qualities(get_tier_policy(SubscriptionTier.FREE))
        assert VideoQuality.P720 in qualities
        assert VideoQuality.P1080 not in qualities
        assert VideoQuality.BEST not in qualities

    def test_lifetime_tier_offers_everything(self, limit_service: LimitService) -> None:
        qualities = limit_service.allowed_video_qualities(
            get_tier_policy(SubscriptionTier.LIFETIME)
        )
        assert VideoQuality.BEST in qualities
        assert len(
            limit_service.allowed_audio_qualities(get_tier_policy(SubscriptionTier.LIFETIME))
        ) == len(AudioQuality)
