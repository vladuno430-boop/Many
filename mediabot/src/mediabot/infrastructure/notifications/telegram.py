"""Telegram delivery adapters.

Two responsibilities:

* :class:`TelegramDelivery` implements the
  :class:`~mediabot.application.services.download_service.MediaDelivery`
  protocol — it uploads a finished artefact and returns the reusable
  ``file_id`` that powers the instant-cache path.
* :class:`NotificationSender` drains the notification outbox, honouring
  Telegram's flood limits and per-user opt-outs.

Both live in the infrastructure layer because they know about aiogram; the
application layer only sees the protocols.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile

from mediabot.core.config import Settings
from mediabot.core.exceptions import MediaBotError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import escape_html
from mediabot.domain.enums import MediaKind, NotificationType
from mediabot.domain.value_objects import DownloadProgress, humanize_bytes
from mediabot.infrastructure.db.models.download import Download
from mediabot.infrastructure.db.models.system import Notification
from mediabot.presentation.bot.i18n.translator import translator_for

log = get_logger(LogChannel.DOWNLOAD, component="telegram_delivery")

#: Telegram rejects captions longer than this.
CAPTION_LIMIT = 1024


class TelegramDelivery:
    """Uploads finished media and keeps the status message in sync."""

    def __init__(self, bot: Bot, settings: Settings, uow_factory: object) -> None:
        self._bot = bot
        self._settings = settings
        self._uow_factory = uow_factory

    async def deliver(self, download: Download) -> str | None:
        """Send the artefact to the user and return its Telegram ``file_id``."""
        if not download.file_path or not Path(download.file_path).exists():
            raise MediaBotError("Artefact is missing on disk")
        chat_id = download.chat_id or download.user_id
        language = await self._language_of(download.user_id)
        t = translator_for(language)

        size = download.file_size or 0
        if size > self._settings.telegram.max_upload_bytes:
            await self._safe_send(
                chat_id,
                t(
                    "error.file_too_large",
                    limit=humanize_bytes(self._settings.telegram.max_upload_bytes),
                ),
            )
            return None

        caption = t(
            "download.ready",
            title=escape_html(download.title or "")[:200],
            size=humanize_bytes(size),
            quality=download.video_quality or download.audio_quality or download.target_format,
            bot=self._settings.telegram.bot_username,
        )[:CAPTION_LIMIT]

        media = FSInputFile(download.file_path, filename=download.file_name or None)
        extra = download.extra or {}
        thumbnail_path = extra.get("thumbnail_path")
        thumbnail = (
            FSInputFile(thumbnail_path)
            if thumbnail_path and Path(str(thumbnail_path)).exists()
            else None
        )

        try:
            if MediaKind(download.kind) is MediaKind.VIDEO:
                message = await self._bot.send_video(
                    chat_id,
                    media,
                    caption=caption,
                    duration=download.duration_seconds,
                    width=extra.get("width"),
                    height=extra.get("height"),
                    thumbnail=thumbnail,
                    supports_streaming=True,
                    reply_to_message_id=download.reply_to_message_id,
                )
                file_id = message.video.file_id if message.video else None
            else:
                message = await self._bot.send_audio(
                    chat_id,
                    media,
                    caption=caption,
                    duration=download.duration_seconds,
                    performer=download.uploader,
                    title=download.title,
                    thumbnail=thumbnail,
                    reply_to_message_id=download.reply_to_message_id,
                )
                file_id = message.audio.file_id if message.audio else None
        except TelegramRetryAfter as exc:
            log.warning("flood limit on delivery, sleeping {}s", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            return await self.deliver(download)
        except TelegramForbiddenError:
            log.warning("user {} blocked the bot", download.user_id)
            return None
        except TelegramBadRequest as exc:
            log.error("delivery rejected by Telegram: {}", exc.message)
            raise MediaBotError(f"Telegram rejected the file: {exc.message}") from exc

        await self._cleanup_status_message(download)
        return file_id

    async def report_progress(self, download: Download, progress: DownloadProgress) -> None:
        """Best-effort progress update of the status message."""
        if not download.status_message_id or not download.chat_id:
            return
        language = await self._language_of(download.user_id)
        t = translator_for(language)
        text = t(
            "download.progress",
            bar=progress.bar(),
            percent=f"{progress.percent:.1f}",
            done=humanize_bytes(progress.downloaded_bytes),
            total=humanize_bytes(progress.total_bytes),
            speed=humanize_bytes(progress.speed_bytes_per_sec or 0),
            eta=str(progress.eta_seconds or "—"),
        )
        try:
            await self._bot.edit_message_text(
                text=text,
                chat_id=download.chat_id,
                message_id=download.status_message_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            # "message is not modified" and blocked chats are both harmless.
            return

    async def report_failure(self, download: Download, error: MediaBotError) -> None:
        language = await self._language_of(download.user_id)
        t = translator_for(language)
        reason_key = f"error.{error.code}"
        reason = t(reason_key) if t.has(reason_key) else error.message
        text = t("download.failed", reason=reason)
        if download.status_message_id and download.chat_id:
            try:
                await self._bot.edit_message_text(
                    text=text,
                    chat_id=download.chat_id,
                    message_id=download.status_message_id,
                )
                return
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        await self._safe_send(download.chat_id or download.user_id, text)

    async def _cleanup_status_message(self, download: Download) -> None:
        if not download.status_message_id or not download.chat_id:
            return
        try:
            await self._bot.delete_message(download.chat_id, download.status_message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return

    async def _language_of(self, user_id: int) -> str:
        async with self._uow_factory() as uow:  # type: ignore[operator]
            user = await uow.users.get(user_id)
            return user.language.value if user else "en"

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:  # pragma: no cover
            log.debug("could not notify chat {}: {}", chat_id, exc)


class NotificationSender:
    """Delivers queued notifications with flood-limit awareness."""

    #: Telegram tolerates ~30 messages/second overall; stay well below it.
    MESSAGES_PER_SECOND = 20

    def __init__(self, bot: Bot, uow_factory: object) -> None:
        self._bot = bot
        self._uow_factory = uow_factory

    async def send(self, notification: Notification) -> bool:
        """Send one notification. Returns ``True`` when it reached the user."""
        text = await self._render(notification)
        try:
            await self._bot.send_message(notification.user_id, text)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            return await self.send(notification)
        except TelegramForbiddenError:
            log.debug("notification skipped, user {} blocked the bot", notification.user_id)
            return False
        except TelegramBadRequest as exc:
            log.warning("notification rejected: {}", exc.message)
            return False
        await asyncio.sleep(1 / self.MESSAGES_PER_SECOND)
        return True

    async def _render(self, notification: Notification) -> str:
        """Translate the notification body for the recipient."""
        async with self._uow_factory() as uow:  # type: ignore[operator]
            user = await uow.users.get(notification.user_id)
        t = translator_for(user.language if user else None)
        payload = notification.payload or {}

        match NotificationType(notification.type):
            case NotificationType.DOWNLOAD_READY:
                return f"✅ {notification.body}"
            case NotificationType.DOWNLOAD_FAILED:
                key = f"error.{payload.get('error', '')}"
                reason = t(key) if t.has(key) else notification.body
                return t("download.failed", reason=reason)
            case NotificationType.ACHIEVEMENT_UNLOCKED:
                return t(
                    "achievements.unlocked",
                    icon="🏆",
                    name=str(payload.get("code", "")).replace("_", " ").title(),
                    coins=payload.get("coins", 0),
                )
            case NotificationType.SUBSCRIPTION_EXPIRING:
                return t(
                    "subscription.expiring",
                    tier=str(payload.get("tier", "")).title(),
                    hours=payload.get("hours", 24),
                )
            case NotificationType.SUBSCRIPTION_EXPIRED:
                return t("subscription.expired", tier=str(payload.get("tier", "")).title())
            case _:
                title = (
                    f"<b>{escape_html(notification.title)}</b>\n\n" if notification.title else ""
                )
                return f"{title}{notification.body}"
