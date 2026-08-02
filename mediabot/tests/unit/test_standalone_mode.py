"""Tests for the standalone runtime mode (the one Termux uses)."""

from __future__ import annotations

import asyncio

import pytest

from mediabot.core.config import Settings
from mediabot.core.security import hash_password, verify_password
from mediabot.infrastructure.cache.memory import InMemoryProgressPublisher, InMemoryRedis
from mediabot.infrastructure.cache.redis_cache import (
    CacheService,
    DistributedLock,
    RateLimiter,
)
from mediabot.infrastructure.queue.inline import InlineDispatcher

pytestmark = pytest.mark.unit


class TestInMemoryRedis:
    async def test_set_and_get(self) -> None:
        redis = InMemoryRedis()
        await redis.set("k", "v")
        assert await redis.get("k") == "v"

    async def test_missing_key_is_none(self) -> None:
        assert await InMemoryRedis().get("nope") is None

    async def test_expiry_is_honoured(self) -> None:
        redis = InMemoryRedis()
        await redis.set("k", "v", ex=1)
        assert await redis.get("k") == "v"
        # Rewind the entry instead of sleeping so the test stays fast.
        redis._data["k"].expires_at = 0.0
        assert await redis.get("k") is None

    async def test_set_nx_does_not_overwrite(self) -> None:
        redis = InMemoryRedis()
        assert await redis.set("k", "first", nx=True) is True
        assert await redis.set("k", "second", nx=True) is False
        assert await redis.get("k") == "first"

    async def test_incr_creates_and_increments(self) -> None:
        redis = InMemoryRedis()
        assert await redis.incr("counter") == 1
        assert await redis.incr("counter") == 2

    async def test_ttl_reports_redis_semantics(self) -> None:
        redis = InMemoryRedis()
        assert await redis.ttl("absent") == -2
        await redis.set("forever", "1")
        assert await redis.ttl("forever") == -1
        await redis.set("timed", "1", ex=60)
        assert 0 < await redis.ttl("timed") <= 60

    async def test_delete_and_exists(self) -> None:
        redis = InMemoryRedis()
        await redis.set("a", "1")
        await redis.set("b", "2")
        assert await redis.exists("a") == 1
        assert await redis.delete("a", "b", "missing") == 2
        assert await redis.exists("a") == 0

    async def test_scan_iter_matches_a_pattern(self) -> None:
        redis = InMemoryRedis()
        await redis.set("media:1", "x")
        await redis.set("media:2", "x")
        await redis.set("user:1", "x")
        found = [key async for key in redis.scan_iter(match="media:*")]
        assert sorted(found) == ["media:1", "media:2"]

    async def test_sweep_drops_expired_entries(self) -> None:
        redis = InMemoryRedis()
        await redis.set("gone", "1", ex=1)
        redis._data["gone"].expires_at = 0.0
        assert redis.sweep() == 1
        assert len(redis) == 0


class TestRedisBackedServicesOnMemory:
    """The production classes must run unchanged on the in-memory backend."""

    async def test_cache_service_round_trip(self) -> None:
        cache = CacheService(InMemoryRedis())  # type: ignore[arg-type]
        await cache.cache_media_info("hash", {"title": "demo"}, ttl=60)
        assert await cache.get_media_info("hash") == {"title": "demo"}

        await cache.cache_user(42, {"tier": "free"})
        assert await cache.get_user(42) == {"tier": "free"}
        await cache.invalidate_user(42)
        assert await cache.get_user(42) is None

    async def test_cache_namespace_invalidation(self) -> None:
        cache = CacheService(InMemoryRedis())  # type: ignore[arg-type]
        await cache.set(CacheService.key("media", "a"), {"x": 1})
        await cache.set(CacheService.key("media", "b"), {"x": 2})
        await cache.set(CacheService.key("user", "c"), {"x": 3})
        assert await cache.invalidate_namespace("media") == 2
        assert await cache.get(CacheService.key("user", "c")) == {"x": 3}

    async def test_rate_limiter_blocks_after_the_budget(self) -> None:
        limiter = RateLimiter(InMemoryRedis())  # type: ignore[arg-type]
        allowed = [await limiter.hit("user:1", limit=3) for _ in range(4)]
        assert [result[0] for result in allowed] == [True, True, True, False]

    async def test_flood_control_bans_and_reports_ttl(self) -> None:
        limiter = RateLimiter(InMemoryRedis())  # type: ignore[arg-type]
        flooding = False
        for _ in range(5):
            flooding = await limiter.check_flood(
                "user:2", threshold=3, window_seconds=5, ban_seconds=60
            )
        assert flooding
        assert await limiter.ban_seconds_left("user:2") > 0

        await limiter.reset("user:2")
        assert await limiter.ban_seconds_left("user:2") == 0

    async def test_distributed_lock_is_exclusive(self) -> None:
        redis = InMemoryRedis()
        lock = DistributedLock(redis)  # type: ignore[arg-type]
        async with lock.acquire("job") as first:
            assert first is True
            async with lock.acquire("job") as second:
                assert second is False
        # Released after the block, so it can be taken again.
        async with lock.acquire("job") as third:
            assert third is True

    async def test_progress_publisher_round_trip(self) -> None:
        from mediabot.domain.value_objects import DownloadProgress

        publisher = InMemoryProgressPublisher()
        publisher.publish(
            7,
            DownloadProgress(
                percent=42.0,
                downloaded_bytes=100,
                total_bytes=200,
                speed_bytes_per_sec=50.0,
                eta_seconds=2,
                stage="downloading",
            ),
        )
        snapshot = publisher.snapshot(7)
        assert snapshot is not None
        assert snapshot["percent"] == 42.0
        publisher.clear(7)
        assert publisher.snapshot(7) is None


