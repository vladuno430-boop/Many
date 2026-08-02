"""The core flow: link → preview → format → quality → queued job."""

from __future__ import annotations

import secrets
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from mediabot.application.dto import MediaPreview, UserContext
from mediabot.core.container import Container
from mediabot.core.exceptions import (
    MediaBotError,
    UnsafeUrlError,
    UnsupportedUrlError,
    ValidationError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import extract_urls
from mediabot.domain.enums import (
    AudioFormat,
    AudioQuality,
    MediaKind,
    VideoFormat,
    VideoQuality,
)
from mediabot.domain.value_objects import DownloadProgress, humanize_bytes, humanize_duration
from mediabot.presentation.bot.callbacks import DownloadActionCallback, DownloadCallback
from mediabot.presentation.bot.formatters import render_media_info, render_progress
from mediabot.presentation.bot.i18n.translator import Translator
from mediabot.presentation.bot.keyboards import (
    audio_quality_choice,
    download_progress_keyboard,
    format_choice,
    video_quality_choice,
)
from mediabot.presentation.bot.states import DownloadStates

router = Router(name="download")
log = get_logger(LogChannel.DOWNLOAD, component="handlers.download")

#: FSM key holding the resolved previews of the current chat.
PREVIEW_KEY = "previews"


@router.message(F.text.regexp(r"https?://\S+"))
async def on_link(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Resolve the first URL in the message and offer the download options."""
    urls = extract_urls(message.text or "")
    if not urls:
        return

    status = await message.answer(t("link.analyzing"))
    try:
        preview = await container.media.build_preview(urls[0], ctx)
    except (UnsupportedUrlError, UnsafeUrlError) as exc:
        await status.edit_text(
            t("link.unsafe") if isinstance(exc, UnsafeUrlError) else t("link.not_supported")
        )
        return
    except MediaBotError as exc:
        await status.edit_text(f"⚠️ {exc.message}")
        return

    token = secrets.token_urlsafe(8)[:10]
    await _store_preview(state, token, preview)

    text = render_media_info(preview, t)
    if preview.metadata_only:
        await status.edit_text(text)
        return

    if container.settings.app.debug or not _auto_download_enabled(ctx):
        await status.edit_text(
            f"{text}\n\n{t('link.choose_format')}",
            reply_markup=format_choice(preview, token, t),
        )
        await state.set_state(DownloadStates.choosing_format)
        return

    # Auto-download: honour the user's saved defaults without extra taps.
    await status.edit_text(text)
    await _start_download(
        message=status,
        preview=preview,
        kind=container.media.default_kind(preview.info),
        quality=None,
        container_format=None,
        ctx=ctx,
        t=t,
        app=container,
    )


@router.callback_query(DownloadCallback.filter(F.quality == "menu"))
async def on_format_selected(
    callback: CallbackQuery,
    callback_data: DownloadCallback,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Show the quality keyboard for the chosen media kind."""
    preview = await _load_preview(state, callback_data.token, container, ctx)
    if preview is None or not isinstance(callback.message, Message):
        await callback.answer(t("common.error"), show_alert=True)
        return

    if callback_data.kind is MediaKind.VIDEO:
        markup = video_quality_choice(preview, callback_data.token, t)
    else:
        markup = audio_quality_choice(preview, callback_data.token, t)
    await callback.message.edit_text(
        f"{render_media_info(preview, t)}\n\n{t('link.choose_quality')}",
        reply_markup=markup,
    )
    await state.set_state(DownloadStates.choosing_quality)
    await callback.answer()


@router.callback_query(DownloadCallback.filter(F.quality == "back"))
async def on_back_to_format(
    callback: CallbackQuery,
    callback_data: DownloadCallback,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    preview = await _load_preview(state, callback_data.token, container, ctx)
    if preview is None or not isinstance(callback.message, Message):
        await callback.answer(t("common.error"), show_alert=True)
        return
    await callback.message.edit_text(
        f"{render_media_info(preview, t)}\n\n{t('link.choose_format')}",
        reply_markup=format_choice(preview, callback_data.token, t),
    )
    await state.set_state(DownloadStates.choosing_format)
    await callback.answer()


@router.callback_query(DownloadCallback.filter())
async def on_quality_selected(
    callback: CallbackQuery,
    callback_data: DownloadCallback,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Admit the request and queue the job."""
    preview = await _load_preview(state, callback_data.token, container, ctx)
    if preview is None or not isinstance(callback.message, Message):
        await callback.answer(t("common.error"), show_alert=True)
        return
    await callback.answer()
    await _start_download(
        message=callback.message,
        preview=preview,
        kind=callback_data.kind,
        quality=callback_data.quality,
        container_format=callback_data.container or None,
        ctx=ctx,
        t=t,
        app=container,
    )
    await state.set_state(None)


async def _start_download(
    *,
    message: Message,
    preview: MediaPreview,
    kind: MediaKind,
    quality: str | None,
    container_format: str | None,
    ctx: UserContext,
    t: Translator,
    app: Container,
) -> None:
    """Shared tail of the manual and automatic download paths."""
    video_quality: VideoQuality | None = None
    audio_quality: AudioQuality | None = None
    video_format: VideoFormat | None = None
    audio_format: AudioFormat | None = None

    try:
        if kind is MediaKind.VIDEO:
            video_quality = VideoQuality(quality) if quality else VideoQuality.P720
            video_format = VideoFormat(container_format) if container_format else VideoFormat.MP4
        else:
            audio_quality = AudioQuality(quality) if quality else AudioQuality.KBPS_192
            audio_format = AudioFormat(container_format) if container_format else AudioFormat.MP3
    except ValueError as exc:
        raise ValidationError("Unknown quality or format") from exc

    ticket = await app.downloads.request(
        context=ctx,
        info=preview.info,
        kind=kind,
        video_quality=video_quality,
        audio_quality=audio_quality,
        video_format=video_format,
        audio_format=audio_format,
        chat_id=message.chat.id,
        reply_to_message_id=message.message_id,
    )

    if ticket.cached_file_id:
        await _send_cached(message, ticket.cached_file_id, kind, preview, t, app)
        return

    if ticket.queued and ticket.position:
        text = t(
            "download.queued",
            position=ticket.position,
            wait=humanize_duration(ticket.estimated_wait_seconds or 0),
        )
    else:
        text = t("download.started")

    status = await message.answer(
        text, reply_markup=download_progress_keyboard(ticket.download_id, t)
    )
    # The worker edits this message as the job progresses.
    async with app.uow_factory.transaction() as uow:
        await uow.downloads.update_by_id(
            ticket.download_id,
            status_message_id=status.message_id,
            chat_id=status.chat.id,
        )


async def _send_cached(
    message: Message,
    file_id: str,
    kind: MediaKind,
    preview: MediaPreview,
    t: Translator,
    app: Container,
) -> None:
    """Instant re-send of an artefact Telegram already stores."""
    caption = t(
        "download.ready",
        title=preview.info.title[:120],
        size=humanize_bytes(preview.info.best_filesize),
        quality="cache",
        bot=app.settings.telegram.bot_username,
    )
    if kind is MediaKind.VIDEO:
        await message.answer_video(file_id, caption=caption)
    else:
        await message.answer_audio(file_id, caption=caption)
    await message.answer(t("download.cached"))


@router.callback_query(DownloadActionCallback.filter(F.action == "cancel"))
async def on_cancel(
    callback: CallbackQuery,
    callback_data: DownloadActionCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    cancelled = await container.downloads.cancel(callback_data.download_id, user_id=ctx.user_id)
    await callback.answer(t("download.cancelled") if cancelled else t("common.done"))
    if cancelled and isinstance(callback.message, Message):
        await callback.message.edit_text(t("download.cancelled"))


@router.callback_query(DownloadActionCallback.filter(F.action == "refresh"))
async def on_refresh(
    callback: CallbackQuery,
    callback_data: DownloadActionCallback,
    t: Translator,
    container: Container,
) -> None:
    """Refresh the progress message from the Redis snapshot."""
    payload = await container.progress_snapshot(callback_data.download_id)
    if not payload:
        await callback.answer(t("common.loading"))
        return
    progress = DownloadProgress(
        percent=float(payload.get("percent") or 0),
        downloaded_bytes=int(payload.get("downloaded") or 0),
        total_bytes=payload.get("total"),
        speed_bytes_per_sec=payload.get("speed"),
        eta_seconds=payload.get("eta"),
        stage=str(payload.get("stage") or "downloading"),
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            render_progress(progress, t),
            reply_markup=download_progress_keyboard(callback_data.download_id, t),
        )
    await callback.answer()


@router.callback_query(DownloadActionCallback.filter(F.action == "favorite"))
async def on_favorite(
    callback: CallbackQuery,
    callback_data: DownloadActionCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    async with container.uow_factory() as uow:
        download = await uow.downloads.get(callback_data.download_id)
    if download is None or download.user_id != ctx.user_id:
        await callback.answer(t("common.error"), show_alert=True)
        return
    try:
        await container.favorites.add(
            ctx.user_id,
            url=download.url,
            title=download.title or "",
            platform=download.platform,
            thumbnail_url=download.thumbnail_url,
        )
        await callback.answer(t("favorites.added"))
    except MediaBotError:
        await callback.answer(t("favorites.exists"), show_alert=True)


@router.callback_query(DownloadActionCallback.filter(F.action == "repeat"))
async def on_repeat(
    callback: CallbackQuery,
    callback_data: DownloadActionCallback,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Re-run a previous job with the same URL."""
    async with container.uow_factory() as uow:
        download = await uow.downloads.get(callback_data.download_id)
    if download is None or download.user_id != ctx.user_id:
        await callback.answer(t("common.error"), show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    preview = await container.media.build_preview(download.url, ctx)
    token = secrets.token_urlsafe(8)[:10]
    await _store_preview(state, token, preview)
    await callback.message.answer(
        f"{render_media_info(preview, t)}\n\n{t('link.choose_format')}",
        reply_markup=format_choice(preview, token, t),
    )
    await callback.answer()


# --------------------------------------------------------------------------- #
# FSM helpers
# --------------------------------------------------------------------------- #
async def _store_preview(state: FSMContext, token: str, preview: MediaPreview) -> None:
    """Keep the resolved preview in the FSM, capped to the last few links."""
    data = await state.get_data()
    previews: dict[str, Any] = dict(data.get(PREVIEW_KEY) or {})
    previews[token] = {
        "url": preview.info.webpage_url or preview.info.source_url,
        "warning": preview.warning,
    }
    # Only the five most recent links stay addressable — keeps the FSM small.
    for stale in list(previews)[:-5]:
        previews.pop(stale, None)
    await state.update_data({PREVIEW_KEY: previews})


async def _load_preview(
    state: FSMContext,
    token: str,
    container: Container,
    ctx: UserContext,
) -> MediaPreview | None:
    """Rebuild a preview from its stored URL.

    Only the URL is kept in the FSM; the (much larger) metadata is re-read from
    the Redis media cache, so an expired token degrades into one cheap
    extraction instead of a stale, oversized FSM payload.
    """
    data = await state.get_data()
    entry = (data.get(PREVIEW_KEY) or {}).get(token)
    if not entry:
        return None
    return await container.media.build_preview(entry["url"], ctx)


def _auto_download_enabled(ctx: UserContext) -> bool:
    """Auto-download is opt-in and currently disabled by default."""
    return False
