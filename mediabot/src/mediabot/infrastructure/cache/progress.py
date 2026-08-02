"""Synchronous progress publisher used from yt-dlp's download thread.

Architecture note
-----------------
yt-dlp invokes its progress hooks from a plain worker thread that has no event
loop, so publishing progress with the async Redis client from there is unsafe.
This module keeps one *synchronous* Redis connection per process and writes the
same key the async :class:`CacheService` reads, so the bot can poll it.
"""

from __future__ import annotations

import json
from typing import Any

import redis as sync_redis

from mediabot.core.config import RedisSettings
from mediabot.domain.value_objects import DownloadProgress
from mediabot.infrastructure.cache.redis_cache import NS_PROGRESS


class ProgressPublisher:
    """Writes ``progress:<download_id>`` snapshots from synchronous code."""

    def __init__(self, settings: RedisSettings, *, ttl_seconds: int = 3600) -> None:
        self._client: sync_redis.Redis = sync_redis.Redis.from_url(
            settings.cache_dsn,
            decode_responses=True,
            socket_timeout=2,
        )
        self._ttl = ttl_seconds

    def publish(self, download_id: int, progress: DownloadProgress) -> None:
        payload: dict[str, Any] = {
            "percent": progress.percent,
            "downloaded": progress.downloaded_bytes,
            "total": progress.total_bytes,
            "speed": progress.speed_bytes_per_sec,
            "eta": progress.eta_seconds,
            "stage": progress.stage,
        }
        try:
            self._client.setex(
                f"{NS_PROGRESS}:{download_id}",
                self._ttl,
                json.dumps(payload),
            )
        except sync_redis.RedisError:
            # Progress is advisory: never fail a download because Redis blinked.
            return

    def clear(self, download_id: int) -> None:
        try:
            self._client.delete(f"{NS_PROGRESS}:{download_id}")
        except sync_redis.RedisError:
            return

    def close(self) -> None:
        """Release the Redis connection held by this publisher."""
        try:
            self._client.close()  # type: ignore[no-untyped-call]
        except sync_redis.RedisError:  # pragma: no cover - shutdown path
            return
