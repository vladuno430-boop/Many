"""Celery application, tasks and the dispatcher abstraction."""

from mediabot.infrastructure.queue.celery_app import celery_app

#: ``celery -A mediabot.infrastructure.queue worker`` resolves this symbol.
app = celery_app

__all__ = ["app", "celery_app"]
