"""Promo-code validation and reward calculation (pure domain logic)."""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime

from mediabot.core.exceptions import (
    PromoCodeAlreadyUsedError,
    PromoCodeExhaustedError,
    PromoCodeExpiredError,
    PromoCodeNotFoundError,
)
from mediabot.domain.enums import PromoStatus, PromoType, SubscriptionTier

_CODE_ALPHABET = string.ascii_uppercase + string.digits


@dataclass(frozen=True, slots=True)
class PromoSnapshot:
    """The subset of a promo code needed to decide whether it may be used."""

    code: str
    promo_type: PromoType
    status: PromoStatus
    value: int
    max_activations: int | None
    activations_used: int
    per_user_limit: int
    starts_at: datetime | None
    expires_at: datetime | None
    min_tier: SubscriptionTier | None
    new_users_only: bool


@dataclass(frozen=True, slots=True)
class PromoReward:
    """What the user receives once a promo code is successfully redeemed."""

    coins: int = 0
    premium_days: int = 0
    vip_days: int = 0
    lifetime: bool = False
    percent_discount: int = 0
    fixed_discount: int = 0
    extra_daily_downloads: int = 0

    @property
    def is_discount(self) -> bool:
        return bool(self.percent_discount or self.fixed_discount)

    @property
    def is_empty(self) -> bool:
        return self == PromoReward()


class PromoService:
    """Validate promo codes and translate them into concrete rewards."""

    def generate_code(self, length: int = 10, prefix: str = "") -> str:
        """Generate a cryptographically random, human-readable promo code."""
        body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
        return f"{prefix.upper()}{body}" if prefix else body

    def normalize(self, code: str) -> str:
        """Codes are compared case-insensitively and without surrounding space."""
        return (code or "").strip().upper()

    def validate(
        self,
        promo: PromoSnapshot | None,
        *,
        user_activations: int,
        user_tier: SubscriptionTier,
        user_is_new: bool,
        now: datetime | None = None,
    ) -> None:
        """Raise a domain error when the code cannot be redeemed by this user."""
        now = now or datetime.now(UTC)
        if promo is None:
            raise PromoCodeNotFoundError()
        if promo.status is PromoStatus.DISABLED:
            raise PromoCodeNotFoundError()
        if promo.status is PromoStatus.EXPIRED:
            raise PromoCodeExpiredError()
        if promo.status is PromoStatus.EXHAUSTED:
            raise PromoCodeExhaustedError()
        if promo.starts_at and promo.starts_at > now:
            raise PromoCodeNotFoundError("Promo code is not active yet")
        if promo.expires_at and promo.expires_at <= now:
            raise PromoCodeExpiredError()
        if promo.max_activations is not None and promo.activations_used >= promo.max_activations:
            raise PromoCodeExhaustedError()
        if user_activations >= promo.per_user_limit:
            raise PromoCodeAlreadyUsedError()
        if promo.new_users_only and not user_is_new:
            raise PromoCodeNotFoundError("Promo code is for new users only")
        if promo.min_tier is not None and user_tier < promo.min_tier:
            raise PromoCodeNotFoundError("Promo code requires a higher subscription tier")

    def reward_for(self, promo: PromoSnapshot) -> PromoReward:
        """Map a promo type + value onto the reward it grants."""
        match promo.promo_type:
            case PromoType.COINS:
                return PromoReward(coins=promo.value)
            case PromoType.PREMIUM_DAYS:
                return PromoReward(premium_days=promo.value)
            case PromoType.VIP_DAYS:
                return PromoReward(vip_days=promo.value)
            case PromoType.LIFETIME:
                return PromoReward(lifetime=True)
            case PromoType.PERCENT_DISCOUNT:
                return PromoReward(percent_discount=min(promo.value, 100))
            case PromoType.FIXED_DISCOUNT:
                return PromoReward(fixed_discount=promo.value)
            case PromoType.LIMIT_BOOST:
                return PromoReward(extra_daily_downloads=promo.value)
        return PromoReward()

    def apply_discount(self, price: int, reward: PromoReward) -> int:
        """Apply a discount reward to ``price``; the result is never negative."""
        if reward.percent_discount:
            price = price - price * reward.percent_discount // 100
        if reward.fixed_discount:
            price -= reward.fixed_discount
        return max(price, 0)

    def next_status(self, promo: PromoSnapshot, *, now: datetime | None = None) -> PromoStatus:
        """Recompute the administrative status after an activation."""
        now = now or datetime.now(UTC)
        if promo.status is PromoStatus.DISABLED:
            return PromoStatus.DISABLED
        if promo.expires_at and promo.expires_at <= now:
            return PromoStatus.EXPIRED
        if promo.max_activations is not None and promo.activations_used >= promo.max_activations:
            return PromoStatus.EXHAUSTED
        return PromoStatus.ACTIVE
