"""Application exception hierarchy.

Every failure that the user (or an API client) may legitimately observe is a
subclass of :class:`MediaBotError`.  Each error carries a stable ``code`` used
for translations in the bot and for machine-readable API responses, plus an
HTTP status used by the FastAPI exception handler.

Keeping the hierarchy in the *core* layer lets the domain raise meaningful
errors without importing web or Telegram frameworks.
"""

from __future__ import annotations

from typing import Any


class MediaBotError(Exception):
    """Base class for every expected error in the system."""

    code: str = "internal_error"
    http_status: int = 500
    #: Message shown to end users when no translation is available.
    default_message: str = "Internal error"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = details
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


# --------------------------------------------------------------------------- #
# Validation / input
# --------------------------------------------------------------------------- #
class ValidationError(MediaBotError):
    code = "validation_error"
    http_status = 422
    default_message = "Invalid input"


class UnsupportedUrlError(ValidationError):
    code = "unsupported_url"
    default_message = "This link is not supported"


class UnsafeUrlError(ValidationError):
    code = "unsafe_url"
    default_message = "This link was rejected by the security filter"


class UnsupportedFormatError(ValidationError):
    code = "unsupported_format"
    default_message = "Requested format is not available for this media"


# --------------------------------------------------------------------------- #
# Authentication / authorisation
# --------------------------------------------------------------------------- #
class AuthenticationError(MediaBotError):
    code = "unauthenticated"
    http_status = 401
    default_message = "Authentication required"


class PermissionDeniedError(MediaBotError):
    code = "permission_denied"
    http_status = 403
    default_message = "You are not allowed to perform this action"


class UserBannedError(PermissionDeniedError):
    code = "user_banned"
    default_message = "Your account is banned"


class UserMutedError(PermissionDeniedError):
    code = "user_muted"
    default_message = "Your account is temporarily muted"


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
class NotFoundError(MediaBotError):
    code = "not_found"
    http_status = 404
    default_message = "Resource not found"


class ConflictError(MediaBotError):
    code = "conflict"
    http_status = 409
    default_message = "Conflicting state"


# --------------------------------------------------------------------------- #
# Limits / quotas
# --------------------------------------------------------------------------- #
class LimitExceededError(MediaBotError):
    code = "limit_exceeded"
    http_status = 429
    default_message = "Limit exceeded"


class DailyQuotaExceededError(LimitExceededError):
    code = "daily_quota_exceeded"
    default_message = "Daily download limit reached"


class FileTooLargeError(LimitExceededError):
    code = "file_too_large"
    http_status = 413
    default_message = "File is larger than your plan allows"


class DurationTooLongError(LimitExceededError):
    code = "duration_too_long"
    default_message = "Media is longer than your plan allows"


class QualityNotAllowedError(LimitExceededError):
    code = "quality_not_allowed"
    http_status = 403
    default_message = "This quality requires a higher subscription tier"


class PlatformNotAllowedError(LimitExceededError):
    code = "platform_not_allowed"
    http_status = 403
    default_message = "This platform requires a higher subscription tier"


class RateLimitedError(LimitExceededError):
    code = "rate_limited"
    default_message = "Too many requests, slow down"


class FloodDetectedError(RateLimitedError):
    code = "flood_detected"
    default_message = "Flood detected, you are temporarily blocked"


class InsufficientFundsError(MediaBotError):
    code = "insufficient_funds"
    http_status = 402
    default_message = "Not enough coins on your balance"


# --------------------------------------------------------------------------- #
# Promo codes
# --------------------------------------------------------------------------- #
class PromoCodeError(MediaBotError):
    code = "promo_error"
    http_status = 400
    default_message = "Promo code cannot be applied"


class PromoCodeNotFoundError(PromoCodeError):
    code = "promo_not_found"
    default_message = "Promo code not found"


class PromoCodeExpiredError(PromoCodeError):
    code = "promo_expired"
    default_message = "Promo code has expired"


class PromoCodeExhaustedError(PromoCodeError):
    code = "promo_exhausted"
    default_message = "Promo code activation limit reached"


class PromoCodeAlreadyUsedError(PromoCodeError):
    code = "promo_already_used"
    default_message = "You have already used this promo code"


# --------------------------------------------------------------------------- #
# Downloads / queue
# --------------------------------------------------------------------------- #
class DownloadError(MediaBotError):
    code = "download_failed"
    http_status = 502
    default_message = "Download failed"


class ExtractionError(DownloadError):
    code = "extraction_failed"
    default_message = "Could not read media information"


class GeoRestrictedError(DownloadError):
    code = "geo_restricted"
    default_message = "Media is not available in this region"


class PrivateContentError(DownloadError):
    code = "private_content"
    default_message = "Media is private or requires authorisation"


class ConversionError(DownloadError):
    code = "conversion_failed"
    default_message = "Media conversion failed"


class QueueFullError(MediaBotError):
    code = "queue_full"
    http_status = 503
    default_message = "The queue is full, try again later"


class JobCancelledError(MediaBotError):
    code = "job_cancelled"
    http_status = 409
    default_message = "Job was cancelled"


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #
class PaymentError(MediaBotError):
    code = "payment_failed"
    http_status = 402
    default_message = "Payment failed"


class PaymentProviderUnavailableError(PaymentError):
    code = "payment_provider_unavailable"
    http_status = 503
    default_message = "Payment provider is currently unavailable"
