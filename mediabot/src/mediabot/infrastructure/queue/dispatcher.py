"""Task dispatch abstraction.

The application layer must be able to enqueue work without importing Celery
(which would make services untestable and tie them to a broker).  It therefore
depends on :class:`TaskDispatcher`; production wires the Celery implementation,
tests wire :class:`InMemoryDispatcher`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from celery import Celery

from mediabot.core.logging import LogChannel, get_logger
from mediabot.infrastructure.queue.celery_app import (
    QUEUE_DOWNLOADS,
    QUEUE_NOTIFICATIONS,
)

log = get_logger(LogChannel.QUEUE, component="dispatcher")


@runtime_checkable
class TaskDispatcher(Protocol):
    """Contract used by application services to schedule background work."""

    def dispatch_download(self, download_id: int, *, priority: int = 0) -> str:
        """Queue a download job and return the task id."""
        ...

    def dispatch_notification(self, notification_id: int) -> str:
        """Queue delivery of a single notification."""
        ...

    def dispatch_broadcast(self, broadcast_id: str) -> str:
        """Queue delivery of a whole broadcast."""
        ...

    def revoke(self, task_id: str) -> None:
        """Best-effort cancellation of a queued or running task."""
        ...


class CeleryDispatcher:
    """Celery-backed :class:`TaskDispatcher`."""

    def __init__(self, app: Celery) -> None:
        self._app = app

    def dispatch_download(self, download_id: int, *, priority: int = 0) -> str:
        result = self._app.send_task(
            "mediabot.download.process",
            args=[download_id],
            queue=QUEUE_DOWNLOADS,
            priority=max(0, min(priority, 30)),
        )
        log.debug("dispatched download={} task={}", download_id, result.id)
        return str(result.id)

    def dispatch_notification(self, notification_id: int) -> str:
        result = self._app.send_task(
            "mediabot.notify.send_one",
            args=[notification_id],
            queue=QUEUE_NOTIFICATIONS,
        )
        return str(result.id)

    def dispatch_broadcast(self, broadcast_id: str) -> str:
        result = self._app.send_task(
            "mediabot.notify.send_broadcast",
            args=[broadcast_id],
            queue=QUEUE_NOTIFICATIONS,
        )
        return str(result.id)

    def revoke(self, task_id: str) -> None:
        self._app.control.revoke(task_id, terminate=True, signal="SIGTERM")


class InMemoryDispatcher:
    """Dispatcher that only records calls — used by the test-suite."""

    def __init__(self) -> None:
        self.downloads: list[tuple[int, int]] = []
        self.notifications: list[int] = []
        self.broadcasts: list[str] = []
        self.revoked: list[str] = []

    def dispatch_download(self, download_id: int, *, priority: int = 0) -> str:
        self.downloads.append((download_id, priority))
        return f"task-download-{download_id}"

    def dispatch_notification(self, notification_id: int) -> str:
        self.notifications.append(notification_id)
        return f"task-notify-{notification_id}"

    def dispatch_broadcast(self, broadcast_id: str) -> str:
        self.broadcasts.append(broadcast_id)
        return f"task-broadcast-{broadcast_id}"

    def revoke(self, task_id: str) -> None:
        self.revoked.append(task_id)
