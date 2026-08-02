"""Repositories for downloads, the queue, history and favourites."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select, update

from mediabot.domain.enums import DownloadStatus, MediaKind, Platform
from mediabot.infrastructure.db.models.download import (
    Download,
    DownloadHistory,
    DownloadQueueEntry,
    Favorite,
    FavoriteCollection,
)
from mediabot.infrastructure.db.repositories.base import BaseRepository

_ACTIVE_STATUSES = (
    DownloadStatus.DOWNLOADING,
    DownloadStatus.PROCESSING,
    DownloadStatus.UPLOADING,
)


class DownloadRepository(BaseRepository[Download]):
    """Download jobs — the operational heart of the system."""

    model = Download

    async def by_task_id(self, task_id: str) -> Download | None:
        return await self.get_by(task_id=task_id)

    async def find_cached(self, cache_key: str, *, max_age_hours: int = 24) -> Download | None:
        """Find a completed job with the same parameters and a live file id.

        Reusing ``telegram_file_id`` makes a repeated request instant and free:
        Telegram re-sends the stored file without any download at all.
        """
        since = datetime.now(UTC) - timedelta(hours=max_age_hours)
        stmt = (
            select(Download)
            .where(
                Download.cache_key == cache_key,
                Download.status == DownloadStatus.COMPLETED,
                Download.telegram_file_id.is_not(None),
                Download.created_at >= since,
            )
            .order_by(Download.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_today(self, user_id: int, *, since: datetime) -> int:
        """Downloads consumed in the current quota window.

        Cancelled and failed jobs do not count against the quota — users should
        not be punished for our failures.
        """
        stmt = (
            select(func.count())
            .select_from(Download)
            .where(
                Download.user_id == user_id,
                Download.created_at >= since,
                Download.status.notin_([DownloadStatus.FAILED, DownloadStatus.CANCELLED]),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def bytes_today(self, user_id: int, *, since: datetime) -> int:
        stmt = select(func.coalesce(func.sum(Download.file_size), 0)).where(
            Download.user_id == user_id,
            Download.created_at >= since,
            Download.status == DownloadStatus.COMPLETED,
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def count_active(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Download)
            .where(Download.user_id == user_id, Download.status.in_(_ACTIVE_STATUSES))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_running(self) -> int:
        stmt = (
            select(func.count()).select_from(Download).where(Download.status.in_(_ACTIVE_STATUSES))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
        status: DownloadStatus | None = None,
    ) -> Sequence[Download]:
        stmt: Select[tuple[Download]] = select(Download).where(Download.user_id == user_id)
        if status is not None:
            stmt = stmt.where(Download.status == status)
        stmt = stmt.order_by(Download.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def set_status(
        self,
        download_id: int,
        status: DownloadStatus,
        **values: object,
    ) -> None:
        await self.session.execute(
            update(Download).where(Download.id == download_id).values(status=status, **values)
        )

    async def set_progress(self, download_id: int, percent: float) -> None:
        await self.session.execute(
            update(Download).where(Download.id == download_id).values(progress=percent)
        )

    async def stale_active(self, *, older_than_minutes: int) -> Sequence[Download]:
        """Jobs stuck in an active state (worker crashed) — reclaimed by the janitor."""
        threshold = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        stmt = select(Download).where(
            Download.status.in_(_ACTIVE_STATUSES),
            or_(Download.started_at.is_(None), Download.started_at <= threshold),
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def expired_artifacts(self, *, older_than_minutes: int) -> Sequence[Download]:
        """Completed jobs whose local file may be removed."""
        threshold = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        stmt = select(Download).where(
            Download.file_path.is_not(None),
            Download.finished_at.is_not(None),
            Download.finished_at <= threshold,
        )
        return (await self.session.execute(stmt)).scalars().all()

    # -- analytics ------------------------------------------------------- #
    async def count_between(
        self,
        start: datetime,
        end: datetime,
        *,
        kind: MediaKind | None = None,
        status: DownloadStatus | None = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Download)
            .where(Download.created_at >= start, Download.created_at < end)
        )
        if kind is not None:
            stmt = stmt.where(Download.kind == kind)
        if status is not None:
            stmt = stmt.where(Download.status == status)
        return int((await self.session.execute(stmt)).scalar_one())

    async def bytes_between(self, start: datetime, end: datetime) -> int:
        stmt = select(func.coalesce(func.sum(Download.file_size), 0)).where(
            Download.created_at >= start,
            Download.created_at < end,
            Download.status == DownloadStatus.COMPLETED,
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def average_size(self, start: datetime, end: datetime) -> int:
        stmt = select(func.coalesce(func.avg(Download.file_size), 0)).where(
            Download.created_at >= start,
            Download.created_at < end,
            Download.status == DownloadStatus.COMPLETED,
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def average_duration_ms(self, start: datetime, end: datetime) -> int:
        stmt = select(func.coalesce(func.avg(Download.duration_ms), 0)).where(
            Download.created_at >= start,
            Download.created_at < end,
            Download.status == DownloadStatus.COMPLETED,
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def by_platform(self, start: datetime, end: datetime) -> dict[str, int]:
        stmt = (
            select(Download.platform, func.count())
            .where(Download.created_at >= start, Download.created_at < end)
            .group_by(Download.platform)
            .order_by(func.count().desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return {Platform(row[0]).value: int(row[1]) for row in rows}

    async def by_hour(self, start: datetime, end: datetime) -> list[int]:
        """24 counters indexed by UTC hour (portable across PostgreSQL/SQLite)."""
        stmt = select(Download.created_at).where(
            Download.created_at >= start, Download.created_at < end
        )
        buckets = [0] * 24
        for (created_at,) in (await self.session.execute(stmt)).all():
            if created_at is None:
                continue
            moment: datetime = created_at
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            buckets[moment.astimezone(UTC).hour] += 1
        return buckets

    async def daily_counts(self, start: datetime, end: datetime) -> dict[str, int]:
        stmt = select(Download.created_at).where(
            Download.created_at >= start, Download.created_at < end
        )
        counts: dict[str, int] = {}
        for (created_at,) in (await self.session.execute(stmt)).all():
            if created_at is None:
                continue
            key = created_at.date().isoformat()
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def top_errors(self, start: datetime, limit: int = 10) -> Sequence[tuple[str, int]]:
        stmt = (
            select(Download.error_code, func.count())
            .where(
                Download.created_at >= start,
                Download.status == DownloadStatus.FAILED,
                Download.error_code.is_not(None),
            )
            .group_by(Download.error_code)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(str(row[0]), int(row[1])) for row in (await self.session.execute(stmt)).all()]


class QueueRepository(BaseRepository[DownloadQueueEntry]):
    """Persistent download queue."""

    model = DownloadQueueEntry

    async def enqueue(
        self,
        *,
        download_id: int,
        user_id: int,
        priority: int,
        estimated_seconds: int,
    ) -> DownloadQueueEntry:
        return await self.create(
            download_id=download_id,
            user_id=user_id,
            priority=priority,
            estimated_seconds=estimated_seconds,
            enqueued_at=datetime.now(UTC),
        )

    async def pending(self, *, limit: int = 500) -> Sequence[DownloadQueueEntry]:
        stmt = (
            select(DownloadQueueEntry)
            .where(DownloadQueueEntry.started_at.is_(None))
            .order_by(
                DownloadQueueEntry.priority.desc(),
                DownloadQueueEntry.enqueued_at.asc(),
            )
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def for_download(self, download_id: int) -> DownloadQueueEntry | None:
        return await self.get_by(download_id=download_id)

    async def mark_started(self, download_id: int, worker: str) -> None:
        await self.session.execute(
            update(DownloadQueueEntry)
            .where(DownloadQueueEntry.download_id == download_id)
            .values(started_at=datetime.now(UTC), locked_by=worker)
        )

    async def remove(self, download_id: int) -> None:
        await self.session.execute(
            delete(DownloadQueueEntry).where(DownloadQueueEntry.download_id == download_id)
        )

    async def clear(self) -> int:
        """Drop every waiting entry (admin action)."""
        result = await self.session.execute(
            delete(DownloadQueueEntry).where(DownloadQueueEntry.started_at.is_(None))
        )
        return int(result.rowcount or 0)

    async def size(self) -> int:
        stmt = (
            select(func.count())
            .select_from(DownloadQueueEntry)
            .where(DownloadQueueEntry.started_at.is_(None))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def user_queued(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(DownloadQueueEntry)
            .where(
                DownloadQueueEntry.user_id == user_id,
                DownloadQueueEntry.started_at.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())


class HistoryRepository(BaseRepository[DownloadHistory]):
    """Searchable user-facing download history."""

    model = DownloadHistory

    async def search(
        self,
        user_id: int,
        *,
        query: str | None = None,
        kind: MediaKind | None = None,
        platform: Platform | None = None,
        favorites_only: bool = False,
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[DownloadHistory]:
        stmt: Select[tuple[DownloadHistory]] = select(DownloadHistory).where(
            DownloadHistory.user_id == user_id
        )
        if query:
            term = f"%{query.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(DownloadHistory.title).like(term),
                    func.lower(DownloadHistory.uploader).like(term),
                )
            )
        if kind is not None:
            stmt = stmt.where(DownloadHistory.kind == kind)
        if platform is not None:
            stmt = stmt.where(DownloadHistory.platform == platform)
        if favorites_only:
            stmt = stmt.where(DownloadHistory.is_favorite.is_(True))
        stmt = stmt.order_by(DownloadHistory.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count_for_user(
        self,
        user_id: int,
        *,
        query: str | None = None,
        kind: MediaKind | None = None,
        platform: Platform | None = None,
        favorites_only: bool = False,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(DownloadHistory)
            .where(DownloadHistory.user_id == user_id)
        )
        if query:
            term = f"%{query.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(DownloadHistory.title).like(term),
                    func.lower(DownloadHistory.uploader).like(term),
                )
            )
        if kind is not None:
            stmt = stmt.where(DownloadHistory.kind == kind)
        if platform is not None:
            stmt = stmt.where(DownloadHistory.platform == platform)
        if favorites_only:
            stmt = stmt.where(DownloadHistory.is_favorite.is_(True))
        return int((await self.session.execute(stmt)).scalar_one())

    async def prune_older_than(self, user_id: int, *, days: int) -> int:
        threshold = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.execute(
            delete(DownloadHistory).where(
                DownloadHistory.user_id == user_id,
                DownloadHistory.created_at < threshold,
                DownloadHistory.is_favorite.is_(False),
            )
        )
        return int(result.rowcount or 0)


class FavoriteRepository(BaseRepository[Favorite]):
    """Saved links and their collections."""

    model = Favorite

    async def list_for_user(
        self,
        user_id: int,
        *,
        collection_id: int | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[Favorite]:
        stmt: Select[tuple[Favorite]] = select(Favorite).where(Favorite.user_id == user_id)
        if collection_id is not None:
            stmt = stmt.where(Favorite.collection_id == collection_id)
        stmt = stmt.order_by(Favorite.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def by_url(self, user_id: int, url: str) -> Favorite | None:
        return await self.get_by(user_id=user_id, url=url)

    async def collections(self, user_id: int) -> Sequence[FavoriteCollection]:
        stmt = (
            select(FavoriteCollection)
            .where(FavoriteCollection.user_id == user_id)
            .order_by(FavoriteCollection.created_at.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def create_collection(
        self,
        user_id: int,
        name: str,
        icon: str = "📁",
    ) -> FavoriteCollection:
        collection = FavoriteCollection(user_id=user_id, name=name, icon=icon)
        self.session.add(collection)
        await self.session.flush()
        return collection

    async def get_collection(self, user_id: int, collection_id: int) -> FavoriteCollection | None:
        stmt = select(FavoriteCollection).where(
            FavoriteCollection.id == collection_id,
            FavoriteCollection.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def recount_collection(self, collection_id: int) -> None:
        total = await self.count(collection_id=collection_id)
        await self.session.execute(
            update(FavoriteCollection)
            .where(FavoriteCollection.id == collection_id)
            .values(items_count=total)
        )
