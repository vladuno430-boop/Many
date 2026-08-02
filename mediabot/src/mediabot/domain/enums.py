"""Domain enumerations shared by every layer.

These enums are the vocabulary of the system.  They are plain ``StrEnum`` /
``IntEnum`` values so they serialise cleanly into PostgreSQL, JSON payloads and
Telegram callback data without adapters.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Language(StrEnum):
    """Interface languages supported by the bot."""

    RU = "ru"
    EN = "en"
    DE = "de"
    ES = "es"
    FR = "fr"

    @classmethod
    def default(cls) -> Language:
        return cls.EN

    @classmethod
    def parse(cls, raw: str | None) -> Language:
        """Best-effort mapping of a Telegram ``language_code`` to our enum."""
        if not raw:
            return cls.default()
        head = raw.split("-")[0].lower()
        try:
            return cls(head)
        except ValueError:
            return cls.default()

    @property
    def flag(self) -> str:
        return {
            Language.RU: "🇷🇺",
            Language.EN: "🇬🇧",
            Language.DE: "🇩🇪",
            Language.ES: "🇪🇸",
            Language.FR: "🇫🇷",
        }[self]

    @property
    def display_name(self) -> str:
        return {
            Language.RU: "Русский",
            Language.EN: "English",
            Language.DE: "Deutsch",
            Language.ES: "Español",
            Language.FR: "Français",
        }[self]


class SubscriptionTier(StrEnum):
    """Monetisation tiers, ordered from least to most privileged."""

    FREE = "free"
    PREMIUM = "premium"
    VIP = "vip"
    LIFETIME = "lifetime"

    @property
    def rank(self) -> int:
        return {
            SubscriptionTier.FREE: 0,
            SubscriptionTier.PREMIUM: 1,
            SubscriptionTier.VIP: 2,
            SubscriptionTier.LIFETIME: 3,
        }[self]

    def __ge__(self, other: object) -> bool:
        if isinstance(other, SubscriptionTier):
            return self.rank >= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, SubscriptionTier):
            return self.rank > other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, SubscriptionTier):
            return self.rank <= other.rank
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, SubscriptionTier):
            return self.rank < other.rank
        return NotImplemented

    @property
    def is_paid(self) -> bool:
        return self is not SubscriptionTier.FREE

    @property
    def emoji(self) -> str:
        return {
            SubscriptionTier.FREE: "🆓",
            SubscriptionTier.PREMIUM: "⭐",
            SubscriptionTier.VIP: "💎",
            SubscriptionTier.LIFETIME: "👑",
        }[self]


class SubscriptionStatus(StrEnum):
    """Lifecycle of a subscription record."""

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    PENDING = "pending"


class Platform(StrEnum):
    """Supported source platforms.

    ``GENERIC`` is the catch-all for any other site that yt-dlp handles; the
    resolver falls back to it so the bot never refuses an extractable link.
    """

    YOUTUBE = "youtube"
    YOUTUBE_SHORTS = "youtube_shorts"
    YOUTUBE_MUSIC = "youtube_music"
    TIKTOK = "tiktok"
    RUTUBE = "rutube"
    VK_VIDEO = "vk_video"
    VK_CLIPS = "vk_clips"
    INSTAGRAM = "instagram"
    INSTAGRAM_REELS = "instagram_reels"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    VIMEO = "vimeo"
    DAILYMOTION = "dailymotion"
    TWITCH = "twitch"
    SOUNDCLOUD = "soundcloud"
    MIXCLOUD = "mixcloud"
    BANDCAMP = "bandcamp"
    BILIBILI = "bilibili"
    PINTEREST = "pinterest"
    REDDIT = "reddit"
    TELEGRAM = "telegram"
    ODNOKLASSNIKI = "odnoklassniki"
    YANDEX_MUSIC = "yandex_music"
    SPOTIFY = "spotify"
    DEEZER = "deezer"
    LIKEE = "likee"
    KUAISHOU = "kuaishou"
    NICONICO = "niconico"
    STREAMABLE = "streamable"
    IMGUR = "imgur"
    TUMBLR = "tumblr"
    LINKEDIN = "linkedin"
    COUB = "coub"
    GENERIC = "generic"

    @property
    def display_name(self) -> str:
        """Human readable platform name (``title`` would shadow ``str.title``)."""
        return _PLATFORM_TITLES.get(self, self.value.replace("_", " ").title())

    @property
    def emoji(self) -> str:
        return _PLATFORM_EMOJI.get(self, "🌐")

    @property
    def is_audio_only(self) -> bool:
        """Platforms that only ever yield audio streams."""
        return self in {
            Platform.SOUNDCLOUD,
            Platform.MIXCLOUD,
            Platform.BANDCAMP,
            Platform.YANDEX_MUSIC,
            Platform.SPOTIFY,
            Platform.DEEZER,
            Platform.YOUTUBE_MUSIC,
        }

    @property
    def metadata_only(self) -> bool:
        """Platforms where we may only surface metadata, never media bytes.

        Streaming catalogues are DRM protected; the bot resolves the track and
        offers a licensed alternative source instead of ripping the stream.
        """
        return self in {Platform.SPOTIFY, Platform.DEEZER, Platform.YANDEX_MUSIC}


_PLATFORM_TITLES: dict[Platform, str] = {
    Platform.YOUTUBE: "YouTube",
    Platform.YOUTUBE_SHORTS: "YouTube Shorts",
    Platform.YOUTUBE_MUSIC: "YouTube Music",
    Platform.TIKTOK: "TikTok",
    Platform.RUTUBE: "Rutube",
    Platform.VK_VIDEO: "VK Видео",
    Platform.VK_CLIPS: "VK Клипы",
    Platform.INSTAGRAM: "Instagram",
    Platform.INSTAGRAM_REELS: "Instagram Reels",
    Platform.FACEBOOK: "Facebook",
    Platform.TWITTER: "Twitter / X",
    Platform.VIMEO: "Vimeo",
    Platform.DAILYMOTION: "Dailymotion",
    Platform.TWITCH: "Twitch",
    Platform.SOUNDCLOUD: "SoundCloud",
    Platform.MIXCLOUD: "MixCloud",
    Platform.BANDCAMP: "Bandcamp",
    Platform.BILIBILI: "Bilibili",
    Platform.PINTEREST: "Pinterest",
    Platform.REDDIT: "Reddit",
    Platform.TELEGRAM: "Telegram",
    Platform.ODNOKLASSNIKI: "Одноклассники",
    Platform.YANDEX_MUSIC: "Яндекс Музыка",
    Platform.SPOTIFY: "Spotify",
    Platform.DEEZER: "Deezer",
    Platform.NICONICO: "NicoNico",
    Platform.STREAMABLE: "Streamable",
    Platform.COUB: "Coub",
    Platform.GENERIC: "Other",
}

_PLATFORM_EMOJI: dict[Platform, str] = {
    Platform.YOUTUBE: "▶️",
    Platform.YOUTUBE_SHORTS: "⏩",
    Platform.YOUTUBE_MUSIC: "🎧",
    Platform.TIKTOK: "🎵",
    Platform.RUTUBE: "📺",
    Platform.VK_VIDEO: "🅥",
    Platform.VK_CLIPS: "🅥",
    Platform.INSTAGRAM: "📸",
    Platform.INSTAGRAM_REELS: "🎞",
    Platform.FACEBOOK: "📘",
    Platform.TWITTER: "🐦",
    Platform.VIMEO: "🎬",
    Platform.DAILYMOTION: "📹",
    Platform.TWITCH: "🟣",
    Platform.SOUNDCLOUD: "🔊",
    Platform.MIXCLOUD: "🎚",
    Platform.BANDCAMP: "🎸",
    Platform.BILIBILI: "📼",
    Platform.PINTEREST: "📌",
    Platform.REDDIT: "👽",
    Platform.TELEGRAM: "✈️",
    Platform.SPOTIFY: "🟢",
    Platform.YANDEX_MUSIC: "🟡",
}


class MediaKind(StrEnum):
    """What the user ultimately receives."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"


