"""History, favourites and the notification outbox."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mediabot.application.dto import BroadcastResult, DownloadView
from mediabot.core.exceptions import ConflictError, NotFoundError, ValidationError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import (
    DownloadStatus,
    Language,
    MediaKind,
    NotificationType,
    Platform,
    SubscriptionTier,
)
from mediabot.infrastructure.db.models.download import Favorite, FavoriteCollection
from mediabot.infrastructure.db.models.system import Notification
from mediabot.infrastructure.db.session import UnitOfWorkFactory
from mediabot.infrastructure.queue.dispatcher import TaskDispatcher

log = get_logger(LogChannel.APP, component="engagement")

MAX_FAVORITES_PER_USER = 500
MAX_COLLECTIONS_PER_USER = 25


class HistoryService:
    """Searchable download history with re-download support."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def list_page(
        self,
        user_id: int,
        *,
        query: str | None = None,
        kind: MediaKind | None = None,
        platform: Platform | None = None,
        favorites_only: bool = False,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[list[DownloadView], int]:
        """Return one page of history plus the total number of matches."""
        page = max(page, 1)
        offset = (page - 1) * page_size
        async with self._uow_factory() as uow:
            rows = await uow.history.search(
                user_id,
                query=query,
                kind=kind,
                platform=platform,
                favorites_only=favorites_only,
                limit=page_size,
                offset=offset,
            )
            total = await uow.history.count_for_user(
                user_id,
                query=query,
                kind=kind,
                platform=platform,
                favorites_only=favorites_only,
            )
        views = [
            DownloadView(
                id=row.id,
                url=row.url,
                title=row.title,
                platform=Platform(row.platform),
                kind=MediaKind(row.kind),
                status=DownloadStatus.COMPLETED,
                quality=row.quality,
                file_format=row.file_format,
                file_size=row.file_size,
                duration_seconds=row.duration_seconds,
                created_at=row.created_at,
                telegram_file_id=row.telegram_file_id,
                is_favorite=row.is_favorite,
            )
            for row in rows
        ]
        return views, total

    async def get(self, user_id: int, history_id: int) -> DownloadView:
        async with self._uow_factory() as uow:
            row = await uow.history.get(history_id)
            if row is None or row.user_id != user_id:
                raise NotFoundError("History entry not found")
            return DownloadView(
                id=row.id,
                url=row.url,
                title=row.title,
                platform=Platform(row.platform),
                kind=MediaKind(row.kind),
                status=DownloadStatus.COMPLETED,
                quality=row.quality,
                file_format=row.file_format,
                file_size=row.file_size,
                duration_seconds=row.duration_seconds,
                created_at=row.created_at,
                telegram_file_id=row.telegram_file_id,
                is_favorite=row.is_favorite,
            )

    async def mark_repeat(self, history_id: int) -> None:
        async with self._uow_factory.transaction() as uow:
            row = await uow.history.get(history_id)
            if row is not None:
                row.repeat_count += 1

    async def toggle_favorite(self, user_id: int, history_id: int) -> bool:
        """Flip the favourite flag of a history entry; returns the new state."""
        async with self._uow_factory.transaction() as uow:
            row = await uow.history.get(history_id)
            if row is None or row.user_id != user_id:
                raise NotFoundError("History entry not found")
            row.is_favorite = not row.is_favorite
            return row.is_favorite

    async def prune(self, user_id: int, *, tier: SubscriptionTier) -> int:
        """Free tier keeps a week of history; paid tiers keep everything."""
        if tier.is_paid:
            return 0
        async with self._uow_factory.transaction() as uow:
            return await uow.history.prune_older_than(user_id, days=7)


