"""Celery tasks.

Architecture note
-----------------
Celery workers are synchronous processes, so every task opens its own event
loop via :func:`asyncio.run` and builds a *process-local* container which is
cached in :data:`_worker_container`.  Building it once per process (rather than
per task) keeps the connection pools warm; the pool is disposed on worker
shutdown.

Tasks are intentionally thin: they translate a task invocation into an
application-service call and manage retries.  All business logic lives in the
application layer, which keeps it testable without a broker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from celery import Task
from celery.signals import worker_process_init, worker_process_shutdown

from mediabot.core.container import Container
from mediabot.core.exceptions import DownloadError, MediaBotError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.infrastructure.queue.celery_app import celery_app

log = get_logger(LogChannel.QUEUE, component="tasks")

T = TypeVar("T")

_worker_container: Container | None = None


def get_container() -> Container:
    """Return (and lazily build) the container of this worker process."""
    global _worker_container
    if _worker_container is None:
        container = Container()
        # The worker delivers finished media itself, so it needs the Telegram
        # adapter wired into the download service.
        from mediabot.infrastructure.notifications.telegram import (
            TelegramDelivery,
        )

        try:
            bot = container.bot()
            container.set_delivery(TelegramDelivery(bot, container.settings, container.uow_factory))
        except RuntimeError:  # pragma: no cover - token-less maintenance worker
            log.warning("bot token missing: worker runs without media delivery")
        _worker_container = container
    return _worker_container


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Execute a coroutine from synchronous Celery code."""
    return asyncio.run(coro)


@worker_process_init.connect
def _on_worker_init(**_: object) -> None:  # pragma: no cover - process hook
    get_container()


@worker_process_shutdown.connect
def _on_worker_shutdown(**_: object) -> None:  # pragma: no cover - process hook
    global _worker_container
    if _worker_container is not None:
        run_async(_worker_container.shutdown())
        _worker_container = None


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
@celery_app.task(
    bind=True,
    name="mediabot.download.process",
    autoretry_for=(DownloadError,),
    retry_backoff=10,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    acks_late=True,
)
def process_download(self: Task, download_id: int) -> dict[str, Any]:
    """Run a queued download job."""
    container = get_container()
    worker_name = self.request.hostname or "worker"
    log.info("processing download={} on {}", download_id, worker_name)
    try:
        run_async(container.downloads.execute(download_id, worker_name=worker_name))
    except MediaBotError as exc:
        log.error("download {} failed: {} ({})", download_id, exc.message, exc.code)
        raise
    return {"download_id": download_id, "status": "completed"}


@celery_app.task(name="mediabot.download.cancel")
def cancel_download(download_id: int) -> bool:
    """Cancel a job from outside the bot (API/admin panel)."""
    container = get_container()
    return bool(run_async(container.downloads.cancel(download_id)))


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
@celery_app.task(name="mediabot.notify.send_one")
def send_notification(notification_id: int) -> bool:
    """Deliver a single queued notification."""
    container = get_container()
    return bool(run_async(_send_notification(container, notification_id)))


async def _send_notification(container: Container, notification_id: int) -> bool:
    from mediabot.infrastructure.notifications.telegram import (
        NotificationSender,
    )

    async with container.uow_factory() as uow:
        notification = await uow.notifications.get(notification_id)
        user = await uow.users.get(notification.user_id) if notification else None
    if notification is None:
        return False
    if user is None or not user.notifications_enabled:
        await container.notifications.mark_skipped(notification_id, "notifications disabled")
        return False

    sender = NotificationSender(container.bot(), container.uow_factory)
    delivered = await sender.send(notification)
    if delivered:
        await container.notifications.mark_sent(notification_id)
    else:
        await container.notifications.mark_failed(notification_id, "delivery refused")
    return delivered


@celery_app.task(name="mediabot.notify.dispatch_pending")
def dispatch_pending_notifications(limit: int = 200) -> int:
    """Deliver every notification whose schedule has arrived."""
    container = get_container()
    return int(run_async(_dispatch_pending(container, limit)))


