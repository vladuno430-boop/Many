"""In-process task dispatcher for the standalone runtime mode.

Architecture note
-----------------
In the distributed topology a Celery worker consumes jobs from Redis.  On a
phone (Termux) there is no broker and no second process, so this dispatcher
runs the very same application services as ``asyncio`` tasks inside the bot's
event loop.

It keeps the two guarantees that matter:

* **Concurrency is bounded** — a semaphore sized from
  ``DOWNLOADER__MAX_CONCURRENT_JOBS`` prevents a phone from starting ten
  FFmpeg processes at once.
* **Failures are contained** — every task is wrapped, so a failed download can
  never take the bot down with it.

Because the dispatcher is created before the services it calls, the collaborators
are injected lazily through callables, which also keeps the import graph acyclic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from mediabot.core.logging import LogChannel, get_logger

log = get_logger(LogChannel.QUEUE, component="inline_dispatcher")

#: ``(download_id) -> awaitable`` and friends, resolved on first use.
DownloadRunner = Callable[[int], Awaitable[None]]
NotificationRunner = Callable[[int], Awaitable[bool]]
BroadcastRunner = Callable[[str], Awaitable[int]]


class InlineDispatcher:
    """Executes jobs in the current process instead of handing them to Celery."""

    def __init__(self, *, max_concurrent_jobs: int = 2) -> None:
        self._semaphore = asyncio.Semaphore(max(max_concurrent_jobs, 1))
        self._tasks: set[asyncio.Task[Any]] = set()
        self._download_runner: DownloadRunner | None = None
        self._notification_runner: NotificationRunner | None = None
        self._broadcast_runner: BroadcastRunner | None = None
        self._cancelled: set[int] = set()

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #
    def bind(
        self,
        *,
        download_runner: DownloadRunner,
        notification_runner: NotificationRunner,
        broadcast_runner: BroadcastRunner,
    ) -> None:
        """Attach the application services once the container is built."""
        self._download_runner = download_runner
        self._notification_runner = notification_runner
        self._broadcast_runner = broadcast_runner

    # ------------------------------------------------------------------ #
    # TaskDispatcher protocol
    # ------------------------------------------------------------------ #
    def dispatch_download(self, download_id: int, *, priority: int = 0) -> str:
        task_id = f"inline-download-{download_id}"
        self._spawn(self._run_download(download_id), task_id)
        return task_id

    def dispatch_notification(self, notification_id: int) -> str:
        task_id = f"inline-notify-{notification_id}"
        self._spawn(self._run_notification(notification_id), task_id)
        return task_id

    def dispatch_broadcast(self, broadcast_id: str) -> str:
        task_id = f"inline-broadcast-{broadcast_id}"
        self._spawn(self._run_broadcast(broadcast_id), task_id)
        return task_id

    def revoke(self, task_id: str) -> None:
        """Cancel a pending or running inline job."""
        if task_id.startswith("inline-download-"):
            self._cancelled.add(int(task_id.rsplit("-", 1)[1]))
        for task in list(self._tasks):
            if task.get_name() == task_id and not task.done():
                task.cancel()

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _spawn(self, coro: Any, name: str) -> None:
        """Start a background task, keeping a strong reference to it.

        Without the reference the event loop may garbage-collect a running
        task mid-flight — a classic asyncio footgun.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - dispatched outside a loop
            log.error("inline dispatch requested without a running event loop: {}", name)
            return
        task = loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_download(self, download_id: int) -> None:
        if self._download_runner is None:  # pragma: no cover - misconfiguration
            log.error("inline dispatcher has no download runner bound")
            return
        async with self._semaphore:
            if download_id in self._cancelled:
                self._cancelled.discard(download_id)
                log.info("inline download {} was cancelled before it started", download_id)
                return
            try:
                await self._download_runner(download_id)
            except asyncio.CancelledError:  # pragma: no cover - user cancellation
                log.info("inline download {} cancelled", download_id)
                raise
            except Exception as exc:
                # The download service already recorded the failure; this guard
                # only stops the exception from reaching the event loop.
                log.warning("inline download {} failed: {}", download_id, exc)

    async def _run_notification(self, notification_id: int) -> None:
        if self._notification_runner is None:  # pragma: no cover - misconfiguration
            return
        try:
            await self._notification_runner(notification_id)
        except Exception as exc:
            log.warning("inline notification {} failed: {}", notification_id, exc)

    async def _run_broadcast(self, broadcast_id: str) -> None:
        if self._broadcast_runner is None:  # pragma: no cover - misconfiguration
            return
        try:
            delivered = await self._broadcast_runner(broadcast_id)
            log.info("inline broadcast {} delivered to {} users", broadcast_id, delivered)
        except Exception as exc:
            log.warning("inline broadcast {} failed: {}", broadcast_id, exc)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def running_jobs(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for the in-flight jobs during a graceful shutdown."""
        pending = [task for task in self._tasks if not task.done()]
        if not pending:
            return
        log.info("waiting for {} inline jobs to finish", len(pending))
        _done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            task.cancel()
