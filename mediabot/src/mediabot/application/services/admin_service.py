"""Administrative operations: moderation, grants, promo management, queue control."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mediabot.application.dto import QueueSnapshot
from mediabot.application.services.user_service import SubscriptionService
from mediabot.core.config import Settings
from mediabot.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import hash_password, verify_password
from mediabot.domain.enums import (
    AdminRole,
    AuditAction,
    DownloadStatus,
    PromoStatus,
    PromoType,
    SubscriptionTier,
    TransactionReason,
    UserStatus,
)
from mediabot.domain.services.promo import PromoService
from mediabot.infrastructure.db.models.billing import PromoCode
from mediabot.infrastructure.db.models.system import Admin
from mediabot.infrastructure.db.models.user import User
from mediabot.infrastructure.db.session import UnitOfWorkFactory
from mediabot.infrastructure.queue.dispatcher import TaskDispatcher

log = get_logger(LogChannel.ADMIN, component="admin_service")


class AdminService:
    """Every privileged operation, each one written to the audit log.

    Authorisation is enforced with :meth:`require_role`; callers pass the
    acting admin's role, which the presentation layer resolved beforehand.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        subscriptions: SubscriptionService,
        promo_service: PromoService,
        dispatcher: TaskDispatcher,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._subscriptions = subscriptions
        self._promos = promo_service
        self._dispatcher = dispatcher
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Authentication / authorisation
    # ------------------------------------------------------------------ #
    @staticmethod
    def require_role(actor: AdminRole, required: AdminRole) -> None:
        if not actor.can(required):
            raise PermissionDeniedError(f"{required.value} role is required")

    async def authenticate(
        self,
        username: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> Admin:
        """Verify web-panel credentials with lockout after repeated failures."""
        async with self._uow_factory.transaction() as uow:
            admin = await uow.admins.by_username(username)
            now = datetime.now(UTC)
            if admin is None or not admin.is_active:
                await uow.audit.record(
                    action=AuditAction.LOGIN_FAILED,
                    actor_id=None,
                    actor_name=username[:64],
                    summary="unknown account",
                    ip_address=ip,
                    user_agent=user_agent,
                )
                raise PermissionDeniedError("Invalid credentials")
            if admin.locked_until and admin.locked_until > now:
                raise PermissionDeniedError("Account temporarily locked")
            if not verify_password(password, admin.password_hash):
                admin.failed_logins += 1
                if admin.failed_logins >= 5:
                    admin.locked_until = now + timedelta(minutes=15)
                await uow.audit.record(
                    action=AuditAction.LOGIN_FAILED,
                    actor_id=admin.id,
                    actor_name=admin.username,
                    summary="bad password",
                    ip_address=ip,
                    user_agent=user_agent,
                )
                raise PermissionDeniedError("Invalid credentials")

            admin.failed_logins = 0
            admin.locked_until = None
            admin.last_login_at = now
            admin.last_login_ip = ip
            await uow.audit.record(
                action=AuditAction.LOGIN,
                actor_id=admin.id,
                actor_name=admin.username,
                summary="web panel login",
                ip_address=ip,
                user_agent=user_agent,
            )
            return admin

    async def resolve_role(self, telegram_id: int) -> AdminRole | None:
        """Role of a Telegram user, or ``None`` when they are not staff."""
        if telegram_id in self._settings.telegram.root_admin_ids:
            return AdminRole.OWNER
        async with self._uow_factory() as uow:
            admin = await uow.admins.by_telegram_id(telegram_id)
            if admin is None or not admin.is_active:
                return None
            return AdminRole(admin.role)

    async def ensure_bootstrap_admin(self) -> None:
        """Create the configured owner account on first start-up."""
        async with self._uow_factory.transaction() as uow:
            await uow.admins.ensure_root(
                username=self._settings.api.admin_username,
                password_hash=hash_password(self._settings.api.admin_password.get_secret_value()),
                telegram_ids=list(self._settings.telegram.root_admin_ids),
            )
            await uow.languages.ensure_defaults()

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #
    async def find_users(self, query: str, *, limit: int = 25, offset: int = 0) -> list[User]:
        async with self._uow_factory() as uow:
            if not query.strip():
                return list(await uow.users.list_filtered(limit=limit, offset=offset))
            return list(await uow.users.search(query, limit=limit, offset=offset))

    async def user_details(self, user_id: int) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            wallet = await uow.wallets.for_user(user_id)
            subscription = await uow.subscriptions.active_for_user(user_id)
            limits = await uow.limits.for_user(user_id)
            downloads = await uow.downloads.list_for_user(user_id, limit=10)
            payments = await uow.payments.list_for_user(user_id, limit=10)
            return {
                "user": user,
                "wallet": wallet,
                "subscription": subscription,
                "limits": limits,
                "recent_downloads": list(downloads),
                "recent_payments": list(payments),
            }

    async def ban(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        user_id: int,
        reason: str,
        days: int | None = None,
    ) -> None:
        self.require_role(actor, AdminRole.MODERATOR)
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            user.status = UserStatus.BANNED
            user.ban_reason = reason[:255]
            user.banned_until = datetime.now(UTC) + timedelta(days=days) if days else None
            await uow.audit.record(
                action=AuditAction.USER_BAN,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
                summary=reason[:255],
                payload={"days": days},
            )
        log.warning("user {} banned by {} reason={}", user_id, actor_id, reason)

    async def unban(self, *, actor: AdminRole, actor_id: int, user_id: int) -> None:
        self.require_role(actor, AdminRole.MODERATOR)
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            user.status = UserStatus.ACTIVE
            user.banned_until = None
            user.ban_reason = None
            await uow.audit.record(
                action=AuditAction.USER_UNBAN,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
            )

    async def mute(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        user_id: int,
        minutes: int,
    ) -> None:
        self.require_role(actor, AdminRole.SUPPORT)
        if minutes <= 0:
            raise ValidationError("Mute duration must be positive")
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            user.status = UserStatus.MUTED
            user.muted_until = datetime.now(UTC) + timedelta(minutes=minutes)
            await uow.audit.record(
                action=AuditAction.USER_MUTE,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
                payload={"minutes": minutes},
            )

    async def unmute(self, *, actor: AdminRole, actor_id: int, user_id: int) -> None:
        self.require_role(actor, AdminRole.SUPPORT)
        async with self._uow_factory.transaction() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found")
            user.status = UserStatus.ACTIVE
            user.muted_until = None
            await uow.audit.record(
                action=AuditAction.USER_UNMUTE,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
            )

    async def grant_subscription(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        user_id: int,
        tier: SubscriptionTier,
        days: int,
    ) -> None:
        self.require_role(actor, AdminRole.ADMIN)
        await self._subscriptions.grant(
            user_id=user_id,
            tier=tier,
            days=days,
            source="admin_grant",
            admin_id=actor_id,
        )
        async with self._uow_factory.transaction() as uow:
            await uow.audit.record(
                action=AuditAction.GRANT_SUBSCRIPTION,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
                summary=f"{tier.value} for {days} days",
                payload={"tier": tier.value, "days": days},
            )

    async def revoke_subscription(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        user_id: int,
    ) -> None:
        self.require_role(actor, AdminRole.ADMIN)
        await self._subscriptions.revoke(user_id, admin_id=actor_id)
        async with self._uow_factory.transaction() as uow:
            await uow.audit.record(
                action=AuditAction.REVOKE_SUBSCRIPTION,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
            )

    async def update_limits(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        user_id: int,
        **values: Any,
    ) -> None:
        self.require_role(actor, AdminRole.ADMIN)
        allowed = {
            "extra_daily_downloads",
            "extra_file_size_bytes",
            "daily_downloads_absolute",
            "max_file_size_absolute",
            "max_duration_absolute",
            "max_concurrent_absolute",
            "unlimited",
            "ads_disabled",
            "expires_at",
            "note",
        }
        payload = {key: value for key, value in values.items() if key in allowed}
        if not payload:
            raise ValidationError("No valid limit fields provided")
        async with self._uow_factory.transaction() as uow:
            await uow.limits.upsert(user_id, **payload)
            await uow.audit.record(
                action=AuditAction.UPDATE_LIMITS,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
                payload={k: str(v) for k, v in payload.items()},
            )

    async def adjust_balance(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        user_id: int,
        amount: int,
        comment: str = "",
    ) -> int:
        """Credit (positive) or debit (negative) a user's coin balance."""
        self.require_role(actor, AdminRole.ADMIN)
        from mediabot.domain.enums import TransactionType  # local import: avoids a cycle

        async with self._uow_factory.transaction() as uow:
            transaction = await uow.wallets.apply(
                user_id=user_id,
                amount=abs(amount),
                transaction_type=(TransactionType.CREDIT if amount >= 0 else TransactionType.DEBIT),
                reason=TransactionReason.ADMIN_ADJUSTMENT,
                comment=comment[:255] or f"admin {actor_id}",
            )
            await uow.audit.record(
                action=AuditAction.ADJUST_BALANCE,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="user",
                target_id=user_id,
                payload={"amount": amount},
            )
            return transaction.balance_after

    # ------------------------------------------------------------------ #
    # Promo codes
    # ------------------------------------------------------------------ #
    async def create_promo(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        promo_type: PromoType,
        value: int,
        code: str | None = None,
        max_activations: int | None = None,
        per_user_limit: int = 1,
        expires_in_days: int | None = None,
        min_tier: SubscriptionTier | None = None,
        new_users_only: bool = False,
        campaign: str | None = None,
        description: str | None = None,
    ) -> PromoCode:
        self.require_role(actor, AdminRole.ADMIN)
        if value < 0:
            raise ValidationError("Promo value cannot be negative")
        final_code = self._promos.normalize(code) if code else self._promos.generate_code()

        async with self._uow_factory.transaction() as uow:
            if await uow.promos.by_code(final_code) is not None:
                raise ValidationError(f"Promo code {final_code} already exists")
            promo = await uow.promos.create(
                code=final_code,
                promo_type=promo_type,
                status=PromoStatus.ACTIVE,
                value=value,
                description=description,
                max_activations=max_activations,
                per_user_limit=max(per_user_limit, 1),
                expires_at=(
                    datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
                ),
                min_tier=min_tier,
                new_users_only=new_users_only,
                campaign=campaign,
                created_by_admin_id=actor_id,
            )
            await uow.audit.record(
                action=AuditAction.CREATE_PROMO,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="promo",
                target_id=final_code,
                summary=f"{promo_type.value}={value}",
            )
            return promo

    async def update_promo(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        code: str,
        **values: Any,
    ) -> PromoCode:
        self.require_role(actor, AdminRole.ADMIN)
        allowed = {
            "value",
            "description",
            "max_activations",
            "per_user_limit",
            "expires_at",
            "min_tier",
            "new_users_only",
            "campaign",
            "status",
        }
        async with self._uow_factory.transaction() as uow:
            promo = await uow.promos.by_code(code)
            if promo is None:
                raise NotFoundError(f"Promo code {code} not found")
            for key, value in values.items():
                if key in allowed:
                    setattr(promo, key, value)
            await uow.audit.record(
                action=AuditAction.UPDATE_PROMO,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="promo",
                target_id=code,
                payload={k: str(v) for k, v in values.items() if k in allowed},
            )
            return promo

    async def disable_promo(self, *, actor: AdminRole, actor_id: int, code: str) -> None:
        self.require_role(actor, AdminRole.ADMIN)
        async with self._uow_factory.transaction() as uow:
            promo = await uow.promos.by_code(code)
            if promo is None:
                raise NotFoundError(f"Promo code {code} not found")
            promo.status = PromoStatus.DISABLED
            await uow.audit.record(
                action=AuditAction.DISABLE_PROMO,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="promo",
                target_id=code,
            )

    async def promo_statistics(self, code: str) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            promo = await uow.promos.by_code(code)
            if promo is None:
                raise NotFoundError(f"Promo code {code} not found")
            stats = await uow.promos.usage_stats(promo.id)
            recent = await uow.promo_activations.list_for_promo(promo.id, limit=20)
            return {
                "promo": promo,
                "stats": stats,
                "recent": [
                    {
                        "user_id": row.user_id,
                        "created_at": row.created_at,
                        "reward": row.reward_summary,
                    }
                    for row in recent
                ],
            }

    async def list_promos(
        self,
        *,
        status: PromoStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PromoCode]:
        async with self._uow_factory() as uow:
            return list(await uow.promos.list_filtered(status=status, limit=limit, offset=offset))

    # ------------------------------------------------------------------ #
    # Queue
    # ------------------------------------------------------------------ #
    async def queue_snapshot(self, *, limit: int = 50) -> QueueSnapshot:
        async with self._uow_factory() as uow:
            entries = await uow.queue.pending(limit=limit)
            running = await uow.downloads.count_running()
            waiting = await uow.queue.size()
            rows: list[dict[str, object]] = []
            for entry in entries:
                download = await uow.downloads.get(entry.download_id)
                rows.append(
                    {
                        "download_id": entry.download_id,
                        "user_id": entry.user_id,
                        "priority": entry.priority,
                        "enqueued_at": entry.enqueued_at,
                        "title": download.title if download else None,
                        "platform": download.platform.value if download else None,
                        "status": download.status.value if download else None,
                    }
                )
        return QueueSnapshot(
            waiting=waiting,
            running=running,
            capacity=self._settings.downloader.max_concurrent_jobs,
            entries=rows,
        )

    async def clear_queue(self, *, actor: AdminRole, actor_id: int) -> int:
        self.require_role(actor, AdminRole.ADMIN)
        async with self._uow_factory.transaction() as uow:
            entries = await uow.queue.pending(limit=10_000)
            for entry in entries:
                download = await uow.downloads.get(entry.download_id)
                if download is not None and not DownloadStatus(download.status).is_terminal:
                    download.status = DownloadStatus.CANCELLED
                    download.finished_at = datetime.now(UTC)
                    if download.task_id:
                        self._dispatcher.revoke(download.task_id)
            removed = await uow.queue.clear()
            await uow.audit.record(
                action=AuditAction.QUEUE_CLEAR,
                actor_id=actor_id,
                actor_name=str(actor_id),
                summary=f"cleared {removed} jobs",
            )
        log.warning("queue cleared by {} ({} jobs)", actor_id, removed)
        return removed

    async def cancel_job(self, *, actor: AdminRole, actor_id: int, download_id: int) -> None:
        self.require_role(actor, AdminRole.MODERATOR)
        async with self._uow_factory.transaction() as uow:
            download = await uow.downloads.get(download_id)
            if download is None:
                raise NotFoundError("Download not found")
            if download.task_id:
                self._dispatcher.revoke(download.task_id)
            download.status = DownloadStatus.CANCELLED
            download.finished_at = datetime.now(UTC)
            await uow.queue.remove(download_id)
            await uow.audit.record(
                action=AuditAction.QUEUE_CANCEL_JOB,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="download",
                target_id=download_id,
            )

    # ------------------------------------------------------------------ #
    # Logs & settings
    # ------------------------------------------------------------------ #
    async def audit_log(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        async with self._uow_factory() as uow:
            rows = await uow.audit.list_recent(limit=limit, offset=offset)
            return [
                {
                    "id": row.id,
                    "created_at": row.created_at,
                    "actor_id": row.actor_id,
                    "actor_name": row.actor_name,
                    "action": row.action.value,
                    "target": f"{row.target_type or ''}:{row.target_id or ''}",
                    "summary": row.summary,
                    "ip": row.ip_address,
                }
                for row in rows
            ]

    async def error_log(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        resolved: bool | None = False,
    ) -> list[dict[str, Any]]:
        async with self._uow_factory() as uow:
            rows = await uow.errors.list_recent(limit=limit, offset=offset, resolved=resolved)
            return [
                {
                    "id": row.id,
                    "created_at": row.created_at,
                    "code": row.code,
                    "message": row.message,
                    "component": row.component,
                    "user_id": row.user_id,
                    "download_id": row.download_id,
                    "url": row.url,
                    "resolved": row.resolved,
                }
                for row in rows
            ]

    async def resolve_error(self, *, actor: AdminRole, error_id: int) -> None:
        self.require_role(actor, AdminRole.MODERATOR)
        async with self._uow_factory.transaction() as uow:
            await uow.errors.resolve(error_id)

    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self._uow_factory() as uow:
            return await uow.settings.get_value(key, default)

    async def set_setting(
        self,
        *,
        actor: AdminRole,
        actor_id: int,
        key: str,
        value: Any,
        category: str = "general",
    ) -> None:
        self.require_role(actor, AdminRole.ADMIN)
        async with self._uow_factory.transaction() as uow:
            await uow.settings.set_value(key, value, category=category, admin_id=actor_id)
            await uow.audit.record(
                action=AuditAction.SETTINGS_UPDATE,
                actor_id=actor_id,
                actor_name=str(actor_id),
                target_type="setting",
                target_id=key,
                payload={"value": str(value)},
            )
