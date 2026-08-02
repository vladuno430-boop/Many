"""Concrete payment gateways: Telegram Stars, card acquirer, crypto, balance."""

from __future__ import annotations

from typing import Any

import aiohttp

from mediabot.core.config import PaymentSettings
from mediabot.core.exceptions import PaymentError, PaymentProviderUnavailableError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import PaymentProvider
from mediabot.infrastructure.payments.base import (
    Invoice,
    InvoiceRequest,
    PaymentGateway,
    PaymentResult,
)

log = get_logger(LogChannel.PAYMENTS, component="gateway")


class TelegramStarsGateway:
    """Telegram Stars (``XTR``).

    Stars invoices are sent by the bot itself, so ``create_invoice`` only
    prepares the payload; the actual message is produced by the bot layer.
    Verification is push-based (``successful_payment`` update), therefore
    :meth:`verify` simply reports that no polling is required.
    """

    provider = PaymentProvider.TELEGRAM_STARS

    def __init__(self, settings: PaymentSettings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.stars_enabled

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        if not self.enabled:
            raise PaymentProviderUnavailableError("Telegram Stars are disabled")
        # For Stars the amount is expressed directly in stars, not in cents.
        return Invoice(
            provider=self.provider,
            payload=request.payload,
            amount=request.amount,
            currency="XTR",
            provider_token="",  # Stars require an empty provider token
            prices=((request.title, request.amount),),
        )

    async def verify(self, external_id: str) -> PaymentResult:
        return PaymentResult(
            success=True,
            external_id=external_id,
            message="Telegram Stars payments are confirmed by the payment update",
        )


class CardGateway:
    """Card payments through a Telegram payment provider token.

    Telegram acts as the integration surface for acquirers (YooKassa, Stripe,
    …): the bot sends an invoice with a provider token and Telegram forwards a
    ``successful_payment`` update once the charge settles.
    """

    provider = PaymentProvider.CARD

    def __init__(self, settings: PaymentSettings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.card_enabled and bool(
            self._settings.card_provider_token.get_secret_value()
        )

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        if not self.enabled:
            raise PaymentProviderUnavailableError("Card payments are not configured")
        # Telegram expects the smallest currency unit (kopeks/cents).
        minor_amount = request.amount * 100
        return Invoice(
            provider=self.provider,
            payload=request.payload,
            amount=minor_amount,
            currency=request.currency,
            provider_token=self._settings.card_provider_token.get_secret_value(),
            prices=((request.title, minor_amount),),
        )

    async def verify(self, external_id: str) -> PaymentResult:
        return PaymentResult(
            success=True,
            external_id=external_id,
            message="Card payments are confirmed by the payment update",
        )


class CryptoGateway:
    """Crypto payments through a Crypto Pay compatible HTTP gateway.

    The gateway is polled (or notified) and returns an invoice URL that the bot
    shows as a button.  Any provider exposing ``createInvoice``/``getInvoices``
    can be plugged in by changing ``PAYMENTS__CRYPTO_API_URL``.
    """

    provider = PaymentProvider.CRYPTO

    def __init__(self, settings: PaymentSettings, session_factory: Any = None) -> None:
        self._settings = settings
        self._session_factory = session_factory or aiohttp.ClientSession

    @property
    def enabled(self) -> bool:
        return self._settings.crypto_enabled and bool(
            self._settings.crypto_api_token.get_secret_value()
        )

    def _headers(self) -> dict[str, str]:
        return {"Crypto-Pay-API-Token": self._settings.crypto_api_token.get_secret_value()}

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        if not self.enabled:
            raise PaymentProviderUnavailableError("Crypto payments are not configured")
        payload = {
            "asset": self._settings.crypto_asset,
            "amount": str(request.amount),
            "description": request.description[:1024],
            "payload": request.payload,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        try:
            async with (
                self._session_factory() as session,
                session.post(
                    f"{self._settings.crypto_api_url}/createInvoice",
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response,
            ):
                data = await response.json()
        except aiohttp.ClientError as exc:
            log.error("crypto gateway unreachable: {}", exc)
            raise PaymentProviderUnavailableError(str(exc)) from exc

        if not data.get("ok"):
            raise PaymentError(str(data.get("error") or "Crypto gateway rejected the invoice"))
        result = data.get("result") or {}
        return Invoice(
            provider=self.provider,
            payload=request.payload,
            amount=request.amount,
            currency=self._settings.crypto_asset,
            url=result.get("pay_url") or result.get("bot_invoice_url"),
            external_id=str(result.get("invoice_id") or ""),
            raw=result,
        )

    async def verify(self, external_id: str) -> PaymentResult:
        if not self.enabled:
            raise PaymentProviderUnavailableError("Crypto payments are not configured")
        try:
            async with (
                self._session_factory() as session,
                session.get(
                    f"{self._settings.crypto_api_url}/getInvoices",
                    params={"invoice_ids": external_id},
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response,
            ):
                data = await response.json()
        except aiohttp.ClientError as exc:
            raise PaymentProviderUnavailableError(str(exc)) from exc

        items = ((data.get("result") or {}).get("items")) or []
        if not items:
            return PaymentResult(success=False, external_id=external_id, message="not found")
        invoice = items[0]
        paid = invoice.get("status") == "paid"
        return PaymentResult(
            success=paid,
            external_id=external_id,
            amount=int(float(invoice.get("amount") or 0)),
            currency=str(invoice.get("asset") or ""),
            message=str(invoice.get("status") or ""),
            raw=invoice,
        )


class BalanceGateway:
    """Payments settled from the internal coin balance.

    Always "available": the wallet service performs the actual debit, this
    gateway only produces a uniform invoice object so the checkout flow is
    identical across rails.
    """

    provider = PaymentProvider.BALANCE

    def __init__(self, settings: PaymentSettings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return True

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        return Invoice(
            provider=self.provider,
            payload=request.payload,
            amount=request.amount,
            currency="COINS",
        )

    async def verify(self, external_id: str) -> PaymentResult:
        return PaymentResult(success=True, external_id=external_id, message="settled internally")


class PaymentGatewayRegistry:
    """Look up gateways by provider and list the ones offered to users."""

    def __init__(self, settings: PaymentSettings) -> None:
        self._gateways: dict[PaymentProvider, PaymentGateway] = {
            PaymentProvider.TELEGRAM_STARS: TelegramStarsGateway(settings),
            PaymentProvider.CARD: CardGateway(settings),
            PaymentProvider.CRYPTO: CryptoGateway(settings),
            PaymentProvider.BALANCE: BalanceGateway(settings),
        }

    def get(self, provider: PaymentProvider) -> PaymentGateway:
        gateway = self._gateways.get(provider)
        if gateway is None:  # pragma: no cover - defensive
            raise PaymentProviderUnavailableError(f"Unknown provider: {provider}")
        if not gateway.enabled:
            raise PaymentProviderUnavailableError(f"{provider.value} is disabled")
        return gateway

    def available(self) -> tuple[PaymentProvider, ...]:
        return tuple(provider for provider, gateway in self._gateways.items() if gateway.enabled)
