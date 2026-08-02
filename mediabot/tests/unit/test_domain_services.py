"""Unit tests for format selection, the queue, the economy and promo codes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediabot.core.exceptions import (
    PromoCodeAlreadyUsedError,
    PromoCodeExhaustedError,
    PromoCodeExpiredError,
    PromoCodeNotFoundError,
)
from mediabot.domain.enums import (
    AudioFormat,
    AudioQuality,
    MediaKind,
    Platform,
    PromoStatus,
    PromoType,
    QueuePriority,
    SubscriptionTier,
    VideoFormat,
    VideoQuality,
)
from mediabot.domain.services.economy import AchievementService, EconomyService
from mediabot.domain.services.format_selection import FormatSelector
from mediabot.domain.services.promo import PromoService, PromoSnapshot
from mediabot.domain.services.queueing import QueueEntry, QueueService
from mediabot.domain.value_objects import (
    DownloadRequest,
    MediaFormat,
    MediaInfo,
    humanize_bytes,
    humanize_duration,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Format selection
# --------------------------------------------------------------------------- #
class TestFormatSelector:
    def _request(self, **kwargs: object) -> DownloadRequest:
        base = {
            "user_id": 1,
            "url": "https://youtube.com/watch?v=1",
            "platform": Platform.YOUTUBE,
            "kind": MediaKind.VIDEO,
            "video_quality": VideoQuality.P1080,
        }
        base.update(kwargs)
        return DownloadRequest(**base)  # type: ignore[arg-type]

    def test_video_selector_caps_the_height(self) -> None:
        plan = FormatSelector().build(self._request(video_quality=VideoQuality.P720))
        assert "height<=720" in plan.selector
        assert plan.merge_output_format == "mp4"
        assert not plan.is_audio

    def test_best_quality_has_no_height_filter(self) -> None:
        plan = FormatSelector().build(self._request(video_quality=VideoQuality.BEST))
        assert "height<=" not in plan.selector

    def test_mkv_accepts_any_codec_pair(self) -> None:
        plan = FormatSelector().build(
            self._request(video_format=VideoFormat.MKV, video_quality=VideoQuality.P1080)
        )
        assert plan.selector.startswith("bestvideo[height<=1080]+bestaudio")
        assert plan.remux_video_to == "mkv"
        assert plan.needs_conversion

    def test_audio_only_video_quality_produces_an_audio_plan(self) -> None:
        plan = FormatSelector().build(
            self._request(
                video_quality=VideoQuality.AUDIO_ONLY,
                audio_quality=AudioQuality.KBPS_192,
            )
        )
        assert plan.is_audio

    def test_audio_plan_sets_codec_and_bitrate(self) -> None:
        request = self._request(
            kind=MediaKind.AUDIO,
            video_quality=None,
            audio_quality=AudioQuality.KBPS_320,
            audio_format=AudioFormat.MP3,
        )
        plan = FormatSelector().build(request)
        assert plan.audio_codec == "mp3"
        assert plan.audio_bitrate_kbps == 320
        assert plan.needs_conversion

    def test_lossless_forces_a_lossless_container(self) -> None:
        request = self._request(
            kind=MediaKind.AUDIO,
            video_quality=None,
            audio_quality=AudioQuality.LOSSLESS,
            audio_format=AudioFormat.MP3,
        )
        plan = FormatSelector().build(request)
        assert plan.audio_codec == "flac"
        assert plan.audio_bitrate_kbps is None

    def test_request_validation_requires_a_quality(self) -> None:
        with pytest.raises(ValueError, match="video_quality"):
            DownloadRequest(
                user_id=1,
                url="https://x.tld/1",
                platform=Platform.YOUTUBE,
                kind=MediaKind.VIDEO,
            )


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #
class TestQueueService:
    def _entry(self, job_id: int, priority: QueuePriority, minutes_ago: int) -> QueueEntry:
        return QueueEntry(
            job_id=job_id,
            user_id=job_id,
            priority=priority,
            enqueued_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            estimated_seconds=60,
        )

    def test_priority_follows_the_tier(self, queue_service: QueueService) -> None:
        assert queue_service.priority_for(SubscriptionTier.FREE) is QueuePriority.LOW
        assert queue_service.priority_for(SubscriptionTier.VIP) is QueuePriority.HIGH
        assert queue_service.priority_for(SubscriptionTier.LIFETIME) is QueuePriority.CRITICAL

    def test_higher_priority_is_served_first(self, queue_service: QueueService) -> None:
        entries = [
            self._entry(1, QueuePriority.LOW, 10),
            self._entry(2, QueuePriority.CRITICAL, 1),
            self._entry(3, QueuePriority.NORMAL, 5),
        ]
        assert [entry.job_id for entry in queue_service.order(entries)] == [2, 3, 1]

    def test_fifo_within_the_same_priority(self, queue_service: QueueService) -> None:
        entries = [
            self._entry(1, QueuePriority.LOW, 1),
            self._entry(2, QueuePriority.LOW, 9),
        ]
        assert [entry.job_id for entry in queue_service.order(entries)] == [2, 1]

    def test_position_and_eta_account_for_workers(self, queue_service: QueueService) -> None:
        # job 1 was enqueued 9 minutes ago, job 4 six minutes ago -> FIFO order.
        entries = [self._entry(i, QueuePriority.NORMAL, 10 - i) for i in range(1, 5)]
        first = queue_service.position_of(entries, job_id=1, workers=2)
        last = queue_service.position_of(entries, job_id=4, workers=2)
        assert first is not None and last is not None
        assert first.position == 1
        assert last.position == 4
        assert last.total == 4
        # 3 jobs ahead x 60s / 2 workers + own 45s baseline
        assert last.estimated_wait_seconds == pytest.approx(135, abs=5)

    def test_unknown_job_has_no_position(self, queue_service: QueueService) -> None:
        assert queue_service.position_of([], job_id=99, workers=2) is None

    def test_job_estimate_grows_with_size_and_conversion(self, queue_service: QueueService) -> None:
        plain = queue_service.estimate_job_seconds(
            duration_seconds=600, size_bytes=100 * 1024**2, needs_conversion=False
        )
        converted = queue_service.estimate_job_seconds(
            duration_seconds=600, size_bytes=100 * 1024**2, needs_conversion=True
        )
        assert converted > plain

    def test_should_queue_when_pool_is_saturated(self, queue_service: QueueService) -> None:
        assert queue_service.should_queue(running_jobs=4, capacity=4)
        assert not queue_service.should_queue(running_jobs=1, capacity=4)

    def test_stale_entries_are_detected(self, queue_service: QueueService) -> None:
        entries = [self._entry(1, QueuePriority.LOW, 120), self._entry(2, QueuePriority.LOW, 1)]
        stale = queue_service.stale_entries(entries, max_age_seconds=3600)
        assert [entry.job_id for entry in stale] == [1]


# --------------------------------------------------------------------------- #
# Economy
# --------------------------------------------------------------------------- #
class TestEconomy:
    def test_first_claim_starts_a_streak(self, economy_service: EconomyService) -> None:
        result = economy_service.claim_daily_bonus(last_claim_at=None, current_streak=0)
        assert result.granted
        assert result.streak == 1
        assert result.coins == 10

    def test_second_claim_on_the_same_day_is_refused(self, economy_service: EconomyService) -> None:
        now = datetime.now(UTC)
        result = economy_service.claim_daily_bonus(last_claim_at=now, current_streak=3, now=now)
        assert not result.granted
        assert result.streak == 3

    def test_consecutive_days_increase_the_reward(self, economy_service: EconomyService) -> None:
        now = datetime.now(UTC)
        result = economy_service.claim_daily_bonus(
            last_claim_at=now - timedelta(days=1), current_streak=4, now=now
        )
        assert result.streak == 5
        assert result.coins == 10 + 4 * 5

    def test_a_missed_day_resets_the_streak(self, economy_service: EconomyService) -> None:
        now = datetime.now(UTC)
        result = economy_service.claim_daily_bonus(
            last_claim_at=now - timedelta(days=3), current_streak=9, now=now
        )
        assert result.streak_broken
        assert result.streak == 1

    def test_reward_is_capped(self, economy_service: EconomyService) -> None:
        now = datetime.now(UTC)
        result = economy_service.claim_daily_bonus(
            last_claim_at=now - timedelta(days=1), current_streak=99, now=now
        )
        assert result.coins == 100

    def test_calendar_marks_claimed_days(self, economy_service: EconomyService) -> None:
        calendar = economy_service.daily_calendar(streak=3)
        assert len(calendar) == 7
        assert [claimed for _day, _coins, claimed in calendar][:3] == [True, True, True]

    def test_every_fifth_referral_grants_a_premium_day(
        self, economy_service: EconomyService
    ) -> None:
        assert economy_service.referral_reward(4).inviter_premium_days == 0
        assert economy_service.referral_reward(5).inviter_premium_days == 1

    def test_referral_codes_are_deterministic_and_unique(
        self, economy_service: EconomyService
    ) -> None:
        assert economy_service.referral_code(12345) == economy_service.referral_code(12345)
        assert economy_service.referral_code(1) != economy_service.referral_code(2)
        assert economy_service.referral_code(999).startswith("REF")

    def test_shop_purchase_resolution(self, economy_service: EconomyService) -> None:
        purchase = economy_service.resolve_purchase("premium_7d")
        assert purchase is not None
        assert purchase.tier is SubscriptionTier.PREMIUM
        assert purchase.days == 7
        assert economy_service.resolve_purchase("nope") is None


class TestAchievements:
    def test_unlocks_by_threshold(self) -> None:
        service = AchievementService()
        unlocked = service.evaluate({"downloads": 12}, set())
        codes = {definition.code for definition in unlocked}
        assert "first_download" in codes
        assert "downloads_10" in codes
        assert "downloads_100" not in codes

    def test_already_unlocked_are_skipped(self) -> None:
        service = AchievementService()
        unlocked = service.evaluate({"downloads": 12}, {"first_download", "downloads_10"})
        assert unlocked == ()

    def test_progress_covers_every_achievement(self) -> None:
        service = AchievementService()
        progress = service.progress({"downloads": 5})
        assert len(progress) == len(service.all_definitions())


# --------------------------------------------------------------------------- #
# Promo codes
# --------------------------------------------------------------------------- #
class TestPromoService:
    def _snapshot(self, **kwargs: object) -> PromoSnapshot:
        base = {
            "code": "TEST10",
            "promo_type": PromoType.COINS,
            "status": PromoStatus.ACTIVE,
            "value": 100,
            "max_activations": 10,
            "activations_used": 0,
            "per_user_limit": 1,
            "starts_at": None,
            "expires_at": None,
            "min_tier": None,
            "new_users_only": False,
        }
        base.update(kwargs)
        return PromoSnapshot(**base)  # type: ignore[arg-type]

    def test_valid_code_passes(self, promo_service: PromoService) -> None:
        promo_service.validate(
            self._snapshot(),
            user_activations=0,
            user_tier=SubscriptionTier.FREE,
            user_is_new=True,
        )

    def test_missing_code_raises(self, promo_service: PromoService) -> None:
        with pytest.raises(PromoCodeNotFoundError):
            promo_service.validate(
                None, user_activations=0, user_tier=SubscriptionTier.FREE, user_is_new=True
            )

    def test_expired_code_raises(self, promo_service: PromoService) -> None:
        snapshot = self._snapshot(expires_at=datetime.now(UTC) - timedelta(days=1))
        with pytest.raises(PromoCodeExpiredError):
            promo_service.validate(
                snapshot, user_activations=0, user_tier=SubscriptionTier.FREE, user_is_new=True
            )

    def test_exhausted_code_raises(self, promo_service: PromoService) -> None:
        snapshot = self._snapshot(max_activations=5, activations_used=5)
        with pytest.raises(PromoCodeExhaustedError):
            promo_service.validate(
                snapshot, user_activations=0, user_tier=SubscriptionTier.FREE, user_is_new=True
            )

    def test_per_user_limit_raises(self, promo_service: PromoService) -> None:
        with pytest.raises(PromoCodeAlreadyUsedError):
            promo_service.validate(
                self._snapshot(),
                user_activations=1,
                user_tier=SubscriptionTier.FREE,
                user_is_new=True,
            )

    def test_new_users_only(self, promo_service: PromoService) -> None:
        with pytest.raises(PromoCodeNotFoundError):
            promo_service.validate(
                self._snapshot(new_users_only=True),
                user_activations=0,
                user_tier=SubscriptionTier.FREE,
                user_is_new=False,
            )

    def test_minimum_tier(self, promo_service: PromoService) -> None:
        with pytest.raises(PromoCodeNotFoundError):
            promo_service.validate(
                self._snapshot(min_tier=SubscriptionTier.VIP),
                user_activations=0,
                user_tier=SubscriptionTier.PREMIUM,
                user_is_new=False,
            )

    @pytest.mark.parametrize(
        ("promo_type", "value", "attribute", "expected"),
        [
            (PromoType.COINS, 250, "coins", 250),
            (PromoType.PREMIUM_DAYS, 7, "premium_days", 7),
            (PromoType.VIP_DAYS, 3, "vip_days", 3),
            (PromoType.LIMIT_BOOST, 20, "extra_daily_downloads", 20),
            (PromoType.PERCENT_DISCOUNT, 30, "percent_discount", 30),
            (PromoType.FIXED_DISCOUNT, 50, "fixed_discount", 50),
        ],
    )
    def test_reward_mapping(
        self,
        promo_service: PromoService,
        promo_type: PromoType,
        value: int,
        attribute: str,
        expected: int,
    ) -> None:
        reward = promo_service.reward_for(self._snapshot(promo_type=promo_type, value=value))
        assert getattr(reward, attribute) == expected

    def test_percent_discount_is_applied(self, promo_service: PromoService) -> None:
        reward = promo_service.reward_for(
            self._snapshot(promo_type=PromoType.PERCENT_DISCOUNT, value=25)
        )
        assert promo_service.apply_discount(200, reward) == 150

    def test_discount_never_goes_negative(self, promo_service: PromoService) -> None:
        reward = promo_service.reward_for(
            self._snapshot(promo_type=PromoType.FIXED_DISCOUNT, value=500)
        )
        assert promo_service.apply_discount(100, reward) == 0

    def test_status_transitions_to_exhausted(self, promo_service: PromoService) -> None:
        snapshot = self._snapshot(max_activations=2, activations_used=2)
        assert promo_service.next_status(snapshot) is PromoStatus.EXHAUSTED

    def test_generated_codes_are_unique(self, promo_service: PromoService) -> None:
        codes = {promo_service.generate_code() for _ in range(50)}
        assert len(codes) == 50

    def test_normalisation_is_case_insensitive(self, promo_service: PromoService) -> None:
        assert promo_service.normalize("  free10 ") == "FREE10"


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
class TestValueObjects:
    def test_humanize_bytes(self) -> None:
        assert humanize_bytes(0) == "—"
        assert humanize_bytes(512) == "512 B"
        assert humanize_bytes(1024) == "1.0 KB"
        assert humanize_bytes(5 * 1024**3) == "5.0 GB"

    def test_humanize_duration(self) -> None:
        assert humanize_duration(None) == "—"
        assert humanize_duration(75) == "1:15"
        assert humanize_duration(3725) == "1:02:05"

    def test_quality_from_height_snaps_down(self) -> None:
        assert VideoQuality.from_height(1085) is VideoQuality.P1080
        assert VideoQuality.from_height(700) is VideoQuality.P480
        assert VideoQuality.from_height(None) is VideoQuality.P360

    def test_media_info_reports_available_qualities(self) -> None:
        info = MediaInfo(
            source_url="https://youtube.com/watch?v=1",
            platform=Platform.YOUTUBE,
            title="Demo",
            duration=120,
            formats=(
                MediaFormat(
                    "18",
                    "mp4",
                    MediaKind.VIDEO,
                    height=360,
                    vcodec="h264",
                    acodec="aac",
                    filesize=10_000_000,
                ),
                MediaFormat(
                    "22",
                    "mp4",
                    MediaKind.VIDEO,
                    height=720,
                    vcodec="h264",
                    acodec="aac",
                    filesize=30_000_000,
                ),
                MediaFormat("140", "m4a", MediaKind.AUDIO, acodec="aac", filesize=2_000_000),
            ),
        )
        assert info.available_video_qualities() == (VideoQuality.P360, VideoQuality.P720)
        assert info.has_video_stream
        assert info.estimated_size(VideoQuality.P720) == 30_000_000
        assert info.estimated_size(VideoQuality.AUDIO_ONLY) == 2_000_000

    def test_cache_key_distinguishes_parameters(self) -> None:
        base = {
            "user_id": 1,
            "url": "https://x.tld/1",
            "platform": Platform.YOUTUBE,
            "kind": MediaKind.VIDEO,
        }
        first = DownloadRequest(**base, video_quality=VideoQuality.P720)  # type: ignore[arg-type]
        second = DownloadRequest(**base, video_quality=VideoQuality.P1080)  # type: ignore[arg-type]
        assert first.cache_key() != second.cache_key()

    def test_subscription_tiers_are_ordered(self) -> None:
        assert SubscriptionTier.FREE < SubscriptionTier.PREMIUM
        assert SubscriptionTier.VIP > SubscriptionTier.PREMIUM
        assert SubscriptionTier.LIFETIME >= SubscriptionTier.VIP
