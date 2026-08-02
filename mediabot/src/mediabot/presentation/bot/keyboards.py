"""Inline and reply keyboard builders.

Every keyboard is a pure function of (state, translator), which keeps handlers
short and makes the markup independently testable.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from mediabot.application.dto import MediaPreview
from mediabot.domain.enums import (
    AudioFormat,
    AudioQuality,
    Language,
    MediaKind,
    PaymentProvider,
    SubscriptionTier,
    VideoFormat,
    VideoQuality,
)
from mediabot.domain.policies import COIN_SHOP, SUBSCRIPTION_PLANS
from mediabot.domain.value_objects import humanize_bytes
from mediabot.presentation.bot.callbacks import (
    AdminCallback,
    CaptchaCallback,
    DownloadActionCallback,
    DownloadCallback,
    FavoriteCallback,
    HistoryCallback,
    MenuCallback,
    SettingsCallback,
    SubscriptionCallback,
    WalletCallback,
)
from mediabot.presentation.bot.i18n.translator import Translator


def main_menu(t: Translator, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Persistent reply keyboard shown under the input field."""
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text=t("menu.profile")),
        KeyboardButton(text=t("menu.history")),
    )
    builder.row(
        KeyboardButton(text=t("menu.favorites")),
        KeyboardButton(text=t("menu.subscription")),
    )
    builder.row(
        KeyboardButton(text=t("menu.wallet")),
        KeyboardButton(text=t("menu.referral")),
    )
    builder.row(
        KeyboardButton(text=t("menu.achievements")),
        KeyboardButton(text=t("menu.settings")),
    )
    if is_admin:
        builder.row(KeyboardButton(text=t("menu.admin")))
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def back_button(t: Translator, section: str = "root") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.back"), callback_data=MenuCallback(section=section))
    return builder.as_markup()


def format_choice(preview: MediaPreview, token: str, t: Translator) -> InlineKeyboardMarkup:
    """Video / audio switch — the first step after a link is resolved."""
    builder = InlineKeyboardBuilder()
    if preview.info.has_video_stream and preview.allowed_video_qualities:
        builder.button(
            text=t("link.video"),
            callback_data=DownloadCallback(token=token, kind=MediaKind.VIDEO, quality="menu"),
        )
    builder.button(
        text=t("link.audio"),
        callback_data=DownloadCallback(token=token, kind=MediaKind.AUDIO, quality="menu"),
    )
    builder.adjust(2)
    return builder.as_markup()


def video_quality_choice(
    preview: MediaPreview,
    token: str,
    t: Translator,
    *,
    container: VideoFormat = VideoFormat.MP4,
) -> InlineKeyboardMarkup:
    """One button per allowed quality, annotated with the estimated size."""
    builder = InlineKeyboardBuilder()
    for quality in preview.allowed_video_qualities:
        size = preview.estimated_sizes.get(quality.value)
        label = quality.label if quality is VideoQuality.BEST else quality.value
        suffix = f" · {humanize_bytes(size)}" if size else ""
        builder.button(
            text=f"{label}{suffix}",
            callback_data=DownloadCallback(
                token=token,
                kind=MediaKind.VIDEO,
                quality=quality.value,
                container=container.value,
            ),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"),
            callback_data=DownloadCallback(
                token=token, kind=MediaKind.VIDEO, quality="back"
            ).pack(),
        )
    )
    return builder.as_markup()


def audio_quality_choice(
    preview: MediaPreview,
    token: str,
    t: Translator,
    *,
    container: AudioFormat = AudioFormat.MP3,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for quality in preview.allowed_audio_qualities:
        builder.button(
            text=quality.label,
            callback_data=DownloadCallback(
                token=token,
                kind=MediaKind.AUDIO,
                quality=quality.value,
                container=container.value,
            ),
        )
    builder.adjust(2)
    for fmt in (AudioFormat.MP3, AudioFormat.M4A, AudioFormat.FLAC, AudioFormat.OGG):
        builder.button(
            text=f"📦 {fmt.value.upper()}",
            callback_data=DownloadCallback(
                token=token,
                kind=MediaKind.AUDIO,
                quality=AudioQuality.KBPS_320.value,
                container=fmt.value,
            ),
        )
    builder.adjust(2, 2, 4)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"),
            callback_data=DownloadCallback(
                token=token, kind=MediaKind.AUDIO, quality="back"
            ).pack(),
        )
    )
    return builder.as_markup()


