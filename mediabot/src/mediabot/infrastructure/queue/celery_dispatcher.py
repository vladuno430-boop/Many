"""Celery-backed task dispatcher.

Separate from :mod:`mediabot.infrastructure.queue.dispatcher` on purpose: that
module must stay importable without Celery installed, because the standalone
runtime mode (Termux) ships neither a broker nor the Celery package.
"""

from __future__ import annotations

from celery import Celery

from mediabot.core.logging import LogChannel, get_logger
from mediabot.infrastructure.queue.queues import QUEUE_DOWNLOADS, QUEUE_NOTIFICATIONS

log = get_logger(LogChannel.QUEUE, component="celery_dispatcher")


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
