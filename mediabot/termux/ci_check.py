"""Smoke-check the standalone profile the way a phone would run it.

Used by CI (and handy locally): builds the container with the standalone
settings, verifies that the in-process infrastructure was selected, and
exercises the database through the real services.
"""

from __future__ import annotations

import asyncio

from mediabot.core.container import Container
from mediabot.infrastructure.cache.memory import InMemoryProgressPublisher, InMemoryRedis
from mediabot.infrastructure.queue.inline import InlineDispatcher


async def main() -> None:
    container = Container()
    settings = container.settings

    assert settings.app.is_standalone, "APP__RUNTIME_MODE must be standalone"
    assert settings.db.is_sqlite, "standalone must run on SQLite"
    assert isinstance(container.redis, InMemoryRedis), "Redis must be replaced in-process"
    assert isinstance(container.dispatcher, InlineDispatcher), "worker must run inline"
    assert isinstance(container.progress_publisher, InMemoryProgressPublisher)

    await container.admin.ensure_bootstrap_admin()
    admin = await container.admin.authenticate(
        settings.api.admin_username,
        settings.api.admin_password.get_secret_value(),
    )
    assert admin.role.value == "owner"

    user = await container.users.get_or_create(user_id=1, username="ci", language_code="ru")
    context = await container.users.build_context(user.id)
    assert context.remaining_downloads == 10, context.remaining_downloads

    await container.cache.cache_user(user.id, {"tier": context.tier.value}, ttl=30)
    assert await container.cache.get_user(user.id) is not None
    await container.security.check(user.id)

    bonus = await container.daily_bonus.claim(user.id)
    assert bonus.granted and await container.wallet.balance(user.id) == bonus.coins

    await container.shutdown()
    print("standalone profile OK: sqlite + in-memory cache + inline worker")


if __name__ == "__main__":
    asyncio.run(main())
