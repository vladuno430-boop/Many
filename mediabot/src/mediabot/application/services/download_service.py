"""Download orchestration: admission control, queueing and job execution."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mediabot.application.dto import DownloadTicket, UserContext
from mediabot.core.config import Settings
from mediabot.core.exceptions import (
    DownloadError,
    MediaBotError,
    NotFoundError,
    QueueFullError,
    ValidationError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import (
    AudioFormat,
    AudioQuality,
    DownloadStatus,
    MediaKind,
    NotificationType,
    Platform,
    QueuePriority,
    VideoFormat,
    VideoQuality,
)
from mediabot.domain.services.format_selection import FormatSelector
from mediabot.domain.services.limits import LimitService
from mediabot.domain.services.queueing import QueueEntry, QueueService
from mediabot.domain.value_objects import (
    DownloadArtifact,
    DownloadProgress,
    DownloadRequest,
    MediaInfo,
    QueuePosition,
)
from mediabot.infrastructure.cache.redis_cache import CacheService
from mediabot.infrastructure.db.models.download import Download
from mediabot.infrastructure.db.session import UnitOfWork, UnitOfWorkFactory
from mediabot.infrastructure.downloader.ffmpeg import FFmpegService, validate_media_file
from mediabot.infrastructure.downloader.ytdlp_adapter import YtDlpAdapter
from mediabot.infrastructure.metrics import (
    BYTES_TRANSFERRED,
    DOWNLOAD_DURATION,
    DOWNLOADS_TOTAL,
    ERRORS_TOTAL,
)
from mediabot.infrastructure.queue.dispatcher import TaskDispatcher
from mediabot.infrastructure.storage import StorageService

log = get_logger(LogChannel.DOWNLOAD, component="download_service")


@runtime_checkable
class ProgressSink(Protocol):
    """Anything the downloader can publish progress snapshots to.

    Distributed mode writes to Redis, standalone keeps them in memory; the
    service only needs this one method.
    """

    def publish(self, download_id: int, progress: DownloadProgress) -> None:
        """Record the latest progress of a job."""
        ...

    def close(self) -> None:
        """Release whatever resources the sink holds."""
        ...


@runtime_checkable
class MediaDelivery(Protocol):
    """Transport that hands the finished file to the user."""

    async def deliver(self, download: Download) -> str | None:
        """Send the artefact and return the reusable Telegram ``file_id``."""
        ...

    async def report_progress(self, download: Download, progress: DownloadProgress) -> None:
        """Update the status message shown to the user (best effort)."""
        ...

    async def report_failure(self, download: Download, error: MediaBotError) -> None:
        """Tell the user the job failed."""
        ...


class DownloadService:
    """The write side of the download flow.

    Two entry points:

    * :meth:`request` — called from the bot/API, performs *admission control*
      (limits, dedupe, capacity) and either dispatches the job or queues it;
    * :meth:`execute` — called from a Celery worker, performs the actual work.

    Splitting them keeps the user-facing path fast (no blocking IO) and lets the
    worker be scaled independently.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        adapter: YtDlpAdapter,
        ffmpeg: FFmpegService,
        storage: StorageService,
        cache: CacheService,
        dispatcher: TaskDispatcher,
        settings: Settings,
        limit_service: LimitService,
        queue_service: QueueService,
        format_selector: FormatSelector,
        delivery: MediaDelivery | None = None,
        progress_publisher: ProgressSink | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._adapter = adapter
        self._ffmpeg = ffmpeg
        self._storage = storage
        self._cache = cache
        self._dispatcher = dispatcher
        self._settings = settings
        self._limits = limit_service
        self._queue = queue_service
        self._formats = format_selector
        self._delivery = delivery
        self._progress = progress_publisher

    # ------------------------------------------------------------------ #
    # Admission control
    # ------------------------------------------------------------------ #
    async def request(
        self,
        *,
        context: UserContext,
        info: MediaInfo,
        kind: MediaKind,
        video_quality: VideoQuality | None = None,
        audio_quality: AudioQuality | None = None,
        video_format: VideoFormat | None = None,
        audio_format: AudioFormat | None = None,
        chat_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> DownloadTicket:
        """Validate a request, deduplicate it and queue the job."""
        platform = info.platform
        if platform.metadata_only:
            raise ValidationError(
                "This platform is protected by DRM; only metadata can be provided",
                platform=platform.value,
            )

        estimated_size = (
            info.estimated_size(video_quality or VideoQuality.P720)
            if kind is MediaKind.VIDEO
            else info.estimated_size(VideoQuality.AUDIO_ONLY)
        )
        decision = self._limits.check_request(
            context.policy,
            context.usage,
            platform=platform,
            kind=kind,
            duration_seconds=info.duration,
            estimated_size=estimated_size,
            video_quality=video_quality,
            audio_quality=audio_quality,
        )
        decision.raise_for_status()

        request = DownloadRequest(
            user_id=context.user_id,
            url=info.webpage_url or info.source_url,
            platform=platform,
            kind=kind,
            video_quality=video_quality if kind is MediaKind.VIDEO else None,
            audio_quality=audio_quality if kind is MediaKind.AUDIO else None,
            video_format=video_format,
            audio_format=audio_format,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
        )
        cache_key = request.cache_key()

        async with self._uow_factory() as uow:
            # 1) Instant path: the exact same artefact already lives on Telegram.
            cached = await uow.downloads.find_cached(cache_key)
            if cached is not None and cached.telegram_file_id:
                download = await self._create_row(
                    uow,
                    request,
                    info,
                    cache_key,
                    status=DownloadStatus.COMPLETED,
                    telegram_file_id=cached.telegram_file_id,
                    file_size=cached.file_size,
                    file_name=cached.file_name,
                )
                await uow.commit()
                DOWNLOADS_TOTAL.labels(platform.value, kind.value, "cached").inc()
                return DownloadTicket(
                    download_id=download.id,
                    status=DownloadStatus.COMPLETED,
                    queued=False,
                    cached_file_id=cached.telegram_file_id,
                )

            # 2) Normal path: create the job row.
            download = await self._create_row(uow, request, info, cache_key)

            running = await uow.downloads.count_running()
            capacity = self._settings.downloader.max_concurrent_jobs
            estimated_seconds = self._queue.estimate_job_seconds(
                duration_seconds=info.duration,
                size_bytes=estimated_size,
                needs_conversion=self._formats.build(request).needs_conversion,
            )
            priority = int(self._queue.priority_for(context.tier))

            queue_size = await uow.queue.size()
            if queue_size >= 1000:
                await uow.rollback()
                raise QueueFullError()

            must_queue = self._queue.should_queue(running_jobs=running, capacity=capacity)
            await uow.queue.enqueue(
                download_id=download.id,
                user_id=context.user_id,
                priority=priority,
                estimated_seconds=estimated_seconds,
            )
            download.status = DownloadStatus.QUEUED
            await uow.commit()

            position: QueuePosition | None = None
            if must_queue:
                position = await self._position(uow, download.id, capacity, running)

        task_id = self._dispatcher.dispatch_download(download.id, priority=priority)
        async with self._uow_factory.transaction() as uow:
            await uow.downloads.update_by_id(download.id, task_id=task_id)

        DOWNLOADS_TOTAL.labels(platform.value, kind.value, "queued").inc()
        return DownloadTicket(
            download_id=download.id,
            status=DownloadStatus.QUEUED,
            queued=must_queue,
            position=position.position if position else None,
            estimated_wait_seconds=position.estimated_wait_seconds if position else None,
            task_id=task_id,
        )

    async def _create_row(
        self,
        uow: UnitOfWork,
        request: DownloadRequest,
        info: MediaInfo,
        cache_key: str,
        *,
        status: DownloadStatus = DownloadStatus.PENDING,
        **extra: Any,
    ) -> Download:
        return await uow.downloads.create(
            user_id=request.user_id,
            url=request.url,
            source_id=info.raw_id,
            platform=request.platform,
            kind=request.kind,
            status=status,
            video_quality=request.video_quality.value if request.video_quality else None,
            audio_quality=request.audio_quality.value if request.audio_quality else None,
            target_format=request.target_extension,
            cache_key=cache_key,
            title=info.title[:512],
            uploader=(info.uploader or "")[:255] or None,
            thumbnail_url=info.thumbnail,
            duration_seconds=info.duration,
            chat_id=request.chat_id,
            reply_to_message_id=request.reply_to_message_id,
            **extra,
        )

    async def _position(
        self,
        uow: UnitOfWork,
        download_id: int,
        capacity: int,
        running: int,
    ) -> QueuePosition | None:
        entries = [
            QueueEntry(
                job_id=entry.download_id,
                user_id=entry.user_id,
                priority=QueuePriority(entry.priority),
                enqueued_at=entry.enqueued_at,
                estimated_seconds=entry.estimated_seconds,
            )
            for entry in await uow.queue.pending()
        ]
        return self._queue.position_of(
            entries,
            download_id,
            workers=capacity,
            running_jobs=running,
        )

    async def queue_position(self, download_id: int) -> QueuePosition | None:
        """Public helper used by the "where am I?" button."""
        async with self._uow_factory() as uow:
            running = await uow.downloads.count_running()
            return await self._position(
                uow,
                download_id,
                self._settings.downloader.max_concurrent_jobs,
                running,
            )

    async def cancel(self, download_id: int, *, user_id: int | None = None) -> bool:
        """Cancel a queued or running job."""
        async with self._uow_factory.transaction() as uow:
            download = await uow.downloads.get(download_id)
            if download is None:
                raise NotFoundError("Download not found")
            if user_id is not None and download.user_id != user_id:
                raise NotFoundError("Download not found")
            if DownloadStatus(download.status).is_terminal:
                return False
            if download.task_id:
                self._dispatcher.revoke(download.task_id)
            download.status = DownloadStatus.CANCELLED
            download.finished_at = datetime.now(UTC)
            await uow.queue.remove(download_id)
        self._storage.remove_job_dir(download_id)
        return True

    # ------------------------------------------------------------------ #
    # Execution (worker side)
    # ------------------------------------------------------------------ #
    async def execute(self, download_id: int, *, worker_name: str = "worker") -> None:
        """Run a queued job end to end.

        Every failure path updates the row, records metrics and notifies the
        user — a job never disappears silently.
        """
        started = time.perf_counter()
        async with self._uow_factory() as uow:
            download = await uow.downloads.get(download_id)
            if download is None:
                raise NotFoundError(f"Download {download_id} not found")
            if DownloadStatus(download.status).is_terminal:
                log.info("skipping terminal download={} status={}", download_id, download.status)
                return
            download.status = DownloadStatus.DOWNLOADING
            download.started_at = datetime.now(UTC)
            download.worker_name = worker_name
            download.attempts += 1
            await uow.queue.mark_started(download_id, worker_name)
            await uow.commit()
            snapshot = download

        try:
            artifact = await self._perform(snapshot)
            await self._finalize_success(snapshot, artifact, started)
        except MediaBotError as exc:
            await self._finalize_failure(snapshot, exc)
            raise
        except Exception as exc:  # pragma: no cover - unexpected crash
            await self._finalize_failure(snapshot, DownloadError(str(exc)))
            raise

    async def _perform(self, download: Download) -> DownloadArtifact:
        """Download, post-process and validate the media file."""
        info = await self._adapter.extract_info(download.url)
        request = self._request_from_row(download)
        plan = self._formats.build(request)

        policy_limit = self._settings.downloader.absolute_max_filesize_bytes
        job_dir = self._storage.job_dir(download.id)

        loop_state: dict[str, float] = {"last": 0.0}

        def on_progress(progress: DownloadProgress) -> None:
            # Called from the yt-dlp thread: only cheap, non-async work here.
            now = time.monotonic()
            if now - loop_state["last"] < 2.0 and progress.percent < 100:
                return
            loop_state["last"] = now
            self._progress_sink(download.id, progress)

        artifact = await self._adapter.download(
            url=download.url,
            plan=plan,
            destination=job_dir,
            info=info,
            progress_callback=on_progress,
            speed_limit=None,
            max_filesize=policy_limit,
        )
        validate_media_file(artifact.file_path, max_bytes=policy_limit)

        if artifact.kind is MediaKind.VIDEO and not artifact.thumbnail_path:
            thumbnail = await self._ffmpeg.make_thumbnail(
                artifact.file_path, Path(job_dir) / "thumb.jpg"
            )
            if thumbnail is not None:
                artifact = DownloadArtifact(
                    file_path=artifact.file_path,
                    file_name=artifact.file_name,
                    file_size=artifact.file_size,
                    mime_type=artifact.mime_type,
                    kind=artifact.kind,
                    duration=artifact.duration,
                    width=artifact.width,
                    height=artifact.height,
                    thumbnail_path=str(thumbnail),
                )
        return artifact

    def _progress_sink(self, download_id: int, progress: DownloadProgress) -> None:
        """Publish progress into Redis from yt-dlp's (synchronous) thread.

        The bot polls this key to refresh the status message, which avoids
        cross-process callbacks and keeps the worker free of Telegram calls.
        """
        if self._progress is not None:
            self._progress.publish(download_id, progress)

    @staticmethod
    def _request_from_row(download: Download) -> DownloadRequest:
        kind = MediaKind(download.kind)
        video_quality = (
            VideoQuality(download.video_quality)
            if download.video_quality
            else (VideoQuality.P720 if kind is MediaKind.VIDEO else None)
        )
        audio_quality = (
            AudioQuality(download.audio_quality)
            if download.audio_quality
            else (AudioQuality.KBPS_192 if kind is MediaKind.AUDIO else None)
        )
        target = download.target_format
        return DownloadRequest(
            user_id=download.user_id,
            url=download.url,
            platform=Platform(download.platform),
            kind=kind,
            video_quality=video_quality,
            audio_quality=audio_quality,
            video_format=VideoFormat(target) if kind is MediaKind.VIDEO else None,
            audio_format=AudioFormat(target) if kind is MediaKind.AUDIO else None,
            chat_id=download.chat_id,
            reply_to_message_id=download.reply_to_message_id,
        )

    async def _finalize_success(
        self,
        snapshot: Download,
        artifact: DownloadArtifact,
        started: float,
    ) -> None:
        """Persist the artefact, deliver it and update every counter."""
        async with self._uow_factory.transaction() as uow:
            download = await uow.downloads.get(snapshot.id)
            if download is None:  # pragma: no cover - deleted mid-flight
                return
            download.status = DownloadStatus.UPLOADING
            download.file_path = artifact.file_path
            download.file_name = artifact.file_name
            download.file_size = artifact.file_size
            download.mime_type = artifact.mime_type
            download.progress = 100.0
            download.extra = {
                **(download.extra or {}),
                "thumbnail_path": artifact.thumbnail_path,
                "width": artifact.width,
                "height": artifact.height,
            }

        file_id: str | None = None
        if self._delivery is not None:
            async with self._uow_factory() as uow:
                fresh = await uow.downloads.get(snapshot.id)
            if fresh is not None:
                file_id = await self._delivery.deliver(fresh)

        duration_ms = int((time.perf_counter() - started) * 1000)
        async with self._uow_factory.transaction() as uow:
            download = await uow.downloads.get(snapshot.id)
            if download is None:  # pragma: no cover
                return
            download.status = DownloadStatus.COMPLETED
            download.telegram_file_id = file_id
            download.finished_at = datetime.now(UTC)
            download.duration_ms = duration_ms
            await uow.queue.remove(download.id)

            kind = MediaKind(download.kind)
            await uow.users.increment_counters(
                download.user_id,
                downloads=1,
                video_downloads=1 if kind is MediaKind.VIDEO else 0,
                audio_downloads=1 if kind is MediaKind.AUDIO else 0,
                size_bytes=download.file_size or 0,
                media_seconds=download.duration_seconds or 0,
            )
            await uow.history.create(
                user_id=download.user_id,
                download_id=download.id,
                url=download.url,
                platform=Platform(download.platform),
                kind=kind,
                title=(download.title or "")[:512],
                uploader=download.uploader,
                quality=download.video_quality or download.audio_quality,
                file_format=download.target_format,
                file_size=download.file_size or 0,
                duration_seconds=download.duration_seconds,
                telegram_file_id=file_id,
            )
            await uow.notifications.enqueue(
                user_id=download.user_id,
                notification_type=NotificationType.DOWNLOAD_READY,
                body=download.title or download.url,
                payload={"download_id": download.id},
            )

        DOWNLOADS_TOTAL.labels(
            str(snapshot.platform), str(snapshot.kind), DownloadStatus.COMPLETED.value
        ).inc()
        BYTES_TRANSFERRED.labels(str(snapshot.kind)).inc(artifact.file_size)
        DOWNLOAD_DURATION.labels(str(snapshot.kind)).observe(duration_ms / 1000)

        # The local copy is no longer needed once Telegram holds the file.
        if file_id:
            self._storage.remove_job_dir(snapshot.id)
            async with self._uow_factory.transaction() as uow:
                await uow.downloads.update_by_id(snapshot.id, file_path=None)

    async def _finalize_failure(self, snapshot: Download, error: MediaBotError) -> None:
        log.error(
            "download failed id={} url={} code={} error={}",
            snapshot.id,
            snapshot.url,
            error.code,
            error.message,
        )
        async with self._uow_factory.transaction() as uow:
            download = await uow.downloads.get(snapshot.id)
            if download is None:  # pragma: no cover
                return
            download.status = DownloadStatus.FAILED
            download.error_code = error.code
            download.error_message = error.message[:1000]
            download.finished_at = datetime.now(UTC)
            await uow.queue.remove(download.id)
            await uow.errors.record(
                code=error.code,
                message=error.message,
                component="worker",
                user_id=download.user_id,
                download_id=download.id,
                url=download.url,
            )
            await uow.notifications.enqueue(
                user_id=download.user_id,
                notification_type=NotificationType.DOWNLOAD_FAILED,
                body=error.code,
                payload={"download_id": download.id, "error": error.code},
            )
        ERRORS_TOTAL.labels(error.code, "worker").inc()
        DOWNLOADS_TOTAL.labels(
            str(snapshot.platform), str(snapshot.kind), DownloadStatus.FAILED.value
        ).inc()
        self._storage.remove_job_dir(snapshot.id)
        if self._delivery is not None:
            async with self._uow_factory() as uow:
                fresh = await uow.downloads.get(snapshot.id)
            if fresh is not None:
                await self._delivery.report_failure(fresh, error)

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #
    async def reclaim_stale(self, *, older_than_minutes: int = 60) -> int:
        """Fail jobs abandoned by a crashed worker so users are not left waiting."""
        async with self._uow_factory.transaction() as uow:
            stale = await uow.downloads.stale_active(older_than_minutes=older_than_minutes)
            for download in stale:
                download.status = DownloadStatus.FAILED
                download.error_code = "worker_lost"
                download.error_message = "Worker did not finish the job"
                download.finished_at = datetime.now(UTC)
                await uow.queue.remove(download.id)
            count = len(stale)
        if count:
            log.warning("reclaimed {} stale downloads", count)
        return count

    async def cleanup_artifacts(self) -> tuple[int, int]:
        """Delete artefacts older than the configured TTL."""
        ttl = self._settings.app.artifact_ttl_minutes
        async with self._uow_factory.transaction() as uow:
            expired = await uow.downloads.expired_artifacts(older_than_minutes=ttl)
            for download in expired:
                self._storage.remove_file(download.file_path)
                download.file_path = None
        return self._storage.cleanup_older_than(ttl)
