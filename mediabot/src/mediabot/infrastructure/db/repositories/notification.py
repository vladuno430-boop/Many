"""Repository for queued outbound notifications."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select, update

from mediabot.domain.enums import NotificationStatus, NotificationType
from mediabot.infrastructure.db.models.system import Notification
from mediabot.infrastructure.db.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    """Durable outbox for Telegram messages."""

    model = Notification

    async def enqueue(
        self,
        *,
        user_id: int,
        notification_type: NotificationType,
        body: str,
        title: str | None = None,
        payload: dict[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        broadcast_id: str | None = None,
    ) -> Notification:
        return await self.create(
            user_id=user_id,
            type=notification_type,
            body=body,
            title=title,
            payload=payload or {},
            scheduled_at=scheduled_at,
            broadcast_id=broadcast_id,
            status=NotificationStatus.PENDING,
        )

    async def due(self, *, limit: int = 100, now: datetime | None = None) -> Sequence[Notification]:
        """Pending notifications whose schedule has arrived."""
        now = now or datetime.now(UTC)
        stmt = (
            select(Notification)
            .where(
                Notification.status == NotificationStatus.PENDING,
                or_(Notification.scheduled_at.is_(None), Notification.scheduled_at <= now),
            )
            .order_by(Notification.created_at.asc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def pending_by_broadcast(
        self,
        broadcast_id: str,
        *,
        limit: int = 200,
    ) -> Sequence[Notification]:
        """Undelivered rows of one broadcast (used to resume a partial send)."""
        stmt = (
            select(Notification)
            .where(
                Notification.broadcast_id == broadcast_id,
                Notification.status == NotificationStatus.PENDING,
            )
            .order_by(Notification.id.asc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def mark_sent(self, notification_id: int) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(status=NotificationStatus.SENT, sent_at=datetime.now(UTC))
        )

    async def mark_failed(self, notification_id: int, error: str) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(
                status=NotificationStatus.FAILED,
                error=error[:255],
                attempts=Notification.attempts + 1,
            )
        )

    async def mark_skipped(self, notification_id: int, reason: str) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(status=NotificationStatus.SKIPPED, error=reason[:255])
        )

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def broadcast_stats(self, broadcast_id: str) -> dict[str, int]:
        stmt: Select[tuple[Any, int]] = (
            select(Notification.status, func.count())
            .where(Notification.broadcast_id == broadcast_id)
            .group_by(Notification.status)
        )
        rows = (await self.session.execute(stmt)).all()
        stats = {status.value: 0 for status in NotificationStatus}
        for status, count in rows:
            stats[NotificationStatus(status).value] = int(count)
        stats["total"] = sum(stats[status.value] for status in NotificationStatus)
        return stats
