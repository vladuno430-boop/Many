"""Process entry points.

Each deployable unit has one function here so the Docker images differ only by
their command:

* ``mediabot-bot``       — the Telegram bot (polling or webhook);
* ``mediabot-api``       — the REST API and the admin web panel;
* ``mediabot-scheduler`` — APScheduler jobs that complement Celery beat.

The Celery worker is started with ``celery -A mediabot.infrastructure.queue``.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import uvicorn

from mediabot.core.container import Container
from mediabot.core.logging import LogChannel, get_logger

log = get_logger(LogChannel.APP, component="main")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Translate SIGTERM/SIGINT into a graceful shutdown event."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX platforms
            continue


async def _run_bot() -> None:
    from mediabot.presentation.bot.app import run_polling

    container = Container()
    log.info("starting bot (env={})", container.settings.app.env)
    await run_polling(container)


def run_bot() -> None:
    """Console-script entry point for the Telegram bot."""
    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        log.info("bot stopped by the operator")


def run_api() -> None:
    """Console-script entry point for the REST API + admin panel."""
    container = Container()
    settings = container.settings
    uvicorn.run(
        "mediabot.presentation.api.app:create_app",
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,  # loguru already intercepts stdlib logging
        proxy_headers=True,
        forwarded_allow_ips="*",
        workers=1,
    )


async def _run_scheduler() -> None:
    """APScheduler loop for periodic maintenance owned by the app itself.

    Celery beat handles queue-related periodics; this scheduler covers jobs
    that need the application services directly (statistics, reminders,
    housekeeping) and can run even when no broker is reachable.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    container = Container()
    scheduler = AsyncIOScheduler(timezone=container.settings.app.timezone)

    async def guarded(name: str, coro_factory: Any) -> None:
        """Run a job under a distributed lock so replicas do not duplicate it."""
        async with container.lock.acquire(f"scheduler:{name}", ttl_seconds=600) as acquired:
            if not acquired:
                return
            try:
                await coro_factory()
            except Exception as exc:  # pragma: no cover - job isolation
                log.opt(exception=exc).error("scheduled job {} failed", name)

    scheduler.add_job(
        lambda: asyncio.create_task(guarded("statistics", container.statistics.aggregate_day)),
        IntervalTrigger(minutes=30),
        id="aggregate-statistics",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(guarded("subscriptions", container.subscriptions.expire_due)),
        IntervalTrigger(minutes=15),
        id="expire-subscriptions",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(guarded("artifacts", container.downloads.cleanup_artifacts)),
        IntervalTrigger(minutes=10),
        id="cleanup-artifacts",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(guarded("stale-jobs", container.downloads.reclaim_stale)),
        IntervalTrigger(minutes=5),
        id="reclaim-stale-jobs",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(guarded("payments", container.payments.verify_pending)),
        IntervalTrigger(minutes=5),
        id="verify-payments",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(
            guarded("daily-statistics", container.statistics.aggregate_day)
        ),
        CronTrigger(hour=0, minute=10),
        id="daily-statistics",
        replace_existing=True,
    )

    scheduler.start()
    log.info("scheduler started with {} jobs", len(scheduler.get_jobs()))

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await container.shutdown()
        log.info("scheduler stopped")


def run_scheduler() -> None:
    """Console-script entry point for the APScheduler process."""
    try:
        asyncio.run(_run_scheduler())
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        log.info("scheduler stopped by the operator")


def main() -> None:
    """``python -m mediabot <bot|api|scheduler>``."""
    command = sys.argv[1] if len(sys.argv) > 1 else "bot"
    match command:
        case "bot":
            run_bot()
        case "api":
            run_api()
        case "scheduler":
            run_scheduler()
        case _:
            print(f"Unknown command: {command}. Use bot | api | scheduler.")
            raise SystemExit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