class VideoQuality(StrEnum):
    """Selectable video qualities."""

    P144 = "144p"
    P240 = "240p"
    P360 = "360p"
    P480 = "480p"
    P720 = "720p"
    P1080 = "1080p"
    P1440 = "1440p"
    P2160 = "2160p"
    BEST = "best"
    AUDIO_ONLY = "audio_only"

    @property
    def height(self) -> int:
        """Vertical resolution in pixels (``BEST`` maps to a practical ceiling)."""
        return {
            VideoQuality.P144: 144,
            VideoQuality.P240: 240,
            VideoQuality.P360: 360,
            VideoQuality.P480: 480,
            VideoQuality.P720: 720,
            VideoQuality.P1080: 1080,
            VideoQuality.P1440: 1440,
            VideoQuality.P2160: 2160,
            VideoQuality.BEST: 4320,
            VideoQuality.AUDIO_ONLY: 0,
        }[self]

    @property
    def label(self) -> str:
        return {
            VideoQuality.BEST: "🏆 Best",
            VideoQuality.AUDIO_ONLY: "🎵 Audio only",
        }.get(self, self.value)

    @classmethod
    def from_height(cls, height: int | None) -> VideoQuality:
        """Snap an arbitrary height down to the nearest selectable quality."""
        if not height:
            return cls.P360
        ladder = [
            cls.P2160,
            cls.P1440,
            cls.P1080,
            cls.P720,
            cls.P480,
            cls.P360,
            cls.P240,
            cls.P144,
        ]
        for quality in ladder:
            if height >= quality.height:
                return quality
        return cls.P144


