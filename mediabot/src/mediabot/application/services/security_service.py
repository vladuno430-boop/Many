"""Abuse prevention: rate limiting, flood control, CAPTCHA and bot detection."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mediabot.core.config import Settings
from mediabot.core.exceptions import FloodDetectedError, RateLimitedError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.infrastructure.cache.redis_cache import (
    NS_CAPTCHA,
    CacheService,
    RateLimiter,
)
from mediabot.infrastructure.db.session import UnitOfWorkFactory
from mediabot.infrastructure.metrics import RATE_LIMIT_HITS

log = get_logger(LogChannel.SECURITY, component="security_service")

#: How long a solved CAPTCHA is trusted.
CAPTCHA_TRUST_HOURS = 24
#: Number of answer options presented to the user.
CAPTCHA_OPTIONS = 4


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    """A simple arithmetic challenge with shuffled answer options."""

    question: str
    answer: int
    options: tuple[int, ...]


class SecurityService:
    """Guards the bot against spam, floods and automated abuse.

    Layers, cheapest first:

    1. **Flood control** — burst detection in a short window with a temporary
       ban; catches scripted hammering immediately.
    2. **Rate limiting** — steady-state per-minute budget per user.
    3. **CAPTCHA** — required after repeated violations, so a human can always
       recover while a script cannot.
    """

    def __init__(
        self,
        *,
        limiter: RateLimiter,
        cache: CacheService,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
    ) -> None:
        self._limiter = limiter
        self._cache = cache
        self._uow_factory = uow_factory
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #
    async def check(self, user_id: int, *, scope: str = "message", cost: int = 1) -> None:
        """Raise when the user exceeds the flood or rate budget."""
        security = self._settings.security
        identity = f"{scope}:{user_id}"

        if await self._limiter.check_flood(
            identity,
            threshold=security.flood_threshold,
            window_seconds=security.flood_window_seconds,
            ban_seconds=security.flood_ban_seconds,
        ):
            RATE_LIMIT_HITS.labels("flood").inc()
            seconds = await self._limiter.ban_seconds_left(identity)
            await self._register_violation(user_id)
            raise FloodDetectedError(
                "Too many requests in a short time",
                retry_after=seconds or security.flood_ban_seconds,
            )

        allowed = True
        remaining = 0
        for _ in range(max(cost, 1)):
            allowed, remaining = await self._limiter.hit(
                identity, limit=security.rate_limit_per_minute
            )
        if not allowed:
            RATE_LIMIT_HITS.labels(scope).inc()
            raise RateLimitedError("Rate limit exceeded", remaining=remaining, retry_after=60)

    async def _register_violation(self, user_id: int) -> int:
        """Count a violation; the counter drives the CAPTCHA requirement."""
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                return 0
            user.violations += 1
            return int(user.violations)

    async def reset(self, user_id: int, *, scope: str = "message") -> None:
        await self._limiter.reset(f"{scope}:{user_id}")

    # ------------------------------------------------------------------ #
    # CAPTCHA
    # ------------------------------------------------------------------ #
    async def captcha_required(self, user_id: int) -> bool:
        """Whether the user must solve a CAPTCHA before continuing."""
        if not self._settings.security.captcha_enabled:
            return False
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                return False
            if user.violations < self._settings.security.captcha_after_violations:
                return False
            if user.captcha_passed_at is None:
                return True
            passed_at = user.captcha_passed_at
            if passed_at.tzinfo is None:
                passed_at = passed_at.replace(tzinfo=UTC)
            return datetime.now(UTC) - passed_at > timedelta(hours=CAPTCHA_TRUST_HOURS)

    async def issue_captcha(self, user_id: int) -> CaptchaChallenge:
        """Create and store a challenge for ``user_id``."""
        left = secrets.randbelow(9) + 1
        right = secrets.randbelow(9) + 1
        answer = left + right
        options = {answer}
        while len(options) < CAPTCHA_OPTIONS:
            candidate = answer + secrets.randbelow(9) - 4
            if candidate > 0:
                options.add(candidate)
        shuffled = list(options)
        secrets.SystemRandom().shuffle(shuffled)

        await self._cache.set(
            CacheService.key(NS_CAPTCHA, user_id),
            {"answer": answer},
            ttl=600,
        )
        return CaptchaChallenge(
            question=f"{left} + {right} = ?",
            answer=answer,
            options=tuple(shuffled),
        )

    async def verify_captcha(self, user_id: int, answer: int) -> bool:
        """Validate an answer; a success clears the violation counter."""
        key = CacheService.key(NS_CAPTCHA, user_id)
        stored = await self._cache.get(key)
        if not stored or int(stored.get("answer", -1)) != int(answer):
            log.warning("captcha failed for user={}", user_id)
            return False
        await self._cache.delete(key)
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is not None:
                user.captcha_passed_at = datetime.now(UTC)
                user.violations = 0
        await self._limiter.reset(f"message:{user_id}")
        log.info("captcha solved by user={}", user_id)
        return True

    # ------------------------------------------------------------------ #
    # Bot detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def looks_automated(
        *,
        is_bot: bool,
        username: str | None,
        message_interval_ms: int | None,
    ) -> bool:
        """Heuristics that flag obviously automated clients.

        Telegram already refuses bot-to-bot messages, but self-hosted clients
        and userbots do get through; sub-200 ms reaction times are a reliable
        signal of scripted interaction.
        """
        if is_bot:
            return True
        if message_interval_ms is not None and message_interval_ms < 200:
            return True
        return bool(username and username.lower().endswith("_bot"))

    async def note_activity(self, user_id: int) -> int | None:
        """Record activity and return the gap since the previous event in ms."""
        key = CacheService.key("activity", user_id)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        previous = await self._cache.get(key)
        await self._cache.set(key, now_ms, ttl=300)
        if not isinstance(previous, int):
            return None
        return now_ms - previous
