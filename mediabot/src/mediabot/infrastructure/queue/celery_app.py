"""Celery application and queue topology.

Architecture note
-----------------
Three queues with different characteristics:

``downloads``
    Long, IO-heavy jobs.  Prefetch is 1 so a worker never hoards jobs it cannot
    start, and ``acks_late`` guarantees a crashed worker's job is redelivered.
``notifications``
    Short, high-volume sends (broadcasts).  High prefetch, fast retries.
``maintenance``
    Periodic housekeeping driven by Celery beat (statistics, janitor, expiry).

Priorities inside ``downloads`` are the numeric queue priorities of the tiers,
so VIP jobs are consumed before free ones.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from mediabot.core.config import Settings, get_settings
from mediabot.infrastructure.queue.queues import (
    QUEUE_DOWNLOADS,
    QUEUE_MAINTENANCE,
    QUEUE_NOTIFICATIONS,
)

__all__ = [
    "QUEUE_DOWNLOADS",
    "QUEUE_MAINTENANCE",
    "QUEUE_NOTIFICATIONS",
    "celery_app",
    "create_celery_app",
]


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Build the configured Celery application."""
    settings = settings or get_settings()
    app = Celery(
        "mediabot",
        broker=settings.redis.broker_dsn,
        backend=settings.redis.result_dsn,
        include=["mediabot.infrastructure.queue.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone=settings.app.timezone,
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=50,  # recycle workers: yt-dlp leaks memory
        worker_send_task_events=True,
        broker_connection_retry_on_startup=True,
        result_expires=3600,
        task_soft_time_limit=settings.downloader.job_timeout_seconds,
        task_time_limit=settings.downloader.job_timeout_seconds + 120,
        task_default_queue=QUEUE_DOWNLOADS,
        task_queues=(
            Queue(QUEUE_DOWNLOADS, queue_arguments={"x-max-priority": 30}),
            Queue(QUEUE_NOTIFICATIONS),
            Queue(QUEUE_MAINTENANCE),
        ),
        task_routes={
            "mediabot.download.*": {"queue": QUEUE_DOWNLOADS},
            "mediabot.notify.*": {"queue": QUEUE_NOTIFICATIONS},
            "mediabot.maintenance.*": {"queue": QUEUE_MAINTENANCE},
        },
        beat_schedule={
            "expire-subscriptions": {
                "task": "mediabot.maintenance.expire_subscriptions",
                "schedule": crontab(minute="*/15"),
            },
            "cleanup-artifacts": {
                "task": "mediabot.maintenance.cleanup_artifacts",
                "schedule": crontab(minute="*/10"),
            },
            "aggregate-statistics": {
                "task": "mediabot.maintenance.aggregate_statistics",
                "schedule": crontab(minute=5, hour="*"),
            },
            "dispatch-notifications": {
                "task": "mediabot.notify.dispatch_pending",
                "schedule": 30.0,
            },
            "reclaim-stale-jobs": {
                "task": "mediabot.maintenance.reclaim_stale_jobs",
                "schedule": crontab(minute="*/5"),
            },
        },
    )
    return app


#: Module-level instance used by ``celery -A`` and by the task decorators.
celery_app = create_celery_app()
