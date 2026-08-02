"""Rendering helpers shared by the bot handlers.

Everything user-visible goes through :func:`escape_html` before being embedded
into an HTML-parsed message, which is the bot's XSS/entity-injection defence:
a media title containing ``<b>`` or ``&`` can never break the markup.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mediabot.application.dto import MediaPreview, UserStatistics
from mediabot.core.security import escape_html
from mediabot.domain.enums import MediaKind, SubscriptionTier
from mediabot.domain.platforms import supported_platforms
from mediabot.domain.value_objects import (
    DownloadProgress,
    TierPolicy,
    humanize_bytes,
    humanize_duration,
)
from mediabot.presentation.bot.i18n.translator import Translator


def render_media_info(preview: MediaPreview, t: Translator) -> str:
    """Caption of the preview message shown right after a link is resolved."""
    info = preview.info
    size = info.best_filesize
    text = t(
        "link.info",
        emoji=preview.platform.emoji,
        title=escape_html(info.title)[:200],
        uploader=escape_html(info.uploader or "—"),
        duration=info.duration_label,
        size=humanize_bytes(size),
        platform=preview.platform.display_name,
    )
    warnings = {
        "live_stream": t("link.live_warning"),
        "playlist_not_allowed": t("link.playlist_warning"),
        "metadata_only": t("link.metadata_only"),
    }
    if preview.warning and preview.warning in warnings:
        text = f"{text}\n\n{warnings[preview.warning]}"
    return text


def render_progress(progress: DownloadProgress, t: Translator) -> str:
    """Progress message body."""
    speed = humanize_bytes(progress.speed_bytes_per_sec) if progress.speed_bytes_per_sec else "—"
    return t(
        "download.progress",
        bar=progress.bar(),
        percent=f"{progress.percent:.1f}",
        done=humanize_bytes(progress.downloaded_bytes),
        total=humanize_bytes(progress.total_bytes),
        speed=speed,
        eta=humanize_duration(progress.eta_seconds),
    )


def render_profile(
    stats: UserStatistics, policy: TierPolicy, used_today: int, t: Translator
) -> str:
    """Profile screen."""
    expires = (
        t("profile.expires", date=stats.subscription_expires_at.strftime("%d.%m.%Y"))
        if stats.subscription_expires_at
        else ""
    )
    limit = (
        t("profile.unlimited") if policy.daily_downloads is None else str(policy.daily_downloads)
    )
    return t(
        "profile.title",
        user_id=stats.user_id,
        tier=f"{stats.tier.emoji} {stats.tier.value.title()}",
        expires=expires,
        downloads=stats.total_downloads,
        video=stats.video_downloads,
        audio=stats.audio_downloads,
        traffic=humanize_bytes(stats.total_bytes),
        balance=stats.balance,
        referrals=stats.referrals,
        achievements=stats.achievements,
        rank=stats.rank,
        streak=stats.daily_streak,
        registered=stats.registered_at.strftime("%d.%m.%Y"),
        used=used_today,
        limit=limit,
    )


def render_tier_benefits(policy: TierPolicy, t: Translator) -> str:
    """Bullet list of what a tier unlocks."""
    unlimited = t("profile.unlimited")
    return t(
        "subscription.benefits",
        downloads=unlimited if policy.daily_downloads is None else policy.daily_downloads,
        size=humanize_bytes(policy.max_file_size_bytes),
        duration=(
            unlimited
            if policy.max_duration_seconds is None
            else humanize_duration(policy.max_duration_seconds)
        ),
        quality=policy.max_video_quality.label,
        concurrent=policy.max_concurrent_jobs,
    )


def render_subscription(
    tier: SubscriptionTier,
    policy: TierPolicy,
    expires_at: datetime | None,
    t: Translator,
) -> str:
    expires = t("profile.expires", date=expires_at.strftime("%d.%m.%Y")) if expires_at else ""
    return t(
        "subscription.title",
        tier=f"{tier.emoji} {tier.value.title()}",
        expires=expires,
        benefits=render_tier_benefits(policy, t),
    )


def render_history_entry(
    index: int,
    *,
    title: str,
    kind: MediaKind,
    quality: str | None,
    size: int,
    created_at: datetime,
    t: Translator,
) -> str:
    return t(
        "history.entry",
        index=index,
        emoji="🎬" if kind is MediaKind.VIDEO else "🎵",
        title=escape_html(title)[:80],
        quality=quality or "—",
        size=humanize_bytes(size),
        date=created_at.strftime("%d.%m %H:%M"),
    )


def short_label(title: str, *, limit: int = 40) -> str:
    """Trim a title for use as a button caption."""
    clean = " ".join((title or "").split())
    return clean[: limit - 1] + "…" if len(clean) > limit else clean or "—"


def platforms_list(limit: int = 24) -> str:
    """Human readable list of supported platforms for the help screen."""
    platforms = supported_platforms()[:limit]
    return ", ".join(f"{platform.emoji} {platform.display_name}" for platform in platforms) + " …"


def platforms_count() -> int:
    """Advertised platform count (yt-dlp supports well over a thousand sites)."""
    return 1000


def time_until(moment: datetime) -> str:
    """Human readable countdown used by "come back later" messages."""
    delta = moment - datetime.now(UTC)
    return humanize_duration(max(int(delta.total_seconds()), 0))