def download_progress_keyboard(download_id: int, t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("download.cancel_button"),
        callback_data=DownloadActionCallback(action="cancel", download_id=download_id),
    )
    builder.button(
        text="🔄",
        callback_data=DownloadActionCallback(action="refresh", download_id=download_id),
    )
    builder.adjust(2)
    return builder.as_markup()


def download_result_keyboard(download_id: int, t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("download.add_favorite"),
        callback_data=DownloadActionCallback(action="favorite", download_id=download_id),
    )
    builder.button(
        text=t("download.repeat"),
        callback_data=DownloadActionCallback(action="repeat", download_id=download_id),
    )
    builder.adjust(2)
    return builder.as_markup()


def history_keyboard(
    entries: list[tuple[int, str]],
    *,
    page: int,
    pages: int,
    kind: str,
    t: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for entry_id, label in entries:
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=HistoryCallback(action="open", entry_id=entry_id, page=page).pack(),
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 1:
        navigation.append(
            InlineKeyboardButton(
                text=t("common.prev"),
                callback_data=HistoryCallback(action="page", page=page - 1, kind=kind).pack(),
            )
        )
    if page < pages:
        navigation.append(
            InlineKeyboardButton(
                text=t("common.next"),
                callback_data=HistoryCallback(action="page", page=page + 1, kind=kind).pack(),
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text=t("history.filter_all"),
            callback_data=HistoryCallback(action="filter", kind="all").pack(),
        ),
        InlineKeyboardButton(
            text=t("history.filter_video"),
            callback_data=HistoryCallback(action="filter", kind="video").pack(),
        ),
        InlineKeyboardButton(
            text=t("history.filter_audio"),
            callback_data=HistoryCallback(action="filter", kind="audio").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=t("history.search"),
            callback_data=HistoryCallback(action="search").pack(),
        )
    )
    return builder.as_markup()


def favorites_keyboard(
    entries: list[tuple[int, str]],
    *,
    page: int,
    pages: int,
    t: Translator,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for entry_id, label in entries:
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=FavoriteCallback(action="open", entry_id=entry_id).pack(),
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=FavoriteCallback(action="delete", entry_id=entry_id).pack(),
            ),
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 1:
        navigation.append(
            InlineKeyboardButton(
                text=t("common.prev"),
                callback_data=FavoriteCallback(action="page", page=page - 1).pack(),
            )
        )
    if page < pages:
        navigation.append(
            InlineKeyboardButton(
                text=t("common.next"),
                callback_data=FavoriteCallback(action="page", page=page + 1).pack(),
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text=t("favorites.collections"),
            callback_data=FavoriteCallback(action="collections").pack(),
        ),
        InlineKeyboardButton(
            text=t("favorites.new_collection"),
            callback_data=FavoriteCallback(action="new_collection").pack(),
        ),
    )
    return builder.as_markup()


def subscription_plans(t: Translator, currency: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tier, days, price, _stars in SUBSCRIPTION_PLANS:
        label = "∞" if days > 3650 else f"{days}d"
        builder.button(
            text=f"{tier.emoji} {tier.value.title()} · {label} · {price} {currency}",
            callback_data=SubscriptionCallback(action="plan", tier=tier, days=days),
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text=t("promo.prompt"),
            callback_data=SubscriptionCallback(action="promo").pack(),
        )
    )
    return builder.as_markup()


def payment_providers(
    providers: tuple[PaymentProvider, ...],
    tier: SubscriptionTier,
    days: int,
    t: Translator,
) -> InlineKeyboardMarkup:
    labels = {
        PaymentProvider.TELEGRAM_STARS: t("subscription.pay_stars"),
        PaymentProvider.CARD: t("subscription.pay_card"),
        PaymentProvider.CRYPTO: t("subscription.pay_crypto"),
        PaymentProvider.BALANCE: t("subscription.pay_balance"),
    }
    builder = InlineKeyboardBuilder()
    for provider in providers:
        builder.button(
            text=labels[provider],
            callback_data=SubscriptionCallback(
                action="pay", tier=tier, days=days, provider=provider.value
            ),
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"),
            callback_data=SubscriptionCallback(action="plans").pack(),
        )
    )
    return builder.as_markup()


def wallet_keyboard(t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("wallet.daily"), callback_data=WalletCallback(action="daily"))
    builder.button(text=t("wallet.shop"), callback_data=WalletCallback(action="shop"))
    builder.button(text=t("wallet.statement"), callback_data=WalletCallback(action="statement"))
    builder.adjust(1, 2)
    return builder.as_markup()


