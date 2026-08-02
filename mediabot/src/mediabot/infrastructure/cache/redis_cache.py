"""Redis-backed cache, rate limiter and distributed locks.

Architecture note
-----------------
Everything that touches Redis lives behind these three small classes so that
the application layer depends on intent (`cache.get_media_info`) rather than on
a Redis client.  Swapping Redis for another store would only touch this module.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from typing import Any, TypeVar, cast

from redis.asyncio import Redis, from_url

from mediabot.core.config import RedisSettings
from mediabot.core.logging import LogChannel, get_logger

T = TypeVar("T")

log = get_logger(LogChannel.APP, component="cache")

#: Key namespaces keep the shared Redis database readable and easy to purge.
NS_MEDIA = "media"
NS_USER = "user"
NS_LIMIT = "limit"
NS_PROGRESS = "progress"
NS_SETTINGS = "settings"
NS_RATE = "rate"
NS_FLOOD = "flood"
NS_LOCK = "lock"
NS_CAPTCHA = "captcha"
NS_SESSION = "session"
NS_DENYLIST = "jwt_denylist"


def create_redis(settings: RedisSettings, db: int | None = None) -> Redis:
    """Create an async Redis client with sane production defaults."""
    return cast(
        Redis,
        from_url(  # type: ignore[no-untyped-call]
            settings.dsn(db if db is not None else settings.db_cache),
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        ),
    )


def _default_serializer(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)


class CacheService:
    """Typed JSON cache with namespaced keys."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def key(namespace: str, *parts: str | int) -> str:
        return ":".join([namespace, *[str(part) for part in parts]])

    async def get(self, key: str) -> Any | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            log.warning("cache value is not valid json, dropping key={}", key)
            await self._redis.delete(key)
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=_default_serializer)
        await self._redis.set(key, payload, ex=ttl or None)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.delete(*keys)

    async def exists(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        ttl: int | None = None,
    ) -> T:
        """Read-through cache helper."""
        cached = await self.get(key)
        if cached is not None:
            return cast(T, cached)
        value = await factory()
        if value is not None:
            await self.set(key, value, ttl)
        return value

    async def invalidate_namespace(self, namespace: str) -> int:
        """Delete every key of a namespace using a non-blocking SCAN."""
        deleted = 0
        async for key in self._redis.scan_iter(match=f"{namespace}:*", count=500):
            await self._redis.delete(key)
            deleted += 1
        return deleted

    async def incr(self, key: str, *, ttl: int | None = None) -> int:
        value = int(await self._redis.incr(key))
        if ttl and value == 1:
            await self._redis.expire(key, ttl)
        return value

    async def ttl(self, key: str) -> int:
        return int(await self._redis.ttl(key))

    # -- convenience wrappers used across the application ---------------- #
    async def cache_media_info(self, url_hash: str, payload: dict[str, Any], ttl: int) -> None:
        await self.set(self.key(NS_MEDIA, url_hash), payload, ttl)

    async def get_media_info(self, url_hash: str) -> dict[str, Any] | None:
        value = await self.get(self.key(NS_MEDIA, url_hash))
        return cast(dict[str, Any] | None, value)

    async def cache_user(self, user_id: int, payload: dict[str, Any], ttl: int = 300) -> None:
        await self.set(self.key(NS_USER, user_id), payload, ttl)

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, await self.get(self.key(NS_USER, user_id)))

    async def invalidate_user(self, user_id: int) -> None:
        await self.delete(self.key(NS_USER, user_id), self.key(NS_LIMIT, user_id))

    async def set_progress(self, download_id: int, payload: dict[str, Any]) -> None:
        await self.set(self.key(NS_PROGRESS, download_id), payload, ttl=3600)

    async def get_progress(self, download_id: int) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, await self.get(self.key(NS_PROGRESS, download_id)))

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # pragma: no cover - health check path
            return False

    async def close(self) -> None:
        await self._redis.aclose()


class RateLimiter:
    """Sliding-window rate limiting and flood detection.

    Two independent mechanisms:

    * ``hit`` implements a fixed-window counter for the generic per-minute
      limit (cheap, one INCR per event);
    * ``check_flood`` uses a short window with a much lower threshold to catch
      bursts, and issues a temporary ban when it trips.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, identity: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        """Register an event.  Returns ``(allowed, remaining)``."""
        key = f"{NS_RATE}:{identity}:{window_seconds}"
        current = int(await self._redis.incr(key))
        if current == 1:
            await self._redis.expire(key, window_seconds)
        remaining = max(limit - current, 0)
        return current <= limit, remaining

    async def check_flood(
        self,
        identity: str,
        *,
        threshold: int,
        window_seconds: int,
        ban_seconds: int,
    ) -> bool:
        """Return ``True`` when the identity is currently flooding/banned."""
        ban_key = f"{NS_FLOOD}:ban:{identity}"
        if await self._redis.exists(ban_key):
            return True
        key = f"{NS_FLOOD}:{identity}"
        current = int(await self._redis.incr(key))
        if current == 1:
            await self._redis.expire(key, window_seconds)
        if current > threshold:
            await self._redis.set(ban_key, "1", ex=ban_seconds)
            log.bind(channel=LogChannel.SECURITY.value).warning(
                "flood detected identity={} hits={} ban={}s", identity, current, ban_seconds
            )
            return True
        return False

    async def ban_seconds_left(self, identity: str) -> int:
        ttl = int(await self._redis.ttl(f"{NS_FLOOD}:ban:{identity}"))
        return max(ttl, 0)

    async def reset(self, identity: str) -> None:
        await self._redis.delete(f"{NS_FLOOD}:{identity}", f"{NS_FLOOD}:ban:{identity}")


class DistributedLock:
    """Redis ``SET NX PX`` lock used to serialise cross-process work.

    Guarantees that, for example, only one worker materialises the nightly
    statistics even when several scheduler replicas are running.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @asynccontextmanager
    async def acquire(
        self,
        name: str,
        *,
        ttl_seconds: int = 60,
        blocking: bool = False,
    ) -> AsyncIterator[bool]:
        key = f"{NS_LOCK}:{name}"
        token = str(id(self)) + name
        acquired = bool(await self._redis.set(key, token, nx=True, ex=ttl_seconds))
        if not acquired and blocking:  # pragma: no cover - contention path
            acquired = bool(await self._redis.set(key, token, nx=True, ex=ttl_seconds))
        try:
            yield acquired
        finally:
            if acquired:
                stored = await self._redis.get(key)
                if stored == token:
                    await self._redis.delete(key)
