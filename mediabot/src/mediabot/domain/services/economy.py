"""Coin economy, daily bonuses, referral rewards and achievements.

Pure calculators.  Persistence (wallets, transactions, achievement rows) is the
responsibility of the application layer; everything here is deterministic and
covered by unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from mediabot.core.config import EconomySettings
from mediabot.domain.enums import AchievementCode, SubscriptionTier
from mediabot.domain.policies import ACHIEVEMENTS, COIN_SHOP
from mediabot.domain.value_objects import AchievementDefinition


@dataclass(frozen=True, slots=True)
class DailyBonusResult:
    """Outcome of a daily-bonus claim attempt."""

    granted: bool
    coins: int
    streak: int
    next_available_at: datetime
    streak_broken: bool = False


@dataclass(frozen=True, slots=True)
class ReferralReward:
    """Reward pair produced by a successful referral."""

    inviter_coins: int
    invitee_coins: int
    inviter_premium_days: int


@dataclass(frozen=True, slots=True)
class ShopPurchase:
    """A validated coin-shop purchase, ready to be applied."""

    item: str
    price: int
    tier: SubscriptionTier | None
    days: int
    extra_downloads: int
    extra_file_size_bytes: int
    no_ads_days: int


class EconomyService:
    """Daily bonuses, referral payouts and the coin shop."""

    def __init__(self, settings: EconomySettings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Daily bonus
    # ------------------------------------------------------------------ #
    def claim_daily_bonus(
        self,
        *,
        last_claim_at: datetime | None,
        current_streak: int,
        now: datetime | None = None,
    ) -> DailyBonusResult:
        """Compute the reward for a daily-bonus claim.

        The streak increases when the previous claim happened *yesterday*,
        resets when a day was skipped, and the claim is refused when it already
        happened today.
        """
        now = now or datetime.now(UTC)
        today = now.date()
        tomorrow_start = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC)

        if last_claim_at is not None:
            last_date: date = last_claim_at.astimezone(UTC).date()
            if last_date == today:
                return DailyBonusResult(
                    granted=False,
                    coins=0,
                    streak=current_streak,
                    next_available_at=tomorrow_start,
                )
            streak_broken = last_date < today - timedelta(days=1)
            streak = 1 if streak_broken else current_streak + 1
        else:
            streak_broken = False
            streak = 1

        coins = min(
            self._settings.daily_bonus_coins
            + (streak - 1) * self._settings.daily_streak_step_coins,
            self._settings.daily_streak_max_coins,
        )
        return DailyBonusResult(
            granted=True,
            coins=coins,
            streak=streak,
            next_available_at=tomorrow_start,
            streak_broken=streak_broken,
        )

    def daily_calendar(self, streak: int, days: int = 7) -> tuple[tuple[int, int, bool], ...]:
        """Render the reward calendar as ``(day, coins, already_claimed)``."""
        calendar: list[tuple[int, int, bool]] = []
        cycle_start = ((streak - 1) // days) * days if streak else 0
        for offset in range(days):
            day_number = cycle_start + offset + 1
            coins = min(
                self._settings.daily_bonus_coins
                + (day_number - 1) * self._settings.daily_streak_step_coins,
                self._settings.daily_streak_max_coins,
            )
            calendar.append((day_number, coins, day_number <= streak))
        return tuple(calendar)

    # ------------------------------------------------------------------ #
    # Referrals
    # ------------------------------------------------------------------ #
    def referral_reward(self, inviter_referral_count: int) -> ReferralReward:
        """Compute the payout for the ``n``-th successful referral.

        Every fifth invited friend also grants a free premium day, which makes
        the programme feel progressive without an unbounded cost.
        """
        premium_days = (
            self._settings.referral_premium_days_per_5
            if inviter_referral_count > 0 and inviter_referral_count % 5 == 0
            else 0
        )
        return ReferralReward(
            inviter_coins=self._settings.referral_bonus_coins,
            invitee_coins=self._settings.referral_invitee_coins,
            inviter_premium_days=premium_days,
        )

    def referral_code(self, user_id: int) -> str:
        """Deterministic, human-friendly referral code for a Telegram user id."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        value = user_id
        digits: list[str] = []
        while value:
            value, remainder = divmod(value, len(alphabet))
            digits.append(alphabet[remainder])
        return "REF" + "".join(reversed(digits or ["2"])).rjust(6, "2")

    # ------------------------------------------------------------------ #
    # Coin shop
    # ------------------------------------------------------------------ #
    def price_of(self, item: str) -> int | None:
        entry = COIN_SHOP.get(item)
        return entry[0] if entry else None

    def resolve_purchase(self, item: str) -> ShopPurchase | None:
        """Translate a shop item code into a structured purchase."""
        entry = COIN_SHOP.get(item)
        if entry is None:
            return None
        price, payload = entry
        tier_raw = payload.get("tier")
        return ShopPurchase(
            item=item,
            price=price,
            tier=SubscriptionTier(tier_raw) if isinstance(tier_raw, str) else None,
            days=int(payload.get("days", 0) or 0),
            extra_downloads=int(payload.get("extra_downloads", 0) or 0),
            extra_file_size_bytes=int(payload.get("extra_file_size_bytes", 0) or 0),
            no_ads_days=int(payload.get("no_ads_days", 0) or 0),
        )

    def coins_for_premium_days(self, days: int) -> int:
        """Price of ``days`` premium days when paying with coins."""
        return max(days, 1) * self._settings.coins_per_premium_day

    def coins_for_extra_downloads(self, count: int) -> int:
        return max(count, 1) * self._settings.coins_per_extra_download


class AchievementService:
    """Evaluate which achievements a set of user metrics unlocks."""

    def evaluate(
        self,
        metrics: dict[str, int],
        already_unlocked: set[str] | frozenset[str],
    ) -> tuple[AchievementDefinition, ...]:
        """Return the achievements unlocked by ``metrics`` and not yet granted."""
        unlocked: list[AchievementDefinition] = []
        for code, definition in ACHIEVEMENTS.items():
            if code.value in already_unlocked:
                continue
            if metrics.get(definition.metric, 0) >= definition.threshold:
                unlocked.append(definition)
        return tuple(unlocked)

    def definition(self, code: AchievementCode | str) -> AchievementDefinition | None:
        key = AchievementCode(code) if isinstance(code, str) else code
        return ACHIEVEMENTS.get(key)

    def progress(self, metrics: dict[str, int]) -> tuple[tuple[AchievementDefinition, int], ...]:
        """Pair every achievement with the user's current metric value."""
        return tuple(
            (definition, metrics.get(definition.metric, 0)) for definition in ACHIEVEMENTS.values()
        )

    def all_definitions(self) -> tuple[AchievementDefinition, ...]:
        return tuple(ACHIEVEMENTS.values())