def shop_keyboard(t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item, (price, _payload) in COIN_SHOP.items():
        builder.button(
            text=f"{item} — {price} 🪙",
            callback_data=WalletCallback(action="buy", item=item),
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"), callback_data=WalletCallback(action="root").pack()
        )
    )
    return builder.as_markup()


def language_keyboard(current: Language, t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for language in Language:
        mark = " ✅" if language is current else ""
        builder.button(
            text=f"{language.flag} {language.display_name}{mark}",
            callback_data=SettingsCallback(action="language", language=language),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"), callback_data=SettingsCallback(action="root").pack()
        )
    )
    return builder.as_markup()


def settings_keyboard(
    t: Translator,
    *,
    notifications_enabled: bool,
    auto_download: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("settings.language"), callback_data=SettingsCallback(action="languages"))
    builder.button(
        text=t(
            "settings.notifications",
            state=t(
                "settings.notifications_on"
                if notifications_enabled
                else "settings.notifications_off"
            ),
        ),
        callback_data=SettingsCallback(action="toggle_notifications"),
    )
    builder.button(
        text=t(
            "settings.auto_download",
            state=t("settings.notifications_on" if auto_download else "settings.notifications_off"),
        ),
        callback_data=SettingsCallback(action="toggle_auto"),
    )
    builder.button(
        text=t("settings.format"),
        callback_data=SettingsCallback(action="formats"),
    )
    builder.adjust(1)
    return builder.as_markup()


def format_preferences_keyboard(t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for video_format in VideoFormat:
        builder.button(
            text=f"🎬 {video_format.value.upper()}",
            callback_data=SettingsCallback(action="set_video_format", video_format=video_format),
        )
    for audio_format in AudioFormat:
        builder.button(
            text=f"🎵 {audio_format.value.upper()}",
            callback_data=SettingsCallback(action="set_audio_format", audio_format=audio_format),
        )
    builder.adjust(3, 5)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"), callback_data=SettingsCallback(action="root").pack()
        )
    )
    return builder.as_markup()


def captcha_keyboard(options: tuple[int, ...]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for option in options:
        builder.button(text=str(option), callback_data=CaptchaCallback(answer=option))
    builder.adjust(len(options))
    return builder.as_markup()


def referral_keyboard(link: str, t: Translator) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url={link}"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t("referral.share"), url=share_url))
    return builder.as_markup()


def admin_menu(t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.stats"), callback_data=AdminCallback(action="stats"))
    builder.button(text=t("admin.users"), callback_data=AdminCallback(action="users"))
    builder.button(text=t("admin.queue"), callback_data=AdminCallback(action="queue"))
    builder.button(text=t("admin.promos"), callback_data=AdminCallback(action="promos"))
    builder.button(text=t("admin.broadcast"), callback_data=AdminCallback(action="broadcast"))
    builder.button(text=t("admin.errors"), callback_data=AdminCallback(action="errors"))
    builder.button(text=t("admin.logs"), callback_data=AdminCallback(action="logs"))
    builder.adjust(2)
    return builder.as_markup()


def admin_user_actions(user_id: int, t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⛔ Ban", callback_data=AdminCallback(action="ban", target_id=user_id))
    builder.button(text="✅ Unban", callback_data=AdminCallback(action="unban", target_id=user_id))
    builder.button(text="🔇 Mute 1h", callback_data=AdminCallback(action="mute", target_id=user_id))
    builder.button(
        text="🔊 Unmute", callback_data=AdminCallback(action="unmute", target_id=user_id)
    )
    builder.button(
        text="⭐ +30d Premium",
        callback_data=AdminCallback(action="grant", target_id=user_id, value="premium"),
    )
    builder.button(
        text="💎 +30d VIP",
        callback_data=AdminCallback(action="grant", target_id=user_id, value="vip"),
    )
    builder.button(
        text="🪙 +100 coins",
        callback_data=AdminCallback(action="coins", target_id=user_id, value="100"),
    )
    builder.button(
        text="🚫 Revoke sub",
        callback_data=AdminCallback(action="revoke", target_id=user_id),
    )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"), callback_data=AdminCallback(action="root").pack()
        )
    )
    return builder.as_markup()


def admin_queue_actions(t: Translator) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Clear queue", callback_data=AdminCallback(action="queue_clear"))
    builder.button(text="🔄 Refresh", callback_data=AdminCallback(action="queue"))
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text=t("common.back"), callback_data=AdminCallback(action="root").pack()
        )
    )
    return builder.as_markup()
