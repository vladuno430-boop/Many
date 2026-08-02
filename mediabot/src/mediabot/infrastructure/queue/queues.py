"""Queue names shared by the dispatcher, the Celery app and the tasks.

Kept in a dependency-free module so the bot can import the dispatcher without
pulling in Celery — which is exactly what the standalone (Termux) mode needs.
"""

from __future__ import annotations

from typing import Final

QUEUE_DOWNLOADS: Final[str] = "downloads"
QUEUE_NOTIFICATIONS: Final[str] = "notifications"
QUEUE_MAINTENANCE: Final[str] = "maintenance"
