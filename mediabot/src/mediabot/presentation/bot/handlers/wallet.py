"""Wallet: balance, daily bonus, coin shop and statement."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.core.exceptions import InsufficientFundsError, MediaBotError
from mediabot.domain.enums import SubscriptionTier
from mediabot.presentation.bot.callbacks import WalletCallback
from mediabot.presentation.bot.formatters import time_until
from mediabot.presentation.bot.i18n.translator import Translator, all_translations
from mediabot.presentation.bot.keyboards import shop_keyboard, wallet_keyboard

router = Router(name="wallet")


async def _wallet_text(ctx: UserContext, container: Container, t: Translator) -> str:
    async with container.uow_factory() as uow:
        wallet = await uow.wallets.get_or_create(ctx.user_id)
        await uow.commit()
    return t(
        "wallet.title",
        balance=wallet.balance,
        earned=wallet.total_earned,
        spent=wallet.total_spent,
    )


@router.message(Command("wallet"))
@router.message(F.text.in_(all_translations("menu.wallet")))
async def show_wallet(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await message.answer(await _wallet_text(ctx, container, t), reply_markup=wallet_keyboard(t))


@router.callback_query(WalletCallback.filter(F.action == "root"))
async def wallet_root(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            await _wallet_text(ctx, container, t), reply_markup=wallet_keyboard(t)
        )
    await callback.answer()


@router.callback_query(WalletCallback.filter(F.action == "daily"))
async def claim_daily(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Claim the daily bonus; refuses a second claim on the same day."""
    result = await container.daily_bonus.claim(ctx.user_id)
    if not result.granted:
        await callback.answer(
            t("wallet.daily_already", time=time_until(result.next_available_at)),
            show_alert=True,
        )
        return
    await callback.answer(t("common.done"))
    if isinstance(callback.message, Message):
        calendar = container.daily_bonus.calendar(result.streak)
        rows = " ".join(f"{'✅' if claimed else '⬜'}{coins}" for _day, coins, claimed in calendar)
        await callback.message.answer(
            t("wallet.daily_claimed", coins=result.coins, streak=result.streak) + f"\n\n{rows}"
        )
    await container.achievements.evaluate(ctx.user_id)


@router.callback_query(WalletCallback.filter(F.action == "shop"))
async def show_shop(callback: CallbackQuery, t: Translator) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(t("wallet.shop"), reply_markup=shop_keyboard(t))
    await callback.answer()


@router.callback_query(WalletCallback.filter(F.action == "buy"))
async def buy_item(
    callback: CallbackQuery,
    callback_data: WalletCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Purchase a shop item, applying subscription grants where relevant."""
    purchase = container.economy.resolve_purchase(callback_data.item)
    if purchase is None:
        await callback.answer(t("common.error"), show_alert=True)
        return
    try:
        await container.wallet.purchase(ctx.user_id, callback_data.item)
    except InsufficientFundsError:
        await callback.answer(t("wallet.not_enough"), show_alert=True)
        return
    except MediaBotError as exc:
        await callback.answer(exc.message, show_alert=True)
        return

    if purchase.tier is not None and purchase.days:
        await container.subscriptions.grant(
            user_id=ctx.user_id,
            tier=SubscriptionTier(purchase.tier),
            days=purchase.days,
            source="coin_shop",
        )
    await callback.answer(t("wallet.purchase_success", item=callback_data.item))
    if isinstance(callback.message, Message):
        await callback.message.answer(
            await _wallet_text(ctx, container, t), reply_markup=wallet_keyboard(t)
        )


@router.callback_query(WalletCallback.filter(F.action == "statement"))
async def show_statement(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    rows = await container.wallet.statement(ctx.user_id, limit=15)
    if not rows:
        text = t("common.empty")
    else:
        text = "\n".join(
            "{sign}{amount} 🪙 · {reason} · {date}".format(
                sign="+" if row["type"] == "credit" else "−",
                amount=row["amount"],
                reason=str(row["reason"]).replace("_", " "),
                date=row["created_at"].strftime("%d.%m %H:%M"),
            )
            for row in rows
        )
    if isinstance(callback.message, Message):
        await callback.message.answer(text)
    await callback.answer()
