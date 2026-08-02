"""Download, history and media-inspection endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from mediabot.core.exceptions import ValidationError
from mediabot.domain.enums import AudioFormat, AudioQuality, MediaKind, VideoFormat
from mediabot.presentation.api.dependencies import AdminDep, ContainerDep
from mediabot.presentation.api.schemas import (
    DownloadItem,
    DownloadRequestSchema,
    DownloadResponse,
    HistoryResponse,
    MediaInfoResponse,
)

router = APIRouter(tags=["downloads"])


@router.post("/downloads", response_model=DownloadResponse)
async def create_download(
    payload: DownloadRequestSchema,
    container: ContainerDep,
    _role: AdminDep,
) -> DownloadResponse:
    """Queue a download on behalf of a user.

    The same admission control as the bot applies: limits, tier restrictions
    and deduplication are enforced by the application layer.
    """
    context = await container.users.build_context(payload.user_id)
    info = await container.media.resolve(payload.url)

    video_format = None
    audio_format = None
    if payload.container:
        try:
            if payload.kind is MediaKind.VIDEO:
                video_format = VideoFormat(payload.container)
            else:
                audio_format = AudioFormat(payload.container)
        except ValueError as exc:
            raise ValidationError(f"Unknown container: {payload.container}") from exc

    ticket = await container.downloads.request(
        context=context,
        info=info,
        kind=payload.kind,
        video_quality=payload.video_quality if payload.kind is MediaKind.VIDEO else None,
        audio_quality=(
            payload.audio_quality or AudioQuality.KBPS_192
            if payload.kind is MediaKind.AUDIO
            else None
        ),
        video_format=video_format,
        audio_format=audio_format,
        chat_id=payload.user_id,
    )
    return DownloadResponse(
        download_id=ticket.download_id,
        status=ticket.status.value,
        queued=ticket.queued,
        position=ticket.position,
        estimated_wait_seconds=ticket.estimated_wait_seconds,
        cached=bool(ticket.cached_file_id),
    )


@router.get("/downloads/{download_id}", response_model=DownloadItem)
async def get_download(
    download_id: int,
    container: ContainerDep,
    _role: AdminDep,
) -> DownloadItem:
    async with container.uow_factory() as uow:
        download = await uow.downloads.get(download_id)
    if download is None:
        from mediabot.core.exceptions import NotFoundError

        raise NotFoundError("Download not found")
    return DownloadItem(
        id=download.id,
        url=download.url,
        title=download.title or "",
        platform=download.platform.value,
        kind=download.kind,
        status=download.status.value,
        quality=download.video_quality or download.audio_quality,
        file_size=download.file_size or 0,
        duration_seconds=download.duration_seconds,
        created_at=download.created_at,
    )


@router.delete("/downloads/{download_id}")
async def cancel_download(
    download_id: int,
    container: ContainerDep,
    _role: AdminDep,
) -> dict[str, bool]:
    cancelled = await container.downloads.cancel(download_id)
    return {"cancelled": cancelled}


@router.get("/users/{user_id}/history", response_model=HistoryResponse)
async def user_history(
    user_id: int,
    container: ContainerDep,
    _role: AdminDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    query: Annotated[str | None, Query(max_length=64)] = None,
    kind: MediaKind | None = None,
) -> HistoryResponse:
    """Paginated, searchable download history of a user."""
    views, total = await container.history.list_page(
        user_id, query=query, kind=kind, page=page, page_size=page_size
    )
    return HistoryResponse(
        items=[
            DownloadItem(
                id=view.id,
                url=view.url,
                title=view.title,
                platform=view.platform.value,
                kind=view.kind,
                status=view.status.value,
                quality=view.quality,
                file_size=view.file_size,
                duration_seconds=view.duration_seconds,
                created_at=view.created_at,
            )
            for view in views
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/media/info", response_model=MediaInfoResponse)
async def media_info(
    container: ContainerDep,
    _role: AdminDep,
    url: Annotated[str, Query(min_length=8, max_length=2048)],
) -> MediaInfoResponse:
    """Inspect a link without downloading anything."""
    info = await container.media.resolve(url)
    qualities = [quality.value for quality in info.available_video_qualities()]
    return MediaInfoResponse(
        url=info.webpage_url or info.source_url,
        platform=info.platform.value,
        title=info.title,
        uploader=info.uploader,
        duration=info.duration,
        thumbnail=info.thumbnail,
        is_live=info.is_live,
        qualities=qualities,
        estimated_sizes={
            quality.value: info.estimated_size(quality)
            for quality in info.available_video_qualities()
        },
    )
