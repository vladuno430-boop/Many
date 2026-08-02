"""ORM model registry.

Importing this package registers every mapper on :class:`Base.metadata`, which
is what Alembic's autogenerate and the test fixtures rely on.
"""

from mediabot.infrastructure.db.base import Base
from mediabot.infrastructure.db.models.billing import (
    Payment,
    PromoActivation,
    PromoCode,
    Subscription,
)
from mediabot.infrastructure.db.models.download import (
    Download,
    DownloadHistory,
    DownloadQueueEntry,
    Favorite,
    FavoriteCollection,
)
from mediabot.infrastructure.db.models.system import (
    Admin,
    AuditLog,
    DailyStatistic,
    ErrorLog,
    Notification,
    Setting,
)
from mediabot.infrastructure.db.models.user import (
    Referral,
    SupportedLanguage,
    Transaction,
    User,
    UserAchievement,
    UserLimit,
    Wallet,
)

__all__ = [
    "Admin",
    "AuditLog",
    "Base",
    "DailyStatistic",
    "Download",
    "DownloadHistory",
    "DownloadQueueEntry",
    "ErrorLog",
    "Favorite",
    "FavoriteCollection",
    "Notification",
    "Payment",
    "PromoActivation",
    "PromoCode",
    "Referral",
    "Setting",
    "Subscription",
    "SupportedLanguage",
    "Transaction",
    "User",
    "UserAchievement",
    "UserLimit",
    "Wallet",
]
