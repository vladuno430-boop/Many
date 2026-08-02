"""Preferences: language, default formats, notifications, auto-download."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.presentation.bot.callbacks import SettingsCallback
from mediabot.presentation.bot.i18n.translator import (
    Translator,
    all_translations,
    translator_for,
)
from mediabot.presentation.bot.keyboards import (
    format_preferences_keyboard,
    language_keyboard,
    main_menu,
    settings_keyboard,
)

router = Router(name="settings")


async def _settings_markup(
    ctx: UserContext, container: Container, t: Translator
) -> InlineKeyboardMarkup:
    async with container.uow_factory() as uow:
        user = await uow.users.get(ctx.user_id)
    return settings_keyboard(
        t,
        notifications_enabled=bool(user and user.notifications_enabled),
        auto_download=bool(user and user.auto_download),
    )


@router.message(Command("settings"))
@router.message(F.text.in_(all_translations("menu.settings")))
async def show_settings(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await message.answer(
        t("settings.title"), reply_markup=await _settings_markup(ctx, container, t)
    )


@router.callback_query(SettingsCallback.filter(F.action == "root"))
async def settings_root(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            t("settings.title"), reply_markup=await _settings_markup(ctx, container, t)
        )
    await callback.answer()


@router.callback_query(SettingsCallback.filter(F.action == "languages"))
async def show_languages(callback: CallbackQuery, ctx: UserContext, t: Translator) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            t("settings.language"), reply_markup=language_keyboard(ctx.language, t)
        )
    await callback.answer()


@router.callback_query(SettingsCallback.filter(F.action == "language"))
async def change_language(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    ctx: UserContext,
    container: Container,
) -> None:
    """Switch the interface language and immediately re-render the menu."""
    await container.users.set_language(ctx.user_id, callback_data.language)
    translator = translator_for(callback_data.language)
    await callback.answer(translator("settings.language_changed"))
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            translator("settings.title"),
            reply_markup=language_keyboard(callback_data.language, translator),
        )
        await callback.message.answer(
            translator("start.menu"),
            reply_markup=main_menu(translator, is_admin=ctx.is_admin),
        )


@router.callback_query(SettingsCallback.filter(F.action == "toggle_notifications"))
async def toggle_notifications(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    async with container.uow_factory() as uow:
        user = await uow.users.get(ctx.user_id)
        enabled = not bool(user and user.notifications_enabled)
    await container.users.update_preferences(ctx.user_id, notifications_enabled=enabled)
    await callback.answer(t("settings.saved"))
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=await _settings_markup(ctx, container, t)
        )


@router.callback_query(SettingsCallback.filter(F.action == "toggle_auto"))
async def toggle_auto_download(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    async with container.uow_factory() as uow:
        user = await uow.users.get(ctx.user_id)
        enabled = not bool(user and user.auto_download)
    await container.users.update_preferences(ctx.user_id, auto_download=enabled)
    await callback.answer(t("settings.saved"))
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=await _settings_markup(ctx, container, t)
        )


@router.callback_query(SettingsCallback.filter(F.action == "formats"))
async def show_formats(callback: CallbackQuery, t: Translator) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            t("settings.format"), reply_markup=format_preferences_keyboard(t)
        )
    await callback.answer()


@router.callback_query(SettingsCallback.filter(F.action == "set_video_format"))
async def set_video_format(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await container.users.update_preferences(
        ctx.user_id, default_video_format=callback_data.video_format.value
    )
    await callback.answer(t("settings.saved"))


@router.callback_query(SettingsCallback.filter(F.action == "set_audio_format"))
async def set_audio_format(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await container.users.update_preferences(
        ctx.user_id, default_audio_format=callback_data.audio_format.value
    )
    await callback.answer(t("settings.saved"))
