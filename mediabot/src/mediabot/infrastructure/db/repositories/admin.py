"""Repositories for staff accounts, audit log, settings, statistics and errors."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, func, select, update

from mediabot.domain.enums import AdminRole, AuditAction
from mediabot.infrastructure.db.models.system import (
    Admin,
    AuditLog,
    DailyStatistic,
    ErrorLog,
    Setting,
)
from mediabot.infrastructure.db.repositories.base import BaseRepository


class AdminRepository(BaseRepository[Admin]):
    """Staff accounts for the bot and the web panel."""

    model = Admin

    async def by_username(self, username: str) -> Admin | None:
        return await self.get_by(username=username.strip().lower())

    async def by_telegram_id(self, telegram_id: int) -> Admin | None:
        return await self.get_by(telegram_id=telegram_id)

    async def list_all(self) -> Sequence[Admin]:
        stmt = select(Admin).order_by(Admin.created_at.asc())
        return (await self.session.execute(stmt)).scalars().all()

    async def register_login(self, admin_id: int, ip: str | None) -> None:
        await self.session.execute(
            update(Admin)
            .where(Admin.id == admin_id)
            .values(
                last_login_at=datetime.now(UTC),
                last_login_ip=ip,
                failed_logins=0,
                locked_until=None,
            )
        )

    async def register_failed_login(self, admin_id: int) -> None:
        await self.session.execute(
            update(Admin).where(Admin.id == admin_id).values(failed_logins=Admin.failed_logins + 1)
        )

    async def ensure_root(
        self,
        *,
        username: str,
        password_hash: str,
        telegram_ids: Sequence[int],
    ) -> Admin:
        """Create (or refresh) the bootstrap owner account.

        Idempotent: on every start-up the configured root Telegram ids are
        promoted to owner-level admins so a locked-out operator can recover.
        """
        admin = await self.by_username(username)
        if admin is None:
            admin = Admin(
                username=username.strip().lower(),
                password_hash=password_hash,
                role=AdminRole.OWNER,
                telegram_id=telegram_ids[0] if telegram_ids else None,
                is_active=True,
            )
            self.session.add(admin)
            await self.session.flush()
        for telegram_id in telegram_ids[1:]:
            existing = await self.by_telegram_id(telegram_id)
            if existing is None:
                self.session.add(
                    Admin(
                        username=f"owner_{telegram_id}",
                        password_hash=password_hash,
                        role=AdminRole.OWNER,
                        telegram_id=telegram_id,
                        is_active=True,
                    )
                )
        await self.session.flush()
        return admin


class AuditLogRepository(BaseRepository[AuditLog]):
    """Append-only audit trail."""

    model = AuditLog

    async def record(
        self,
        *,
        action: AuditAction,
        actor_id: int | None,
        actor_name: str = "system",
        target_type: str | None = None,
        target_id: str | int | None = None,
        summary: str = "",
        payload: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            actor_id=actor_id,
            actor_name=actor_name,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            summary=summary[:255],
            payload=payload or {},
            ip_address=ip_address,
            user_agent=(user_agent or "")[:255] or None,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_recent(
        self,
        *,
        action: AuditAction | None = None,
        actor_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditLog]:
        stmt: Select[tuple[AuditLog]] = select(AuditLog)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()


class SettingRepository(BaseRepository[Setting]):
    """Runtime-editable settings."""

    model = Setting

    async def get_value(self, key: str, default: Any = None) -> Any:
        setting = await self.get_by(key=key)
        return setting.value if setting is not None else default

    async def set_value(
        self,
        key: str,
        value: Any,
        *,
        category: str = "general",
        description: str | None = None,
        admin_id: int | None = None,
    ) -> Setting:
        setting = await self.get_by(key=key)
        if setting is None:
            return await self.create(
                key=key,
                value=value,
                category=category,
                description=description,
                updated_by_admin_id=admin_id,
            )
        setting.value = value
        setting.category = category
        if description is not None:
            setting.description = description
        setting.updated_by_admin_id = admin_id
        await self.session.flush()
        return setting

    async def all_settings(self) -> Sequence[Setting]:
        stmt = select(Setting).order_by(Setting.category, Setting.key)
        return (await self.session.execute(stmt)).scalars().all()


class StatisticRepository(BaseRepository[DailyStatistic]):
    """Pre-aggregated daily analytics."""

    model = DailyStatistic

    async def for_day(self, day: date) -> DailyStatistic | None:
        return await self.get_by(day=day)

    async def upsert(self, day: date, **values: Any) -> DailyStatistic:
        row = await self.for_day(day)
        if row is None:
            return await self.create(day=day, **values)
        for key, value in values.items():
            setattr(row, key, value)
        await self.session.flush()
        return row

    async def range(self, start: date, end: date) -> Sequence[DailyStatistic]:
        stmt = (
            select(DailyStatistic)
            .where(DailyStatistic.day >= start, DailyStatistic.day <= end)
            .order_by(DailyStatistic.day.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def totals(self) -> dict[str, int]:
        stmt = select(
            func.coalesce(func.sum(DailyStatistic.downloads_total), 0),
            func.coalesce(func.sum(DailyStatistic.bytes_total), 0),
            func.coalesce(func.sum(DailyStatistic.revenue), 0),
            func.coalesce(func.sum(DailyStatistic.new_users), 0),
        )
        row = (await self.session.execute(stmt)).one()
        return {
            "downloads": int(row[0]),
            "bytes": int(row[1]),
            "revenue": int(row[2]),
            "new_users": int(row[3]),
        }


class ErrorLogRepository(BaseRepository[ErrorLog]):
    """Searchable error records for the admin panel."""

    model = ErrorLog

    async def record(
        self,
        *,
        code: str,
        message: str,
        component: str = "bot",
        user_id: int | None = None,
        download_id: int | None = None,
        url: str | None = None,
        traceback: str | None = None,
    ) -> ErrorLog:
        return await self.create(
            code=code[:64],
            message=message[:4000],
            component=component,
            user_id=user_id,
            download_id=download_id,
            url=url,
            traceback=traceback,
        )

    async def list_recent(
        self,
        *,
        code: str | None = None,
        resolved: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ErrorLog]:
        stmt: Select[tuple[ErrorLog]] = select(ErrorLog)
        if code:
            stmt = stmt.where(ErrorLog.code == code)
        if resolved is not None:
            stmt = stmt.where(ErrorLog.resolved.is_(resolved))
        stmt = stmt.order_by(ErrorLog.created_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def resolve(self, error_id: int) -> None:
        await self.session.execute(
            update(ErrorLog).where(ErrorLog.id == error_id).values(resolved=True)
        )
