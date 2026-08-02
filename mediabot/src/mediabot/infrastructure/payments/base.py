"""Payment provider abstraction.

Architecture note
-----------------
Each rail (Telegram Stars, card acquirer, crypto gateway, internal balance)
implements the same :class:`PaymentGateway` protocol.  The application layer
picks a gateway by :class:`PaymentProvider` and never learns provider details,
so adding a new acquirer means adding one file and one registry entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from mediabot.domain.enums import PaymentProvider, SubscriptionTier


@dataclass(frozen=True, slots=True)
class InvoiceRequest:
    """Everything needed to create an invoice."""

    user_id: int
    tier: SubscriptionTier
    days: int
    amount: int
    currency: str
    title: str
    description: str
    payload: str
    language: str = "en"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Invoice:
    """Provider response describing how the user should pay."""

    provider: PaymentProvider
    payload: str
    amount: int
    currency: str
    #: Ready-to-open payment URL (crypto/card) — ``None`` for Telegram Stars,
    #: where the bot sends a native invoice message instead.
    url: str | None = None
    external_id: str | None = None
    provider_token: str | None = None
    prices: tuple[tuple[str, int], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """Normalised outcome of a payment verification."""

    success: bool
    external_id: str | None = None
    amount: int = 0
    currency: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaymentGateway(Protocol):
    """Contract every payment provider must satisfy."""

    provider: PaymentProvider

    @property
    def enabled(self) -> bool:
        """Whether the gateway is configured and may be offered to users."""
        ...

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        """Create an invoice for ``request``."""
        ...

    async def verify(self, external_id: str) -> PaymentResult:
        """Check the current state of a payment with the provider."""
        ...
