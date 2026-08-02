"""In-process replacement for Redis, used by the standalone runtime mode.

Architecture note
-----------------
Instead of writing a second implementation of the cache, the rate limiter and
the lock, this module provides a tiny object that speaks the *subset of the
Redis protocol* those three classes actually use.  They therefore run unchanged
on a phone under Termux, and there is exactly one code path to reason about.

Everything is guarded by an ``asyncio.Lock`` and lives in this process only —
which is fine, because the standalone mode is by definition a single process.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Entry:
    """A stored value with an optional absolute expiry."""

    value: str
    expires_at: float | None = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class InMemoryRedis:
    """Minimal async, Redis-compatible key/value store.

    Implements only the commands MediaBot issues: ``get``, ``set`` (with
    ``ex``/``nx``), ``delete``, ``exists``, ``incr``, ``expire``, ``ttl``,
    ``scan_iter``, ``ping`` and ``aclose``.  Expiry is lazy (checked on read)
    plus an opportunistic sweep, which keeps the implementation small without
    leaking memory for short-lived keys.
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Strings
    # ------------------------------------------------------------------ #
    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.is_expired(time.monotonic()):
                del self._data[key]
                return None
            return entry.value

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        async with self._lock:
            now = time.monotonic()
            existing = self._data.get(key)
            if existing is not None and existing.is_expired(now):
                del self._data[key]
                existing = None
            if nx and existing is not None:
                return False
            self._data[key] = _Entry(str(value), now + ex if ex else None)
            return True

    async def setex(self, key: str, seconds: int, value: Any) -> bool:
        return await self.set(key, value, ex=seconds)

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            removed = 0
            for key in keys:
                if self._data.pop(key, None) is not None:
                    removed += 1
            return removed

    async def exists(self, key: str) -> int:
        return 1 if await self.get(key) is not None else 0

    async def incr(self, key: str, amount: int = 1) -> int:
        async with self._lock:
            now = time.monotonic()
            entry = self._data.get(key)
            if entry is None or entry.is_expired(now):
                entry = _Entry("0")
                self._data[key] = entry
            current = int(entry.value or 0) + amount
            entry.value = str(current)
            return current

    # ------------------------------------------------------------------ #
    # Expiry
    # ------------------------------------------------------------------ #
    async def expire(self, key: str, seconds: int) -> bool:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            entry.expires_at = time.monotonic() + seconds
            return True

    async def ttl(self, key: str) -> int:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return -2  # Redis: key does not exist
            if entry.expires_at is None:
                return -1  # Redis: key exists but has no expiry
            return max(int(entry.expires_at - time.monotonic()), 0)

    # ------------------------------------------------------------------ #
    # Scanning and lifecycle
    # ------------------------------------------------------------------ #
    async def scan_iter(self, match: str = "*", count: int = 100) -> AsyncIterator[str]:
        async with self._lock:
            now = time.monotonic()
            keys = [
                key
                for key, entry in self._data.items()
                if not entry.is_expired(now) and fnmatch.fnmatch(key, match)
            ]
        for key in keys:
            yield key

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        async with self._lock:
            self._data.clear()

    async def flushall(self) -> bool:
        await self.aclose()
        return True

    def sweep(self) -> int:
        """Drop expired entries; called periodically by the janitor job."""
        now = time.monotonic()
        expired = [key for key, entry in self._data.items() if entry.is_expired(now)]
        for key in expired:
            self._data.pop(key, None)
        return len(expired)

    def __len__(self) -> int:  # pragma: no cover - diagnostics only
        return len(self._data)


class InMemoryProgressPublisher:
    """Progress sink for the standalone mode.

    In distributed mode the worker is a separate process and publishes progress
    through Redis; standalone runs the download in the bot's own event loop, so
    a plain dictionary is enough (and avoids a synchronous Redis client on the
    yt-dlp thread).
    """

    def __init__(self) -> None:
        self._data: dict[int, dict[str, Any]] = {}

    def publish(self, download_id: int, progress: Any) -> None:
        self._data[download_id] = {
            "percent": progress.percent,
            "downloaded": progress.downloaded_bytes,
            "total": progress.total_bytes,
            "speed": progress.speed_bytes_per_sec,
            "eta": progress.eta_seconds,
            "stage": progress.stage,
        }

    def snapshot(self, download_id: int) -> dict[str, Any] | None:
        return self._data.get(download_id)

    def clear(self, download_id: int) -> None:
        self._data.pop(download_id, None)

    def close(self) -> None:
        self._data.clear()
