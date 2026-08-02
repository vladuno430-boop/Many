"""aiogram middlewares: DI, i18n, user resolution, throttling and metrics.

Order matters and is set in :mod:`mediabot.presentation.bot.app`:

1. :class:`MetricsMiddleware`   — measures everything below it.
2. :class:`ContainerMiddleware` — injects services.
3. :class:`ThrottleMiddleware`  — rejects floods *before* any DB write.
4. :class:`UserMiddleware`      — registers/loads the user and its context.
5. :class:`I18nMiddleware`      — builds the translator from the user's locale.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.core.exceptions import (
    FloodDetectedError,
    MediaBotError,
    RateLimitedError,
    UserBannedError,
    UserMutedError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import Language
from mediabot.infrastructure.metrics import BOT_UPDATES, HANDLER_LATENCY
from mediabot.presentation.bot.i18n.translator import Translator, translator_for

log = get_logger(LogChannel.APP, component="middleware")

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _extract_user(event: TelegramObject) -> TgUser | None:
    return getattr(event, "from_user", None)


class ContainerMiddleware(BaseMiddleware):
    """Injects the DI container and the most used services into handler data."""

    def __init__(self, container: Container) -> None:
        self.container = container

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["container"] = self.container
        data["settings"] = self.container.settings
        return await handler(event, data)


class MetricsMiddleware(BaseMiddleware):
    """Counts updates and records handler latency."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_type = type(event).__name__
        BOT_UPDATES.labels(event_type).inc()
        started = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            HANDLER_LATENCY.labels(event_type).observe(time.perf_counter() - started)


class ThrottleMiddleware(BaseMiddleware):
    """Applies flood control and rate limiting to every incoming update."""

    def __init__(self, container: Container) -> None:
        self.container = container

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _extract_user(event)
        if user is None:
            return await handler(event, data)

        scope = "callback" if isinstance(event, CallbackQuery) else "message"
        try:
            await self.container.security.check(user.id, scope=scope)
        except FloodDetectedError as exc:
            await self._reject(event, f"🚫 {exc.message}")
            return None
        except RateLimitedError as exc:
            await self._reject(event, f"🐢 {exc.message}")
            return None
        return await handler(event, data)

    @staticmethod
    async def _reject(event: TelegramObject, text: str) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)


class UserMiddleware(BaseMiddleware):
    """Registers the user on first contact and injects their context.

    Banned users are stopped here, which means no handler ever has to think
    about moderation state.
    """

    def __init__(self, container: Container) -> None:
        self.container = container

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = _extract_user(event)
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        payload = self._start_payload(event)
        user = await self.container.users.get_or_create(
            user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code,
            is_premium_telegram=bool(getattr(tg_user, "is_premium", False)),
            referral_code=payload,
        )
        try:
            await self.container.users.ensure_not_restricted(user)
        except (UserBannedError, UserMutedError) as exc:
            await self._reject(event, exc)
            return None

        context: UserContext = await self.container.users.build_context(tg_user.id)
        data["user"] = user
        data["ctx"] = context
        return await handler(event, data)

    @staticmethod
    def _start_payload(event: TelegramObject) -> str | None:
        """Extract the ``/start <payload>`` referral code, if present."""
        if not isinstance(event, Message) or not event.text:
            return None
        parts = event.text.split(maxsplit=1)
        if parts[0].split("@")[0] != "/start" or len(parts) < 2:
            return None
        return parts[1].strip()[:32]

    @staticmethod
    async def _reject(event: TelegramObject, exc: MediaBotError) -> None:
        text = f"⛔ {exc.message}"
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)


class I18nMiddleware(BaseMiddleware):
    """Builds a :class:`Translator` for the user's language."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        context: UserContext | None = data.get("ctx")
        if context is not None:
            language = context.language
        else:
            tg_user = _extract_user(event)
            language = Language.parse(tg_user.language_code if tg_user else None)
        translator: Translator = translator_for(language)
        data["t"] = translator
        data["lang"] = language
        return await handler(event, data)