async def _dispatch_pending(container: Container, limit: int) -> int:
    pending = await container.notifications.due(limit=limit)
    delivered = 0
    for notification in pending:
        if await _send_notification(container, notification.id):
            delivered += 1
    return delivered


@celery_app.task(name="mediabot.notify.send_broadcast")
def send_broadcast(broadcast_id: str, batch: int = 200) -> int:
    """Deliver one broadcast, resuming automatically after a crash."""
    container = get_container()
    return int(run_async(_send_broadcast(container, broadcast_id, batch)))


async def _send_broadcast(container: Container, broadcast_id: str, batch: int) -> int:
    delivered = 0
    while True:
        pending = await container.notifications.pending_for_broadcast(broadcast_id, limit=batch)
        if not pending:
            break
        for notification in pending:
            if await _send_notification(container, notification.id):
                delivered += 1
    log.bind(channel=LogChannel.ADMIN.value).info(
        "broadcast {} delivered to {} users", broadcast_id, delivered
    )
    return delivered


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #
@celery_app.task(name="mediabot.maintenance.expire_subscriptions")
def expire_subscriptions() -> int:
    """Downgrade users whose subscription elapsed and warn the ones expiring."""
    container = get_container()
    return int(run_async(_expire_subscriptions(container)))


async def _expire_subscriptions(container: Container) -> int:
    from mediabot.domain.enums import NotificationType

    expired = await container.subscriptions.expire_due()
    for user_id in expired:
        await container.notifications.notify(
            user_id,
            NotificationType.SUBSCRIPTION_EXPIRED,
            body="subscription_expired",
            dispatch_now=False,
        )
    for subscription in await container.subscriptions.expiring_soon(within_hours=24):
        await container.notifications.notify(
            subscription.user_id,
            NotificationType.SUBSCRIPTION_EXPIRING,
            body="subscription_expiring",
            payload={"tier": subscription.tier.value, "hours": 24},
            dispatch_now=False,
        )
    return len(expired)


@celery_app.task(name="mediabot.maintenance.cleanup_artifacts")
def cleanup_artifacts() -> dict[str, int]:
    """Remove stale files from the storage volume."""
    container = get_container()
    removed, freed = run_async(container.downloads.cleanup_artifacts())
    return {"removed": removed, "freed_bytes": freed}


@celery_app.task(name="mediabot.maintenance.reclaim_stale_jobs")
def reclaim_stale_jobs() -> int:
    """Fail jobs whose worker died so the user is not left waiting forever."""
    container = get_container()
    return int(run_async(container.downloads.reclaim_stale()))


@celery_app.task(name="mediabot.maintenance.aggregate_statistics")
def aggregate_statistics() -> bool:
    """Materialise today's analytics row (guarded by a distributed lock)."""
    container = get_container()
    return bool(run_async(_aggregate(container)))


async def _aggregate(container: Container) -> bool:
    async with container.lock.acquire("aggregate-statistics", ttl_seconds=300) as acquired:
        if not acquired:
            return False
        await container.statistics.aggregate_day()
        return True


@celery_app.task(name="mediabot.maintenance.verify_payments")
def verify_payments() -> int:
    """Poll pull-based payment providers for pending invoices."""
    container = get_container()
    return int(run_async(container.payments.verify_pending()))


@celery_app.task(name="mediabot.maintenance.expire_promos")
def expire_promos() -> int:
    """Flip elapsed promo codes to the expired state."""
    container = get_container()
    return int(run_async(_expire_promos(container)))


async def _expire_promos(container: Container) -> int:
    async with container.uow_factory.transaction() as uow:
        expired = await uow.promos.expire_due()
        await uow.limits.clear_expired()
    return expired


__all__: list[str] = [
    "aggregate_statistics",
    "cancel_download",
    "cleanup_artifacts",
    "dispatch_pending_notifications",
    "expire_promos",
    "expire_subscriptions",
    "process_download",
    "reclaim_stale_jobs",
    "send_broadcast",
    "send_notification",
    "verify_payments",
]
