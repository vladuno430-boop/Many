"""Shared pytest fixtures.

Integration tests run against SQLite (``aiosqlite``) and ``fakeredis`` so the
whole suite is hermetic: no PostgreSQL, no Redis, no network.  The production
code path is unchanged — only the DSNs differ, which is exactly what the
``url_override`` escape hatch in :class:`DatabaseSettings` exists for.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

os.environ.setdefault("MEDIABOT_ENV_FILE", "/dev/null")

# Importing aiogram installs uvloop's event-loop policy — the right choice in
# production, but pytest-asyncio calls ``get_event_loop()`` outside a running
# loop and uvloop refuses to create one implicitly.  Force the stdlib policy
# for the whole suite, at import time (collection happens before fixtures).
import aiogram  # noqa: F401  (imported for its side effect)

asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

from mediabot.core.config import Settings
from mediabot.domain.enums import Language
from mediabot.domain.services.economy import EconomyService
from mediabot.domain.services.limits import LimitService
from mediabot.domain.services.promo import PromoService
from mediabot.domain.services.queueing import QueueService


@pytest.fixture
def settings() -> Settings:
    """Test settings pointing at in-memory infrastructure."""
    return Settings(
        app={
            "env": "local",
            "debug": True,
            "storage_dir": "/tmp/mediabot-test/storage",
            "temp_dir": "/tmp/mediabot-test/tmp",
        },
        telegram={"bot_token": "123:test", "bot_username": "test_bot", "root_admin_ids": [1]},
        db={"url_override": "sqlite+aiosqlite:///:memory:"},
        security={"jwt_secret": "unit-test-secret-key-that-is-long-enough!!"},
        api={"admin_username": "admin", "admin_password": "test-admin-password"},
        observability={"log_dir": "/tmp/mediabot-test/logs", "json_logs": False},
    )


@pytest.fixture
def limit_service() -> LimitService:
    return LimitService()


@pytest.fixture
def queue_service() -> QueueService:
    return QueueService()


@pytest.fixture
def promo_service() -> PromoService:
    return PromoService()


@pytest.fixture
def economy_service(settings: Settings) -> EconomyService:
    return EconomyService(settings.economy)


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[object]:
    """A fresh in-memory database with the full schema, per test."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from mediabot.infrastructure.db.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def uow_factory(engine: object):
    from mediabot.infrastructure.db.session import UnitOfWorkFactory, create_session_factory

    return UnitOfWorkFactory(create_session_factory(engine))  # type: ignore[arg-type]


@pytest.fixture
async def cache():
    """Cache backed by fakeredis (falls back to a stub when unavailable)."""
    from mediabot.infrastructure.cache.redis_cache import CacheService

    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = CacheService(client)
    yield service
    await client.aclose()


@pytest.fixture
async def rate_limiter(cache):
    from mediabot.infrastructure.cache.redis_cache import RateLimiter

    fakeredis = pytest.importorskip("fakeredis")
    return RateLimiter(fakeredis.aioredis.FakeRedis(decode_responses=True))


@pytest.fixture
def dispatcher():
    from mediabot.infrastructure.queue.dispatcher import InMemoryDispatcher

    return InMemoryDispatcher()


@pytest.fixture
async def user_service(uow_factory, cache, settings, limit_service, economy_service):
    from mediabot.application.services.user_service import UserService

    return UserService(uow_factory, cache, settings, limit_service, economy_service)


@pytest.fixture
async def registered_user(user_service):
    """A user with id 1000 already present in the database."""
    return await user_service.get_or_create(
        user_id=1000,
        username="tester",
        first_name="Test",
        language_code=Language.EN.value,
    )


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)
