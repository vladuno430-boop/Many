"""Static business policies: subscription tiers, achievements, shop prices.

Architecture note
-----------------
These tables are pure data.  Keeping them in the domain layer (instead of the
database) makes limits deterministic, diff-reviewable and unit-testable, while
per-user *overrides* still live in the ``user_limits`` table and are merged on
top of the tier policy at runtime by :mod:`mediabot.domain.services.limits`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from mediabot.domain.enums import (
    AchievementCode,
    AudioQuality,
    Platform,
    SubscriptionTier,
    VideoQuality,
)
from mediabot.domain.value_objects import AchievementDefinition, TierPolicy

MB: Final[int] = 1024 * 1024
GB: Final[int] = 1024 * MB

#: Platforms available to free users.  Paid tiers unlock everything.
FREE_PLATFORMS: Final[frozenset[Platform]] = frozenset(
    {
        Platform.YOUTUBE,
        Platform.YOUTUBE_SHORTS,
        Platform.TIKTOK,
        Platform.INSTAGRAM,
        Platform.INSTAGRAM_REELS,
        Platform.VK_VIDEO,
        Platform.VK_CLIPS,
        Platform.RUTUBE,
        Platform.TWITTER,
        Platform.REDDIT,
        Platform.PINTEREST,
        Platform.SOUNDCLOUD,
        Platform.GENERIC,
    }
)


TIER_POLICIES: Final[MappingProxyType[SubscriptionTier, TierPolicy]] = MappingProxyType(
    {
        SubscriptionTier.FREE: TierPolicy(
            tier=SubscriptionTier.FREE.value,
            daily_downloads=10,
            max_file_size_bytes=250 * MB,
            max_duration_seconds=20 * 60,
            max_concurrent_jobs=1,
            speed_limit_bytes_per_sec=2 * MB,
            max_video_quality=VideoQuality.P720,
            max_audio_quality=AudioQuality.KBPS_192,
            allow_lossless=False,
            allow_playlists=False,
            playlist_max_items=1,
            allowed_platforms=FREE_PLATFORMS,
            ads_enabled=True,
            priority=0,
            monthly_price=0,
            features=("basic_quality", "history_7_days"),
        ),
        SubscriptionTier.PREMIUM: TierPolicy(
            tier=SubscriptionTier.PREMIUM.value,
            daily_downloads=100,
            max_file_size_bytes=2 * GB,
            max_duration_seconds=3 * 3600,
            max_concurrent_jobs=3,
            speed_limit_bytes_per_sec=None,
            max_video_quality=VideoQuality.P1080,
            max_audio_quality=AudioQuality.KBPS_320,
            allow_lossless=False,
            allow_playlists=True,
            playlist_max_items=25,
            allowed_platforms=None,
            ads_enabled=False,
            priority=10,
            monthly_price=199,
            features=("all_platforms", "no_ads", "priority_queue", "history_unlimited"),
        ),
        SubscriptionTier.VIP: TierPolicy(
            tier=SubscriptionTier.VIP.value,
            daily_downloads=500,
            max_file_size_bytes=4 * GB,
            max_duration_seconds=8 * 3600,
            max_concurrent_jobs=5,
            speed_limit_bytes_per_sec=None,
            max_video_quality=VideoQuality.P2160,
            max_audio_quality=AudioQuality.LOSSLESS,
            allow_lossless=True,
            allow_playlists=True,
            playlist_max_items=100,
            allowed_platforms=None,
            ads_enabled=False,
            priority=20,
            monthly_price=499,
            features=(
                "all_platforms",
                "no_ads",
                "4k_quality",
                "lossless_audio",
                "high_priority",
                "batch_downloads",
            ),
        ),
        SubscriptionTier.LIFETIME: TierPolicy(
            tier=SubscriptionTier.LIFETIME.value,
            daily_downloads=None,
            max_file_size_bytes=8 * GB,
            max_duration_seconds=None,
            max_concurrent_jobs=8,
            speed_limit_bytes_per_sec=None,
            max_video_quality=VideoQuality.BEST,
            max_audio_quality=AudioQuality.LOSSLESS,
            allow_lossless=True,
            allow_playlists=True,
            playlist_max_items=500,
            allowed_platforms=None,
            ads_enabled=False,
            priority=30,
            monthly_price=4990,
            features=(
                "unlimited_downloads",
                "all_platforms",
                "no_ads",
                "best_quality",
                "lossless_audio",
                "max_priority",
                "batch_downloads",
                "early_access",
            ),
        ),
    }
)


def get_tier_policy(tier: SubscriptionTier) -> TierPolicy:
    """Return the immutable policy attached to ``tier``."""
    return TIER_POLICIES[tier]


#: Catalogue offered in the subscription menu: (tier, days, price in RUB, stars).
SUBSCRIPTION_PLANS: Final[tuple[tuple[SubscriptionTier, int, int, int], ...]] = (
    (SubscriptionTier.PREMIUM, 7, 69, 45),
    (SubscriptionTier.PREMIUM, 30, 199, 130),
    (SubscriptionTier.PREMIUM, 365, 1490, 990),
    (SubscriptionTier.VIP, 30, 499, 330),
    (SubscriptionTier.VIP, 365, 3990, 2600),
    (SubscriptionTier.LIFETIME, 36500, 4990, 3300),
)


ACHIEVEMENTS: Final[MappingProxyType[AchievementCode, AchievementDefinition]] = MappingProxyType(
    {
        AchievementCode.FIRST_DOWNLOAD: AchievementDefinition(
            code=AchievementCode.FIRST_DOWNLOAD.value,
            icon="🌱",
            reward_coins=10,
            threshold=1,
            metric="downloads",
        ),
        AchievementCode.DOWNLOADS_10: AchievementDefinition(
            code=AchievementCode.DOWNLOADS_10.value,
            icon="🔟",
            reward_coins=25,
            threshold=10,
            metric="downloads",
        ),
        AchievementCode.DOWNLOADS_100: AchievementDefinition(
            code=AchievementCode.DOWNLOADS_100.value,
            icon="💯",
            reward_coins=100,
            threshold=100,
            metric="downloads",
        ),
        AchievementCode.DOWNLOADS_1000: AchievementDefinition(
            code=AchievementCode.DOWNLOADS_1000.value,
            icon="🏆",
            reward_coins=750,
            threshold=1000,
            metric="downloads",
        ),
        AchievementCode.MUSIC_LOVER: AchievementDefinition(
            code=AchievementCode.MUSIC_LOVER.value,
            icon="🎧",
            reward_coins=50,
            threshold=50,
            metric="audio_downloads",
        ),
        AchievementCode.CINEMA_FAN: AchievementDefinition(
            code=AchievementCode.CINEMA_FAN.value,
            icon="🎬",
            reward_coins=50,
            threshold=50,
            metric="video_downloads",
        ),
        AchievementCode.NIGHT_OWL: AchievementDefinition(
            code=AchievementCode.NIGHT_OWL.value,
            icon="🦉",
            reward_coins=30,
            threshold=10,
            metric="night_downloads",
            hidden=True,
        ),
        AchievementCode.STREAK_7: AchievementDefinition(
            code=AchievementCode.STREAK_7.value,
            icon="🔥",
            reward_coins=70,
            threshold=7,
            metric="daily_streak",
        ),
        AchievementCode.STREAK_30: AchievementDefinition(
            code=AchievementCode.STREAK_30.value,
            icon="☄️",
            reward_coins=300,
            threshold=30,
            metric="daily_streak",
        ),
        AchievementCode.INVITED_FRIEND: AchievementDefinition(
            code=AchievementCode.INVITED_FRIEND.value,
            icon="🤝",
            reward_coins=25,
            threshold=1,
            metric="referrals",
        ),
        AchievementCode.INVITED_5_FRIENDS: AchievementDefinition(
            code=AchievementCode.INVITED_5_FRIENDS.value,
            icon="👥",
            reward_coins=150,
            threshold=5,
            metric="referrals",
        ),
        AchievementCode.INVITED_25_FRIENDS: AchievementDefinition(
            code=AchievementCode.INVITED_25_FRIENDS.value,
            icon="🌟",
            reward_coins=1000,
            threshold=25,
            metric="referrals",
        ),
        AchievementCode.PREMIUM_MEMBER: AchievementDefinition(
            code=AchievementCode.PREMIUM_MEMBER.value,
            icon="⭐",
            reward_coins=50,
            threshold=1,
            metric="premium_days",
        ),
        AchievementCode.VIP_MEMBER: AchievementDefinition(
            code=AchievementCode.VIP_MEMBER.value,
            icon="💎",
            reward_coins=150,
            threshold=1,
            metric="vip_days",
        ),
        AchievementCode.POLYGLOT: AchievementDefinition(
            code=AchievementCode.POLYGLOT.value,
            icon="🗺",
            reward_coins=20,
            threshold=3,
            metric="languages_used",
            hidden=True,
        ),
        AchievementCode.COLLECTOR: AchievementDefinition(
            code=AchievementCode.COLLECTOR.value,
            icon="📚",
            reward_coins=60,
            threshold=25,
            metric="favorites",
        ),
    }
)


#: Coin shop: item code -> (price in coins, payload used by the wallet service).
COIN_SHOP: Final[MappingProxyType[str, tuple[int, dict[str, int | str]]]] = MappingProxyType(
    {
        "premium_1d": (100, {"tier": SubscriptionTier.PREMIUM.value, "days": 1}),
        "premium_7d": (600, {"tier": SubscriptionTier.PREMIUM.value, "days": 7}),
        "premium_30d": (2200, {"tier": SubscriptionTier.PREMIUM.value, "days": 30}),
        "vip_7d": (1500, {"tier": SubscriptionTier.VIP.value, "days": 7}),
        "downloads_5": (60, {"extra_downloads": 5}),
        "downloads_20": (200, {"extra_downloads": 20}),
        "size_boost_1g": (250, {"extra_file_size_bytes": GB}),
        "no_ads_30d": (400, {"no_ads_days": 30}),
    }
)
