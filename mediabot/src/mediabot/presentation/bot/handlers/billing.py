"""Subscription catalogue, checkout, promo codes and payment confirmation."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.core.exceptions import (
    InsufficientFundsError,
    MediaBotError,
    PromoCodeAlreadyUsedError,
    PromoCodeExhaustedError,
    PromoCodeExpiredError,
    PromoCodeNotFoundError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import PaymentProvider, SubscriptionTier
from mediabot.domain.policies import get_tier_policy
from mediabot.presentation.bot.callbacks import SubscriptionCallback
from mediabot.presentation.bot.formatters import render_subscription
from mediabot.presentation.bot.i18n.translator import Translator, all_translations
from mediabot.presentation.bot.keyboards import payment_providers, subscription_plans
from mediabot.presentation.bot.states import PromoStates

router = Router(name="billing")
log = get_logger(LogChannel.PAYMENTS, component="handlers.billing")


@router.message(Command("subscription"))
@router.message(F.text.in_(all_translations("menu.subscription")))
async def show_subscription(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    subscription = await container.subscriptions.current(ctx.user_id)
    await message.answer(
        render_subscription(
            ctx.tier,
            ctx.policy,
            subscription.expires_at if subscription else None,
            t,
        )
        + "\n\n"
        + t("subscription.choose_plan"),
        reply_markup=subscription_plans(t, container.settings.payments.currency),
    )


@router.callback_query(SubscriptionCallback.filter(F.action == "plans"))
async def back_to_plans(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            t("subscription.choose_plan"),
            reply_markup=subscription_plans(t, container.settings.payments.currency),
        )
    await callback.answer()


@router.callback_query(SubscriptionCallback.filter(F.action == "plan"))
async def choose_provider(
    callback: CallbackQuery,
    callback_data: SubscriptionCallback,
    t: Translator,
    container: Container,
) -> None:
    price, _stars = container.payments.find_plan(callback_data.tier, callback_data.days)
    providers = container.payments.available_providers()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            t(
                "subscription.choose_provider",
                price=f"{price} {container.settings.payments.currency}",
            ),
            reply_markup=payment_providers(providers, callback_data.tier, callback_data.days, t),
        )
    await callback.answer()


@router.callback_query(SubscriptionCallback.filter(F.action == "pay"))
async def start_checkout(
    callback: CallbackQuery,
    callback_data: SubscriptionCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Create the invoice and hand it to the user."""
    provider = PaymentProvider(callback_data.provider)
    if not isinstance(callback.message, Message):
        await callback.answer()
        return

    if provider is PaymentProvider.BALANCE:
        try:
            await container.payments.pay_with_balance(
                user_id=ctx.user_id, tier=callback_data.tier, days=callback_data.days
            )
        except InsufficientFundsError as exc:
            await callback.answer(
                t(
                    "error.insufficient_funds",
                    required=exc.details.get("required", "?"),
                    balance=exc.details.get("balance", 0),
                ),
                show_alert=True,
            )
            return
        await callback.message.answer(
            t(
                "subscription.success",
                tier=callback_data.tier.value.title(),
                days=callback_data.days,
            )
        )
        await callback.answer()
        return

    _payment, invoice = await container.payments.create_invoice(
        user_id=ctx.user_id,
        tier=callback_data.tier,
        days=callback_data.days,
        provider=provider,
        language=ctx.language.value,
    )

    if invoice.url:
        await callback.message.answer(f"🔗 {invoice.url}")
        await callback.answer()
        return

    await callback.message.answer_invoice(
        title=f"{callback_data.tier.emoji} {callback_data.tier.value.title()}",
        description=f"MediaBot subscription — {callback_data.days} days",
        payload=invoice.payload,
        provider_token=invoice.provider_token or "",
        currency=invoice.currency,
        prices=[LabeledPrice(label=label, amount=amount) for label, amount in invoice.prices],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, container: Container) -> None:
    """Telegram asks for a final go/no-go before charging the user."""
    async with container.uow_factory() as uow:
        payment = await uow.payments.by_payload(query.invoice_payload)
    if payment is None:
        await query.answer(ok=False, error_message="Invoice not found or expired")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Confirm the payment and grant the subscription."""
    payment_info = message.successful_payment
    if payment_info is None:  # pragma: no cover - guarded by the filter
        return
    payment = await container.payments.confirm(
        payload=payment_info.invoice_payload,
        external_id=payment_info.provider_payment_charge_id,
        amount=payment_info.total_amount,
    )
    tier = SubscriptionTier(payment.tier) if payment.tier else SubscriptionTier.PREMIUM
    await message.answer(
        t("subscription.success", tier=tier.value.title(), days=payment.days)
        + "\n\n"
        + render_subscription(tier, get_tier_policy(tier), None, t)
    )
    await container.achievements.evaluate(ctx.user_id)


# --------------------------------------------------------------------------- #
# Promo codes
# --------------------------------------------------------------------------- #
@router.message(Command("promo"))
async def promo_prompt(message: Message, state: FSMContext, t: Translator) -> None:
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer(t("promo.prompt"))


@router.callback_query(SubscriptionCallback.filter(F.action == "promo"))
async def promo_prompt_callback(
    callback: CallbackQuery,
    state: FSMContext,
    t: Translator,
) -> None:
    await state.set_state(PromoStates.waiting_for_code)
    if isinstance(callback.message, Message):
        await callback.message.answer(t("promo.prompt"))
    await callback.answer()


@router.message(PromoStates.waiting_for_code)
async def promo_redeem(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await state.clear()
    try:
        redemption = await container.promos.redeem(ctx.user_id, message.text or "")
    except PromoCodeNotFoundError:
        await message.answer(t("promo.not_found"))
        return
    except PromoCodeExpiredError:
        await message.answer(t("promo.expired"))
        return
    except PromoCodeExhaustedError:
        await message.answer(t("promo.exhausted"))
        return
    except PromoCodeAlreadyUsedError:
        await message.answer(t("promo.already_used"))
        return
    except MediaBotError as exc:
        await message.answer(f"⚠️ {exc.message}")
        return

    # Subscription rewards are applied here, keeping the promo service free of
    # a dependency on the subscription service.
    if redemption.lifetime:
        await container.subscriptions.grant(
            user_id=ctx.user_id, tier=SubscriptionTier.LIFETIME, days=0, source="promo"
        )
    elif redemption.vip_days:
        await container.subscriptions.grant(
            user_id=ctx.user_id,
            tier=SubscriptionTier.VIP,
            days=redemption.vip_days,
            source="promo",
        )
    elif redemption.premium_days:
        await container.subscriptions.grant(
            user_id=ctx.user_id,
            tier=SubscriptionTier.PREMIUM,
            days=redemption.premium_days,
            source="promo",
        )

    await message.answer(t("promo.success", summary=redemption.summary))
    await container.achievements.evaluate(ctx.user_id)