class AudioQuality(StrEnum):
    """Selectable audio bitrates."""

    KBPS_128 = "128"
    KBPS_192 = "192"
    KBPS_256 = "256"
    KBPS_320 = "320"
    LOSSLESS = "lossless"

    @property
    def bitrate(self) -> int:
        """Nominal bitrate in kbps; lossless is reported as 1411 (CD quality)."""
        return 1411 if self is AudioQuality.LOSSLESS else int(self.value)

    @property
    def label(self) -> str:
        return "💿 Lossless" if self is AudioQuality.LOSSLESS else f"{self.value} kbps"


class VideoFormat(StrEnum):
    """Container formats offered for video downloads."""

    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"

    @property
    def mime(self) -> str:
        return {
            VideoFormat.MP4: "video/mp4",
            VideoFormat.MKV: "video/x-matroska",
            VideoFormat.WEBM: "video/webm",
        }[self]


class AudioFormat(StrEnum):
    """Container/codec formats offered for audio downloads."""

    MP3 = "mp3"
    M4A = "m4a"
    FLAC = "flac"
    WAV = "wav"
    OGG = "ogg"

    @property
    def mime(self) -> str:
        return {
            AudioFormat.MP3: "audio/mpeg",
            AudioFormat.M4A: "audio/mp4",
            AudioFormat.FLAC: "audio/flac",
            AudioFormat.WAV: "audio/wav",
            AudioFormat.OGG: "audio/ogg",
        }[self]

    @property
    def is_lossless(self) -> bool:
        return self in {AudioFormat.FLAC, AudioFormat.WAV}


class DownloadStatus(StrEnum):
    """Finite state machine for a download job."""

    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
            DownloadStatus.EXPIRED,
        }

    @property
    def is_active(self) -> bool:
        return self in {
            DownloadStatus.DOWNLOADING,
            DownloadStatus.PROCESSING,
            DownloadStatus.UPLOADING,
        }


class QueuePriority(IntEnum):
    """Queue weights — higher values are served first."""

    LOW = 0
    NORMAL = 10
    HIGH = 20
    CRITICAL = 30

    @classmethod
    def for_tier(cls, tier: SubscriptionTier) -> QueuePriority:
        return {
            SubscriptionTier.FREE: cls.LOW,
            SubscriptionTier.PREMIUM: cls.NORMAL,
            SubscriptionTier.VIP: cls.HIGH,
            SubscriptionTier.LIFETIME: cls.CRITICAL,
        }[tier]


class PromoType(StrEnum):
    """What a promo code grants when redeemed."""

    PERCENT_DISCOUNT = "percent_discount"
    FIXED_DISCOUNT = "fixed_discount"
    PREMIUM_DAYS = "premium_days"
    VIP_DAYS = "vip_days"
    LIFETIME = "lifetime"
    COINS = "coins"
    LIMIT_BOOST = "limit_boost"


