"""Async engine, session factory and Unit of Work.

Architecture note
-----------------
Application services never create sessions themselves; they receive a
:class:`UnitOfWork` that owns the transaction boundary and exposes every
repository.  That keeps "one request = one transaction" enforceable and makes
services trivially testable against SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mediabot.core.config import DatabaseSettings
from mediabot.infrastructure.db.repositories.admin import (
    AdminRepository,
    AuditLogRepository,
    ErrorLogRepository,
    SettingRepository,
    StatisticRepository,
)
from mediabot.infrastructure.db.repositories.billing import (
    PaymentRepository,
    PromoActivationRepository,
    PromoCodeRepository,
    SubscriptionRepository,
)
from mediabot.infrastructure.db.repositories.download import (
    DownloadRepository,
    FavoriteRepository,
    HistoryRepository,
    QueueRepository,
)
from mediabot.infrastructure.db.repositories.notification import NotificationRepository
from mediabot.infrastructure.db.repositories.user import (
    AchievementRepository,
    LanguageRepository,
    ReferralRepository,
    UserLimitRepository,
    UserRepository,
    WalletRepository,
)


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Create the async engine.

    ``pool_pre_ping`` protects against connections dropped by PgBouncer or by
    an idle-timeout on the database side; SQLite (tests) gets no pool options
    because it does not support them.
    """
    if settings.async_dsn.startswith("sqlite"):
        engine = create_async_engine(
            settings.async_dsn,
            echo=settings.echo,
            future=True,
            connect_args={"timeout": 30},
        )
        _apply_sqlite_pragmas(engine)
        return engine
    return create_async_engine(
        settings.async_dsn,
        echo=settings.echo,
        future=True,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_recycle=settings.pool_recycle_seconds,
        pool_pre_ping=True,
    )


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Configure SQLite for concurrent, referentially-correct operation.

    * ``journal_mode=WAL`` lets the bot read while a download job writes, which
      matters because the standalone mode shares one database between the
      handlers and the inline worker.
    * ``foreign_keys=ON`` is off by default in SQLite; without it the
      ``ON DELETE CASCADE`` rules the schema relies on would silently not fire.
    * ``busy_timeout`` replaces "database is locked" errors with a short wait.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with ``expire_on_commit=False``.

    Objects returned by services stay usable after ``commit()``, which avoids
    accidental lazy-load round-trips (and ``MissingGreenlet`` errors) in the
    presentation layer.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


class UnitOfWork:
    """Transaction boundary aggregating every repository.

    Usage::

        async with uow_factory() as uow:
            user = await uow.users.get_or_create(...)
            await uow.commit()

    Leaving the context without an explicit ``commit()`` rolls back, so a
    failure can never persist a half-written state.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        session = self._session
        self.users = UserRepository(session)
        self.limits = UserLimitRepository(session)
        self.wallets = WalletRepository(session)
        self.referrals = ReferralRepository(session)
        self.achievements = AchievementRepository(session)
        self.languages = LanguageRepository(session)
        self.downloads = DownloadRepository(session)
        self.queue = QueueRepository(session)
        self.history = HistoryRepository(session)
        self.favorites = FavoriteRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        self.payments = PaymentRepository(session)
        self.promos = PromoCodeRepository(session)
        self.promo_activations = PromoActivationRepository(session)
        self.notifications = NotificationRepository(session)
        self.admins = AdminRepository(session)
        self.audit = AuditLogRepository(session)
        self.settings = SettingRepository(session)
        self.statistics = StatisticRepository(session)
        self.errors = ErrorLogRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if exc_type is not None:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:  # pragma: no cover - programming error
            raise RuntimeError("UnitOfWork used outside of its context manager")
        return self._session

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()


class UnitOfWorkFactory:
    """Callable factory so consumers depend on an abstraction, not on a maker."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[UnitOfWork]:
        """Open a UoW that commits automatically when the block succeeds."""
        async with UnitOfWork(self._session_factory) as uow:
            yield uow
            await uow.commit()
