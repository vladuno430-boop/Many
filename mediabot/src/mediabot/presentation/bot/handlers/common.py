"""Start, help, main menu and the global error handler."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.core.exceptions import MediaBotError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.value_objects import humanize_bytes, humanize_duration
from mediabot.infrastructure.metrics import ERRORS_TOTAL
from mediabot.presentation.bot.callbacks import MenuCallback
from mediabot.presentation.bot.formatters import platforms_count, platforms_list
from mediabot.presentation.bot.i18n.translator import Translator
from mediabot.presentation.bot.keyboards import main_menu

router = Router(name="common")
log = get_logger(LogChannel.APP, component="handlers.common")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Entry point; also settles the referral bonus for invited users."""
    await state.clear()
    granted = await container.referrals.register(ctx.user_id)

    text = t(
        "start.greeting",
        name=message.from_user.first_name if message.from_user else "",
        platforms=platforms_count(),
    )
    if granted:
        text += "\n\n" + t(
            "start.referral_bonus",
            coins=container.settings.economy.referral_invitee_coins,
        )
    await message.answer(text, reply_markup=main_menu(t, is_admin=ctx.is_admin))
    await container.achievements.evaluate(ctx.user_id)


@router.message(Command("help"))
@router.message(F.text.func(lambda text: text and text.endswith("Help")))
async def cmd_help(message: Message, t: Translator) -> None:
    await message.answer(t("help.text", platforms=platforms_list()))


@router.message(Command("menu"))
async def cmd_menu(message: Message, ctx: UserContext, t: Translator) -> None:
    await message.answer(t("start.menu"), reply_markup=main_menu(t, is_admin=ctx.is_admin))


@router.callback_query(MenuCallback.filter(F.section == "root"))
async def back_to_menu(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
) -> None:
    if isinstance(callback.message, Message):
        await callback.message.answer(
            t("start.menu"), reply_markup=main_menu(t, is_admin=ctx.is_admin)
        )
    await callback.answer()


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Global error handler.

    Domain errors are translated into a friendly message; anything else is
    logged with a traceback and reported as a generic failure, so an internal
    detail never leaks into a chat.
    """
    exception = event.exception
    update = event.update
    message: Message | None = getattr(update, "message", None)
    callback: CallbackQuery | None = getattr(update, "callback_query", None)
    target = message or (
        callback.message if callback and isinstance(callback.message, Message) else None
    )

    if isinstance(exception, MediaBotError):
        ERRORS_TOTAL.labels(exception.code, "bot").inc()
        text = _translate_error(exception)
        log.warning("handled domain error code={} message={}", exception.code, exception.message)
    else:
        ERRORS_TOTAL.labels("unhandled", "bot").inc()
        log.opt(exception=exception).error("unhandled error in update {}", update.update_id)
        text = "⚠️ Something went wrong. Please try again."

    if callback is not None:
        await callback.answer(text[:200], show_alert=True)
    elif target is not None:
        await target.answer(text)
    return True


def _translate_error(exception: MediaBotError) -> str:
    """Best-effort localisation of a domain error without a user context."""
    details = exception.details
    if "limit" in details and isinstance(details["limit"], int):
        details = {**details, "limit": humanize_bytes(details["limit"])}
    if "seconds" in details and isinstance(details["seconds"], int):
        details = {**details, "seconds": humanize_duration(details["seconds"])}
    return f"⚠️ {exception.message}"
