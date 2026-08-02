"""Typed callback-data factories.

Using aiogram's :class:`CallbackData` instead of raw strings gives us parsing,
validation and type safety for free, and keeps every payload under Telegram's
64-byte limit because only short field values are packed.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from mediabot.domain.enums import (
    AudioFormat,
    Language,
    MediaKind,
    SubscriptionTier,
    VideoFormat,
)


class MenuCallback(CallbackData, prefix="menu"):
    """Navigation between the main sections."""

    section: str


class DownloadCallback(CallbackData, prefix="dl"):
    """Format/quality choice for a resolved media item.

    ``token`` references the resolved :class:`MediaInfo` stored in the FSM, so
    the (potentially very long) URL never travels inside callback data.
    """

    token: str
    kind: MediaKind
    quality: str
    container: str = ""


class DownloadActionCallback(CallbackData, prefix="dla"):
    """Actions on an existing job (cancel, refresh, repeat, favourite)."""

    action: str
    download_id: int


class HistoryCallback(CallbackData, prefix="hist"):
    """History browsing: paging, filtering, opening an entry."""

    action: str
    page: int = 1
    entry_id: int = 0
    kind: str = "all"


class FavoriteCallback(CallbackData, prefix="fav"):
    """Favourites and collections."""

    action: str
    page: int = 1
    entry_id: int = 0
    collection_id: int = 0


class SubscriptionCallback(CallbackData, prefix="sub"):
    """Plan selection and checkout."""

    action: str
    tier: SubscriptionTier = SubscriptionTier.PREMIUM
    days: int = 0
    provider: str = ""


class WalletCallback(CallbackData, prefix="wal"):
    """Wallet, shop and daily bonus."""

    action: str
    item: str = ""
    page: int = 1


class SettingsCallback(CallbackData, prefix="set"):
    """Preferences."""

    action: str
    language: Language = Language.EN
    video_format: VideoFormat = VideoFormat.MP4
    audio_format: AudioFormat = AudioFormat.MP3
    value: str = ""


class CaptchaCallback(CallbackData, prefix="cap"):
    """CAPTCHA answer buttons."""

    answer: int


class AdminCallback(CallbackData, prefix="adm"):
    """Admin panel navigation and actions."""

    action: str
    target_id: int = 0
    value: str = ""
    page: int = 1


class ConfirmCallback(CallbackData, prefix="cfm"):
    """Generic yes/no confirmation."""

    action: str
    target_id: int = 0
    confirmed: bool = False
