"""Bot composition: dispatcher, routers, middlewares and lifecycle."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from mediabot.core.container import Container
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import Language
from mediabot.infrastructure.cache.redis_cache import create_redis
from mediabot.presentation.bot.handlers import (
    admin,
    billing,
    captcha,
    common,
    download,
    library,
    profile,
    wallet,
)
from mediabot.presentation.bot.handlers import (
    settings as settings_handlers,
)
from mediabot.presentation.bot.i18n.translator import translator_for
from mediabot.presentation.bot.middlewares import (
    ContainerMiddleware,
    I18nMiddleware,
    MetricsMiddleware,
    ThrottleMiddleware,
    UserMiddleware,
)

log = get_logger(LogChannel.APP, component="bot")

#: Commands advertised in the Telegram UI, per language.
_COMMAND_KEYS: tuple[tuple[str, str], ...] = (
    ("start", "start.menu"),
    ("profile", "menu.profile"),
    ("history", "menu.history"),
    ("favorites", "menu.favorites"),
    ("subscription", "menu.subscription"),
    ("wallet", "menu.wallet"),
    ("referral", "menu.referral"),
    ("achievements", "menu.achievements"),
    ("promo", "promo.prompt"),
    ("settings", "menu.settings"),
    ("help", "menu.help"),
)


def create_dispatcher(container: Container) -> Dispatcher:
    """Build the dispatcher with middlewares and routers in the right order.

    The FSM storage follows the runtime mode: Redis when it is available (so
    conversations survive a restart and can be shared between replicas), an
    in-memory store in standalone mode, where there is exactly one process.
    """
    storage: BaseStorage
    if container.settings.redis.enabled and not container.settings.app.is_standalone:
        storage = RedisStorage(
            redis=create_redis(container.settings.redis, container.settings.redis.db_fsm)
        )
    else:
        storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)

    for middleware in (
        MetricsMiddleware(),
        ContainerMiddleware(container),
        ThrottleMiddleware(container),
        UserMiddleware(container),
        I18nMiddleware(),
    ):
        dispatcher.message.middleware(middleware)
        dispatcher.callback_query.middleware(middleware)

    # Pre-checkout queries must not be throttled or they time out.
    dispatcher.pre_checkout_query.middleware(ContainerMiddleware(container))

    dispatcher.include_routers(
        common.router,
        captcha.router,
        admin.router,
        billing.router,
        profile.router,
        library.router,
        wallet.router,
        settings_handlers.router,
        # The link handler is last: it owns the broad "any text with a URL"
        # filter and must not shadow the command/menu routers above.
        download.router,
    )
    return dispatcher


async def set_commands(bot: Bot) -> None:
    """Publish the command list in every supported language."""
    for language in Language:
        t = translator_for(language)
        commands = [
            BotCommand(command=command, description=t(key)[:256]) for command, key in _COMMAND_KEYS
        ]
        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeAllPrivateChats(),
            language_code=language.value,
        )


async def on_startup(container: Container, bot: Bot) -> None:
    """Prepare the runtime: admin bootstrap, commands, webhook wiring."""
    await container.admin.ensure_bootstrap_admin()
    await set_commands(bot)

    telegram = container.settings.telegram
    if telegram.use_webhook:
        await bot.set_webhook(
            url=f"{telegram.webhook_url.rstrip('/')}{telegram.webhook_path}",
            secret_token=telegram.webhook_secret.get_secret_value() or None,
            drop_pending_updates=True,
            allowed_updates=[
                "message",
                "callback_query",
                "pre_checkout_query",
                "my_chat_member",
            ],
        )
        log.info("webhook registered at {}", telegram.webhook_path)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("running in long-polling mode")


async def on_shutdown(container: Container, bot: Bot) -> None:
    log.info("shutting the bot down")
    if container.settings.telegram.use_webhook:
        await bot.delete_webhook()
    await container.shutdown()


async def _maintenance_loop(container: Container, stop: asyncio.Event) -> None:
    """Periodic housekeeping for the standalone mode.

    In the distributed topology Celery beat and the scheduler process own these
    jobs.  With a single process there is nobody else to run them, so the bot
    does it on a slow timer: expired subscriptions, stale jobs, artefact
    clean-up and the statistics roll-up.
    """
    interval = 300.0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        for name, job in (
            ("subscriptions", container.subscriptions.expire_due),
            ("stale-jobs", container.downloads.reclaim_stale),
            ("artifacts", container.downloads.cleanup_artifacts),
            ("statistics", container.statistics.aggregate_day),
        ):
            try:
                await job()
            except Exception as exc:  # pragma: no cover - job isolation
                log.warning("maintenance job {} failed: {}", name, exc)


async def run_polling(container: Container) -> None:
    """Entry point for long polling (development, standalone and small setups)."""
    bot = container.bot()
    dispatcher = create_dispatcher(container)

    from mediabot.infrastructure.notifications.telegram import (
        TelegramDelivery,
    )

    container.set_delivery(TelegramDelivery(bot, container.settings, container.uow_factory))

    await on_startup(container, bot)
    stop = asyncio.Event()
    maintenance: asyncio.Task[None] | None = None
    if container.settings.app.is_standalone:
        maintenance = asyncio.create_task(_maintenance_loop(container, stop))
        log.info("standalone mode: maintenance runs inside the bot process")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        stop.set()
        if maintenance is not None:
            maintenance.cancel()
        await on_shutdown(container, bot)
