"""Celery application, tasks and the dispatcher abstraction.

``celery -A mediabot.infrastructure.queue worker`` resolves the ``app`` symbol
below.  It is exposed through a module-level ``__getattr__`` so that importing
this package (which the bot does for the dispatcher protocol) does not require
Celery to be installed — the standalone mode runs without it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "celery_app"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from mediabot.infrastructure.queue.celery_app import celery_app

        return celery_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