class PromoStatus(StrEnum):
    """Administrative state of a promo code."""

    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class PaymentProvider(StrEnum):
    """Supported payment rails."""

    TELEGRAM_STARS = "telegram_stars"
    CARD = "card"
    CRYPTO = "crypto"
    BALANCE = "balance"


class PaymentStatus(StrEnum):
    """Lifecycle of a payment."""

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class TransactionType(StrEnum):
    """Direction of a wallet transaction."""

    CREDIT = "credit"
    DEBIT = "debit"


class TransactionReason(StrEnum):
    """Why coins moved — used for the wallet statement and analytics."""

    REFERRAL_BONUS = "referral_bonus"
    REFERRAL_JOIN = "referral_join"
    DAILY_BONUS = "daily_bonus"
    PROMO_CODE = "promo_code"
    ACHIEVEMENT = "achievement"
    TASK_REWARD = "task_reward"
    PURCHASE = "purchase"
    REFUND = "refund"
    SPEND_PREMIUM = "spend_premium"
    SPEND_DOWNLOADS = "spend_downloads"
    SPEND_LIMIT_BOOST = "spend_limit_boost"
    SPEND_NO_ADS = "spend_no_ads"
    ADMIN_ADJUSTMENT = "admin_adjustment"


class AchievementCode(StrEnum):
    """Stable identifiers for achievements (never renumber these)."""

    FIRST_DOWNLOAD = "first_download"
    DOWNLOADS_10 = "downloads_10"
    DOWNLOADS_100 = "downloads_100"
    DOWNLOADS_1000 = "downloads_1000"
    MUSIC_LOVER = "music_lover"
    CINEMA_FAN = "cinema_fan"
    NIGHT_OWL = "night_owl"
    STREAK_7 = "streak_7"
    STREAK_30 = "streak_30"
    INVITED_FRIEND = "invited_friend"
    INVITED_5_FRIENDS = "invited_5_friends"
    INVITED_25_FRIENDS = "invited_25_friends"
    PREMIUM_MEMBER = "premium_member"
    VIP_MEMBER = "vip_member"
    POLYGLOT = "polyglot"
    COLLECTOR = "collector"


class NotificationType(StrEnum):
    """Categories of outbound notifications (users can opt out per category)."""

    DOWNLOAD_READY = "download_ready"
    DOWNLOAD_FAILED = "download_failed"
    QUEUE_POSITION = "queue_position"
    SUBSCRIPTION_EXPIRING = "subscription_expiring"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    PROMOTION = "promotion"
    PROMO_CODE = "promo_code"
    SYSTEM_UPDATE = "system_update"
    ACHIEVEMENT_UNLOCKED = "achievement_unlocked"
    DAILY_BONUS_READY = "daily_bonus_ready"
    BROADCAST = "broadcast"


class NotificationStatus(StrEnum):
    """Delivery state of a queued notification."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class AdminRole(StrEnum):
    """Role-based access control for staff accounts."""

    SUPPORT = "support"
    MODERATOR = "moderator"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return {
            AdminRole.SUPPORT: 0,
            AdminRole.MODERATOR: 1,
            AdminRole.ADMIN: 2,
            AdminRole.OWNER: 3,
        }[self]

    def can(self, required: AdminRole) -> bool:
        """Return ``True`` when this role satisfies ``required``."""
        return self.rank >= required.rank


class AuditAction(StrEnum):
    """Auditable staff actions (append-only log)."""

    USER_BAN = "user_ban"
    USER_UNBAN = "user_unban"
    USER_MUTE = "user_mute"
    USER_UNMUTE = "user_unmute"
    GRANT_SUBSCRIPTION = "grant_subscription"
    REVOKE_SUBSCRIPTION = "revoke_subscription"
    UPDATE_LIMITS = "update_limits"
    CREATE_PROMO = "create_promo"
    UPDATE_PROMO = "update_promo"
    DISABLE_PROMO = "disable_promo"
    BROADCAST = "broadcast"
    QUEUE_CLEAR = "queue_clear"
    QUEUE_CANCEL_JOB = "queue_cancel_job"
    ADJUST_BALANCE = "adjust_balance"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    SETTINGS_UPDATE = "settings_update"


class UserStatus(StrEnum):
    """Moderation state of an end user."""

    ACTIVE = "active"
    MUTED = "muted"
    BANNED = "banned"
    DELETED = "deleted"
