"""History and favourites: browsing, searching, filtering and re-downloading."""

from __future__ import annotations

import math

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.core.exceptions import MediaBotError
from mediabot.domain.enums import MediaKind
from mediabot.presentation.bot.callbacks import FavoriteCallback, HistoryCallback
from mediabot.presentation.bot.formatters import render_history_entry, short_label
from mediabot.presentation.bot.i18n.translator import Translator, all_translations
from mediabot.presentation.bot.keyboards import favorites_keyboard, history_keyboard
from mediabot.presentation.bot.states import FavoriteStates, HistoryStates

router = Router(name="library")

PAGE_SIZE = 5


def _kind_from(value: str) -> MediaKind | None:
    return {"video": MediaKind.VIDEO, "audio": MediaKind.AUDIO}.get(value)


async def _render_history(
    *,
    target: Message,
    ctx: UserContext,
    container: Container,
    t: Translator,
    page: int,
    kind: str,
    query: str | None,
    edit: bool,
) -> None:
    views, total = await container.history.list_page(
        ctx.user_id,
        query=query,
        kind=_kind_from(kind),
        page=page,
        page_size=PAGE_SIZE,
    )
    if not views:
        text = t("history.empty")
        markup = history_keyboard([], page=1, pages=1, kind=kind, t=t)
    else:
        pages = max(math.ceil(total / PAGE_SIZE), 1)
        lines = [t("history.title", total=total), ""]
        buttons: list[tuple[int, str]] = []
        for index, view in enumerate(views, start=(page - 1) * PAGE_SIZE + 1):
            lines.append(
                render_history_entry(
                    index,
                    title=view.title,
                    kind=view.kind,
                    quality=view.quality,
                    size=view.file_size,
                    created_at=view.created_at,
                    t=t,
                )
            )
            buttons.append((view.id, f"{index}. {short_label(view.title)}"))
        lines.append("")
        lines.append(t("common.page", page=page, pages=pages))
        text = "\n".join(lines)
        markup = history_keyboard(buttons, page=page, pages=pages, kind=kind, t=t)

    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(Command("history"))
@router.message(F.text.in_(all_translations("menu.history")))
async def show_history(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await state.clear()
    await _render_history(
        target=message,
        ctx=ctx,
        container=container,
        t=t,
        page=1,
        kind="all",
        query=None,
        edit=False,
    )


@router.callback_query(HistoryCallback.filter(F.action.in_({"page", "filter"})))
async def history_navigate(
    callback: CallbackQuery,
    callback_data: HistoryCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    page = callback_data.page if callback_data.action == "page" else 1
    await _render_history(
        target=callback.message,
        ctx=ctx,
        container=container,
        t=t,
        page=page,
        kind=callback_data.kind,
        query=None,
        edit=True,
    )
    await callback.answer()


@router.callback_query(HistoryCallback.filter(F.action == "search"))
async def history_search_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    t: Translator,
) -> None:
    await state.set_state(HistoryStates.waiting_for_query)
    if isinstance(callback.message, Message):
        await callback.message.answer(t("history.search_prompt"))
    await callback.answer()


@router.message(HistoryStates.waiting_for_query)
async def history_search(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await state.clear()
    await _render_history(
        target=message,
        ctx=ctx,
        container=container,
        t=t,
        page=1,
        kind="all",
        query=(message.text or "")[:64],
        edit=False,
    )


@router.callback_query(HistoryCallback.filter(F.action == "open"))
async def history_open(
    callback: CallbackQuery,
    callback_data: HistoryCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Re-send a previously produced file straight from Telegram's storage."""
    view = await container.history.get(ctx.user_id, callback_data.entry_id)
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    if view.telegram_file_id:
        caption = f"<b>{view.title[:120]}</b>"
        if view.kind is MediaKind.VIDEO:
            await callback.message.answer_video(view.telegram_file_id, caption=caption)
        else:
            await callback.message.answer_audio(view.telegram_file_id, caption=caption)
        await container.history.mark_repeat(view.id)
    else:
        await callback.message.answer(view.url)
    await callback.answer()


# --------------------------------------------------------------------------- #
# Favourites
# --------------------------------------------------------------------------- #
async def _render_favorites(
    *,
    target: Message,
    ctx: UserContext,
    container: Container,
    t: Translator,
    page: int,
    edit: bool,
) -> None:
    items, total = await container.favorites.list_page(ctx.user_id, page=page, page_size=PAGE_SIZE)
    if not items:
        text = t("favorites.empty")
        markup = favorites_keyboard([], page=1, pages=1, t=t)
    else:
        pages = max(math.ceil(total / PAGE_SIZE), 1)
        text = t("favorites.title", total=total) + "\n\n" + t("common.page", page=page, pages=pages)
        markup = favorites_keyboard(
            [
                (item.id, f"{item.platform.emoji} {short_label(item.title or item.url)}")
                for item in items
            ],
            page=page,
            pages=pages,
            t=t,
        )
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(Command("favorites"))
@router.message(F.text.in_(all_translations("menu.favorites")))
async def show_favorites(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await state.clear()
    await _render_favorites(target=message, ctx=ctx, container=container, t=t, page=1, edit=False)


@router.callback_query(FavoriteCallback.filter(F.action == "page"))
async def favorites_page(
    callback: CallbackQuery,
    callback_data: FavoriteCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    if isinstance(callback.message, Message):
        await _render_favorites(
            target=callback.message,
            ctx=ctx,
            container=container,
            t=t,
            page=callback_data.page,
            edit=True,
        )
    await callback.answer()


@router.callback_query(FavoriteCallback.filter(F.action == "open"))
async def favorites_open(
    callback: CallbackQuery,
    callback_data: FavoriteCallback,
    ctx: UserContext,
    container: Container,
) -> None:
    async with container.uow_factory() as uow:
        favorite = await uow.favorites.get(callback_data.entry_id)
    if favorite is None or favorite.user_id != ctx.user_id:
        await callback.answer()
        return
    if isinstance(callback.message, Message):
        await callback.message.answer(favorite.url)
    await callback.answer()


@router.callback_query(FavoriteCallback.filter(F.action == "delete"))
async def favorites_delete(
    callback: CallbackQuery,
    callback_data: FavoriteCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    try:
        await container.favorites.remove(ctx.user_id, callback_data.entry_id)
        await callback.answer(t("favorites.removed"))
    except MediaBotError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    if isinstance(callback.message, Message):
        await _render_favorites(
            target=callback.message, ctx=ctx, container=container, t=t, page=1, edit=True
        )


@router.callback_query(FavoriteCallback.filter(F.action == "collections"))
async def favorites_collections(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    collections = await container.favorites.collections(ctx.user_id)
    if not collections:
        text = t("common.empty")
    else:
        text = "\n".join(
            f"{collection.icon} <b>{collection.name}</b> — {collection.items_count}"
            for collection in collections
        )
    if isinstance(callback.message, Message):
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(FavoriteCallback.filter(F.action == "new_collection"))
async def favorites_new_collection(
    callback: CallbackQuery,
    state: FSMContext,
    t: Translator,
) -> None:
    await state.set_state(FavoriteStates.waiting_for_collection_name)
    if isinstance(callback.message, Message):
        await callback.message.answer(t("favorites.collection_prompt"))
    await callback.answer()


@router.message(FavoriteStates.waiting_for_collection_name)
async def favorites_create_collection(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await state.clear()
    collection = await container.favorites.create_collection(
        ctx.user_id, (message.text or "").strip()
    )
    await message.answer(t("favorites.collection_created", name=collection.name))