class FavoriteService:
    """Saved links organised in collections."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def add(
        self,
        user_id: int,
        *,
        url: str,
        title: str = "",
        platform: Platform = Platform.GENERIC,
        thumbnail_url: str | None = None,
        collection_id: int | None = None,
        note: str | None = None,
    ) -> Favorite:
        async with self._uow_factory.transaction() as uow:
            if await uow.favorites.by_url(user_id, url) is not None:
                raise ConflictError("This link is already in your favourites")
            if await uow.favorites.count(user_id=user_id) >= MAX_FAVORITES_PER_USER:
                raise ValidationError("Favourites limit reached")
            favorite = await uow.favorites.create(
                user_id=user_id,
                url=url,
                title=title[:512],
                platform=platform,
                thumbnail_url=thumbnail_url,
                collection_id=collection_id,
                note=note,
            )
            if collection_id:
                await uow.favorites.recount_collection(collection_id)
            return favorite

    async def remove(self, user_id: int, favorite_id: int) -> None:
        async with self._uow_factory.transaction() as uow:
            favorite = await uow.favorites.get(favorite_id)
            if favorite is None or favorite.user_id != user_id:
                raise NotFoundError("Favourite not found")
            collection_id = favorite.collection_id
            await uow.favorites.delete(favorite)
            await uow.flush()
            if collection_id:
                await uow.favorites.recount_collection(collection_id)

    async def list_page(
        self,
        user_id: int,
        *,
        collection_id: int | None = None,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[list[Favorite], int]:
        offset = (max(page, 1) - 1) * page_size
        async with self._uow_factory() as uow:
            rows = await uow.favorites.list_for_user(
                user_id, collection_id=collection_id, limit=page_size, offset=offset
            )
            filters: dict[str, Any] = {"user_id": user_id}
            if collection_id is not None:
                filters["collection_id"] = collection_id
            total = await uow.favorites.count(**filters)
        return list(rows), total

    async def collections(self, user_id: int) -> list[FavoriteCollection]:
        async with self._uow_factory() as uow:
            return list(await uow.favorites.collections(user_id))

    async def create_collection(
        self,
        user_id: int,
        name: str,
        icon: str = "📁",
    ) -> FavoriteCollection:
        clean = name.strip()[:64]
        if not clean:
            raise ValidationError("Collection name cannot be empty")
        async with self._uow_factory.transaction() as uow:
            existing = await uow.favorites.collections(user_id)
            if len(existing) >= MAX_COLLECTIONS_PER_USER:
                raise ValidationError("Collections limit reached")
            if any(collection.name.lower() == clean.lower() for collection in existing):
                raise ConflictError("A collection with this name already exists")
            return await uow.favorites.create_collection(user_id, clean, icon)

    async def delete_collection(self, user_id: int, collection_id: int) -> None:
        async with self._uow_factory.transaction() as uow:
            collection = await uow.favorites.get_collection(user_id, collection_id)
            if collection is None:
                raise NotFoundError("Collection not found")
            await uow.session.delete(collection)


class NotificationService:
    """Durable outbox for user-facing messages and broadcasts."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        dispatcher: TaskDispatcher,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher

    async def notify(
        self,
        user_id: int,
        notification_type: NotificationType,
        body: str,
        *,
        title: str | None = None,
        payload: dict[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        dispatch_now: bool = True,
    ) -> Notification:
        async with self._uow_factory.transaction() as uow:
            notification = await uow.notifications.enqueue(
                user_id=user_id,
                notification_type=notification_type,
                body=body,
                title=title,
                payload=payload,
                scheduled_at=scheduled_at,
            )
        if dispatch_now and scheduled_at is None:
            self._dispatcher.dispatch_notification(notification.id)
        return notification

    async def broadcast(
        self,
        *,
        body: str,
        broadcast_id: str,
        title: str | None = None,
        tier: SubscriptionTier | None = None,
        language: Language | None = None,
        only_active_days: int | None = None,
        payload: dict[str, Any] | None = None,
        batch_size: int = 1000,
    ) -> BroadcastResult:
        """Materialise one notification row per recipient, then dispatch.

        Writing rows first makes a broadcast resumable: if the worker dies
        mid-way, the remaining rows are still ``PENDING`` and get picked up by
        the periodic dispatcher.
        """
        recipients = 0
        offset = 0
        while True:
            async with self._uow_factory.transaction() as uow:
                user_ids = await uow.users.broadcast_targets(
                    tier=tier,
                    language=language,
                    only_active_days=only_active_days,
                    limit=batch_size,
                    offset=offset,
                )
                if not user_ids:
                    break
                for user_id in user_ids:
                    await uow.notifications.enqueue(
                        user_id=user_id,
                        notification_type=NotificationType.BROADCAST,
                        body=body,
                        title=title,
                        payload=payload,
                        broadcast_id=broadcast_id,
                    )
                recipients += len(user_ids)
                offset += batch_size
        if recipients:
            self._dispatcher.dispatch_broadcast(broadcast_id)
        log.bind(channel=LogChannel.ADMIN.value).info(
            "broadcast {} queued for {} users", broadcast_id, recipients
        )
        return BroadcastResult(broadcast_id=broadcast_id, recipients=recipients)

    async def due(self, *, limit: int = 100) -> list[Notification]:
        async with self._uow_factory() as uow:
            return list(await uow.notifications.due(limit=limit, now=datetime.now(UTC)))

    async def pending_for_broadcast(
        self,
        broadcast_id: str,
        *,
        limit: int = 200,
    ) -> list[Notification]:
        async with self._uow_factory() as uow:
            return list(await uow.notifications.pending_by_broadcast(broadcast_id, limit=limit))

    async def mark_sent(self, notification_id: int) -> None:
        async with self._uow_factory.transaction() as uow:
            await uow.notifications.mark_sent(notification_id)

    async def mark_failed(self, notification_id: int, error: str) -> None:
        async with self._uow_factory.transaction() as uow:
            await uow.notifications.mark_failed(notification_id, error)

    async def mark_skipped(self, notification_id: int, reason: str) -> None:
        async with self._uow_factory.transaction() as uow:
            await uow.notifications.mark_skipped(notification_id, reason)

    async def broadcast_stats(self, broadcast_id: str) -> dict[str, int]:
        async with self._uow_factory() as uow:
            return await uow.notifications.broadcast_stats(broadcast_id)