class TestInlineDispatcher:
    async def test_runs_the_download_in_process(self) -> None:
        executed: list[int] = []

        async def runner(download_id: int) -> None:
            executed.append(download_id)

        dispatcher = InlineDispatcher(max_concurrent_jobs=2)
        dispatcher.bind(
            download_runner=runner,
            notification_runner=lambda _id: asyncio.sleep(0, result=True),
            broadcast_runner=lambda _id: asyncio.sleep(0, result=0),
        )
        task_id = dispatcher.dispatch_download(11)
        assert task_id == "inline-download-11"
        await dispatcher.drain(timeout=5)
        assert executed == [11]

    async def test_concurrency_is_bounded(self) -> None:
        peak = 0
        current = 0

        async def runner(_download_id: int) -> None:
            nonlocal peak, current
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1

        dispatcher = InlineDispatcher(max_concurrent_jobs=2)
        dispatcher.bind(
            download_runner=runner,
            notification_runner=lambda _id: asyncio.sleep(0, result=True),
            broadcast_runner=lambda _id: asyncio.sleep(0, result=0),
        )
        for job_id in range(6):
            dispatcher.dispatch_download(job_id)
        await dispatcher.drain(timeout=5)
        assert peak <= 2

    async def test_a_failing_job_does_not_escape(self) -> None:
        async def runner(_download_id: int) -> None:
            raise RuntimeError("boom")

        dispatcher = InlineDispatcher()
        dispatcher.bind(
            download_runner=runner,
            notification_runner=lambda _id: asyncio.sleep(0, result=True),
            broadcast_runner=lambda _id: asyncio.sleep(0, result=0),
        )
        dispatcher.dispatch_download(1)
        await dispatcher.drain(timeout=5)  # must not raise

    async def test_revoke_prevents_a_queued_job_from_running(self) -> None:
        executed: list[int] = []
        gate = asyncio.Event()

        async def runner(download_id: int) -> None:
            await gate.wait()
            executed.append(download_id)

        dispatcher = InlineDispatcher(max_concurrent_jobs=1)
        dispatcher.bind(
            download_runner=runner,
            notification_runner=lambda _id: asyncio.sleep(0, result=True),
            broadcast_runner=lambda _id: asyncio.sleep(0, result=0),
        )
        dispatcher.dispatch_download(1)  # occupies the single slot
        dispatcher.dispatch_download(2)  # waits for the semaphore
        await asyncio.sleep(0)
        dispatcher.revoke("inline-download-2")
        gate.set()
        await dispatcher.drain(timeout=5)
        assert executed == [1]


class TestStandaloneSettings:
    def test_sqlite_dsn_is_built_from_the_path(self) -> None:
        settings = Settings(
            app={"runtime_mode": "standalone"},
            db={"backend": "sqlite", "sqlite_path": "/data/mediabot.db"},
        )
        assert settings.app.is_standalone
        assert settings.db.is_sqlite
        assert settings.db.async_dsn == "sqlite+aiosqlite:////data/mediabot.db"
        assert settings.db.sync_dsn == "sqlite:////data/mediabot.db"

    def test_postgres_stays_the_default(self) -> None:
        settings = Settings()
        assert settings.app.runtime_mode == "distributed"
        assert settings.db.async_dsn.startswith("postgresql+asyncpg://")
        assert settings.redis.enabled is True

    def test_redis_can_be_disabled(self) -> None:
        settings = Settings(redis={"enabled": False})
        assert settings.redis.enabled is False


class TestPasswordFallback:
    def test_pbkdf2_is_used_without_bcrypt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("mediabot.core.security._BCRYPT_AVAILABLE", False)
        hashed = hash_password("phone-password")
        assert hashed.startswith("$pbkdf2-sha256$")
        assert verify_password("phone-password", hashed)
        assert not verify_password("wrong", hashed)

    def test_bcrypt_hashes_still_verify(self) -> None:
        hashed = hash_password("server-password")
        assert verify_password("server-password", hashed)

    def test_pbkdf2_hashes_verify_even_when_bcrypt_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database created on a phone must keep working on a server."""
        monkeypatch.setattr("mediabot.core.security._BCRYPT_AVAILABLE", False)
        hashed = hash_password("portable")
        monkeypatch.undo()
        assert verify_password("portable", hashed)

    def test_malformed_hash_is_rejected(self) -> None:
        assert not verify_password("x", "$pbkdf2-sha256$broken")
        assert not verify_password("x", "")
