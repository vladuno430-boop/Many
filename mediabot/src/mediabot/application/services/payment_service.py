"""Checkout flow: invoice creation and payment confirmation."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from mediabot.application.services.economy_service import PromoRedemptionService, WalletService
from mediabot.application.services.user_service import SubscriptionService
from mediabot.core.config import Settings
from mediabot.core.exceptions import (
    ConflictError,
    NotFoundError,
    PaymentError,
    ValidationError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import TokenEncryptor
from mediabot.domain.enums import (
    NotificationType,
    PaymentProvider,
    PaymentStatus,
    SubscriptionTier,
    TransactionReason,
)
from mediabot.domain.policies import SUBSCRIPTION_PLANS
from mediabot.infrastructure.db.models.billing import Payment
from mediabot.infrastructure.db.session import UnitOfWorkFactory
from mediabot.infrastructure.metrics import PAYMENTS_TOTAL
from mediabot.infrastructure.payments.base import Invoice, InvoiceRequest
from mediabot.infrastructure.payments.providers import PaymentGatewayRegistry

log = get_logger(LogChannel.PAYMENTS, component="payment_service")


class PaymentService:
    """Turns a plan selection into an invoice and a paid invoice into a grant.

    The payload string is the join key between our database and the provider:
    it is unique per attempt, stored on the row and echoed back by Telegram in
    ``successful_payment``, which makes confirmation idempotent.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        gateways: PaymentGatewayRegistry,
        subscriptions: SubscriptionService,
        wallet: WalletService,
        promos: PromoRedemptionService,
        encryptor: TokenEncryptor,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._gateways = gateways
        self._subscriptions = subscriptions
        self._wallet = wallet
        self._promos = promos
        self._encryptor = encryptor
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Catalogue
    # ------------------------------------------------------------------ #
    def plans(self) -> tuple[tuple[SubscriptionTier, int, int, int], ...]:
        return SUBSCRIPTION_PLANS

    def find_plan(self, tier: SubscriptionTier, days: int) -> tuple[int, int]:
        """Return ``(price_in_currency, price_in_stars)`` for a plan."""
        for plan_tier, plan_days, price, stars in SUBSCRIPTION_PLANS:
            if plan_tier is tier and plan_days == days:
                return price, stars
        raise NotFoundError(f"Unknown plan: {tier.value}/{days}d")

    def available_providers(self) -> tuple[PaymentProvider, ...]:
        return self._gateways.available()

    # ------------------------------------------------------------------ #
    # Checkout
    # ------------------------------------------------------------------ #
    async def create_invoice(
        self,
        *,
        user_id: int,
        tier: SubscriptionTier,
        days: int,
        provider: PaymentProvider,
        promo_code: str | None = None,
        language: str = "en",
    ) -> tuple[Payment, Invoice]:
        """Create a pending payment row and the provider invoice."""
        price, stars = self.find_plan(tier, days)
        amount = stars if provider is PaymentProvider.TELEGRAM_STARS else price
        discount = 0
        applied_code: str | None = None

        if promo_code:
            discounted, applied_code = await self._promos.preview_discount(
                promo_code, amount, user_id
            )
            discount = amount - discounted
            amount = discounted

        if amount <= 0:
            raise ValidationError("Invoice amount must be positive after the discount")

        payload = f"sub_{tier.value}_{days}_{secrets.token_hex(8)}"
        gateway = self._gateways.get(provider)
        request = InvoiceRequest(
            user_id=user_id,
            tier=tier,
            days=days,
            amount=amount,
            currency=(
                "XTR"
                if provider is PaymentProvider.TELEGRAM_STARS
                else self._settings.payments.currency
            ),
            title=f"{tier.emoji} {tier.value.title()} — {days} days",
            description=f"MediaBot {tier.value} subscription for {days} days",
            payload=payload,
            language=language,
        )
        invoice = await gateway.create_invoice(request)

        async with self._uow_factory.transaction() as uow:
            payment = await uow.payments.create(
                user_id=user_id,
                provider=provider,
                status=PaymentStatus.PENDING,
                amount=amount,
                currency=invoice.currency,
                tier=tier,
                days=days,
                external_id=invoice.external_id,
                invoice_payload=payload,
                promo_code=applied_code,
                discount_amount=discount,
                provider_payload=(
                    self._encryptor.encrypt(str(invoice.raw)) if invoice.raw else None
                ),
            )
        PAYMENTS_TOTAL.labels(provider.value, PaymentStatus.PENDING.value).inc()
        log.info(
            "invoice created user={} provider={} tier={} days={} amount={}",
            user_id,
            provider.value,
            tier.value,
            days,
            amount,
        )
        return payment, invoice

    async def pay_with_balance(
        self,
        *,
        user_id: int,
        tier: SubscriptionTier,
        days: int,
    ) -> Payment:
        """Settle a subscription from the internal coin balance."""
        price, _ = self.find_plan(tier, days)
        coins = price  # 1 coin == 1 currency unit in the internal economy
        payload = f"bal_{tier.value}_{days}_{secrets.token_hex(6)}"

        await self._wallet.debit(
            user_id,
            coins,
            TransactionReason.SPEND_PREMIUM,
            comment=f"{tier.value}:{days}d",
            reference=payload,
        )
        async with self._uow_factory.transaction() as uow:
            payment = await uow.payments.create(
                user_id=user_id,
                provider=PaymentProvider.BALANCE,
                status=PaymentStatus.PAID,
                amount=coins,
                currency="COINS",
                tier=tier,
                days=days,
                invoice_payload=payload,
                paid_at=datetime.now(UTC),
            )
        await self._subscriptions.grant(
            user_id=user_id,
            tier=tier,
            days=days,
            source="balance",
            payment_id=payment.id,
        )
        PAYMENTS_TOTAL.labels(PaymentProvider.BALANCE.value, PaymentStatus.PAID.value).inc()
        return payment

    async def confirm(
        self,
        *,
        payload: str,
        external_id: str | None = None,
        amount: int | None = None,
    ) -> Payment:
        """Confirm a payment and grant the subscription.

        Idempotent: confirming an already-paid payload returns the existing row
        without granting twice, which matters because Telegram may retry the
        ``successful_payment`` update.
        """
        async with self._uow_factory() as uow:
            payment = await uow.payments.by_payload(payload)
        if payment is None:
            raise NotFoundError(f"Unknown payment payload: {payload}")
        if payment.status is PaymentStatus.PAID:
            log.warning("duplicate confirmation for payload={}", payload)
            return payment
        if amount is not None and amount < payment.amount:
            raise PaymentError("Paid amount is lower than the invoice amount")

        async with self._uow_factory.transaction() as uow:
            await uow.payments.mark_paid(payment.id, external_id)
            await uow.notifications.enqueue(
                user_id=payment.user_id,
                notification_type=NotificationType.SYSTEM_UPDATE,
                body="payment_success",
                payload={"payment_id": payment.id, "tier": payment.tier},
            )

        if payment.tier is not None:
            await self._subscriptions.grant(
                user_id=payment.user_id,
                tier=SubscriptionTier(payment.tier),
                days=payment.days,
                source=payment.provider.value,
                payment_id=payment.id,
            )
        PAYMENTS_TOTAL.labels(payment.provider.value, PaymentStatus.PAID.value).inc()
        log.info("payment confirmed id={} user={}", payment.id, payment.user_id)

        async with self._uow_factory() as uow:
            refreshed = await uow.payments.get(payment.id)
        return refreshed or payment

    async def fail(self, payload: str, reason: str) -> None:
        async with self._uow_factory.transaction() as uow:
            payment = await uow.payments.by_payload(payload)
            if payment is None or payment.status is PaymentStatus.PAID:
                return
            payment.status = PaymentStatus.FAILED
            payment.failure_reason = reason[:255]
        PAYMENTS_TOTAL.labels("unknown", PaymentStatus.FAILED.value).inc()

    async def refund(self, payment_id: int, *, admin_id: int | None = None) -> Payment:
        """Mark a payment as refunded and revoke what it granted."""
        async with self._uow_factory.transaction() as uow:
            payment = await uow.payments.get(payment_id)
            if payment is None:
                raise NotFoundError("Payment not found")
            if payment.status is not PaymentStatus.PAID:
                raise ConflictError("Only paid payments can be refunded")
            payment.status = PaymentStatus.REFUNDED
            payment.refunded_at = datetime.now(UTC)
            payment.extra = {**(payment.extra or {}), "refunded_by": admin_id}
            user_id = payment.user_id
        await self._subscriptions.revoke(user_id, admin_id=admin_id)
        PAYMENTS_TOTAL.labels(payment.provider.value, PaymentStatus.REFUNDED.value).inc()
        return payment

    async def verify_pending(self, *, limit: int = 50) -> int:
        """Poll pull-based providers (crypto) for pending invoices."""
        confirmed = 0
        async with self._uow_factory() as uow:
            pending = await uow.payments.list_recent(status=PaymentStatus.PENDING, limit=limit)
        for payment in pending:
            if payment.provider is not PaymentProvider.CRYPTO or not payment.external_id:
                continue
            try:
                gateway = self._gateways.get(payment.provider)
                result = await gateway.verify(payment.external_id)
            except Exception as exc:  # pragma: no cover - provider outage
                log.warning("verification failed payment={}: {}", payment.id, exc)
                continue
            if result.success and payment.invoice_payload:
                await self.confirm(
                    payload=payment.invoice_payload,
                    external_id=result.external_id,
                    amount=result.amount or None,
                )
                confirmed += 1
        return confirmed
