"""In-chat admin panel: statistics, users, queue, promo codes, broadcasts, logs."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from mediabot.application.dto import UserContext
from mediabot.application.services.economy_service import generate_broadcast_id
from mediabot.core.container import Container
from mediabot.core.exceptions import MediaBotError, PermissionDeniedError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import escape_html
from mediabot.domain.enums import AdminRole, PromoType, SubscriptionTier
from mediabot.domain.value_objects import humanize_bytes
from mediabot.presentation.bot.callbacks import AdminCallback
from mediabot.presentation.bot.i18n.translator import Translator, all_translations
from mediabot.presentation.bot.keyboards import (
    admin_menu,
    admin_queue_actions,
    admin_user_actions,
)
from mediabot.presentation.bot.states import AdminStates

router = Router(name="admin")
log = get_logger(LogChannel.ADMIN, component="handlers.admin")


async def _role(ctx: UserContext, container: Container) -> AdminRole:
    """Resolve the caller's staff role or refuse the action."""
    role = await container.admin.resolve_role(ctx.user_id)
    if role is None:
        raise PermissionDeniedError("Staff access required")
    return role


@router.message(Command("admin"))
@router.message(F.text.in_(all_translations("menu.admin")))
async def admin_root(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    try:
        await _role(ctx, container)
    except PermissionDeniedError:
        await message.answer(t("admin.denied"))
        return
    await state.clear()
    await message.answer(t("admin.title"), reply_markup=admin_menu(t))


@router.callback_query(AdminCallback.filter(F.action == "root"))
async def admin_back(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(t("admin.title"), reply_markup=admin_menu(t))
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.action == "stats"))
async def admin_stats(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    metrics = await container.statistics.dashboard()
    text = t(
        "admin.stats_text",
        dau=metrics.dau,
        mau=metrics.mau,
        new_today=metrics.new_users_today,
        downloads=metrics.downloads_today,
        failed=metrics.downloads_failed_today,
        traffic=humanize_bytes(metrics.bytes_today),
        subs=metrics.active_subscriptions,
        revenue=metrics.revenue_today,
        queue=metrics.queue_size,
        active=metrics.active_jobs,
        cpu=round(metrics.cpu_percent, 1),
        memory=round(metrics.memory_percent, 1),
        disk=round(metrics.disk_used_percent, 1),
    )
    top = "\n".join(
        f"{index}. {platform} — {count}"
        for index, (platform, count) in enumerate(
            sorted(metrics.by_platform.items(), key=lambda item: -item[1])[:5], start=1
        )
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"{text}\n\n<b>Top platforms</b>\n{top or '—'}", reply_markup=admin_menu(t)
        )
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.action == "users"))
async def admin_users_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    await state.set_state(AdminStates.waiting_for_user_query)
    if isinstance(callback.message, Message):
        await callback.message.answer(t("admin.user_prompt"))
    await callback.answer()


@router.message(AdminStates.waiting_for_user_query)
async def admin_find_user(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    await state.clear()
    users = await container.admin.find_users((message.text or "").strip(), limit=5)
    if not users:
        await message.answer(t("common.empty"))
        return
    for user in users:
        details = await container.admin.user_details(user.id)
        wallet = details["wallet"]
        subscription = details["subscription"]
        await message.answer(
            "\n".join(
                [
                    f"👤 <b>{escape_html(user.full_name)}</b> (<code>{user.id}</code>)",
                    f"@{escape_html(user.username or '—')} · {user.language.value}",
                    f"💎 {user.tier.value} · status: {user.status.value}",
                    f"📥 {user.total_downloads} · 💾 {humanize_bytes(user.total_bytes)}",
                    f"🪙 {wallet.balance if wallet else 0}",
                    f"📅 {user.created_at.strftime('%d.%m.%Y')}",
                    f"⏳ sub until: {subscription.expires_at if subscription else '—'}",
                ]
            ),
            reply_markup=admin_user_actions(user.id, t),
        )


@router.callback_query(AdminCallback.filter(F.action.in_({"ban", "unban", "mute", "unmute"})))
async def admin_moderate(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    role = await _role(ctx, container)
    action = callback_data.action
    target = callback_data.target_id
    try:
        if action == "ban":
            await container.admin.ban(
                actor=role,
                actor_id=ctx.user_id,
                user_id=target,
                reason="via bot admin panel",
            )
        elif action == "unban":
            await container.admin.unban(actor=role, actor_id=ctx.user_id, user_id=target)
        elif action == "mute":
            await container.admin.mute(actor=role, actor_id=ctx.user_id, user_id=target, minutes=60)
        else:
            await container.admin.unmute(actor=role, actor_id=ctx.user_id, user_id=target)
    except MediaBotError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    await callback.answer(t("common.done"))


@router.callback_query(AdminCallback.filter(F.action == "grant"))
async def admin_grant(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    role = await _role(ctx, container)
    tier = SubscriptionTier.VIP if callback_data.value == "vip" else SubscriptionTier.PREMIUM
    try:
        await container.admin.grant_subscription(
            actor=role,
            actor_id=ctx.user_id,
            user_id=callback_data.target_id,
            tier=tier,
            days=30,
        )
    except MediaBotError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    await callback.answer(t("common.done"))


@router.callback_query(AdminCallback.filter(F.action == "revoke"))
async def admin_revoke(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    role = await _role(ctx, container)
    await container.admin.revoke_subscription(
        actor=role, actor_id=ctx.user_id, user_id=callback_data.target_id
    )
    await callback.answer(t("common.done"))


@router.callback_query(AdminCallback.filter(F.action == "coins"))
async def admin_coins(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    role = await _role(ctx, container)
    balance = await container.admin.adjust_balance(
        actor=role,
        actor_id=ctx.user_id,
        user_id=callback_data.target_id,
        amount=int(callback_data.value or 0),
        comment="bot admin panel",
    )
    await callback.answer(f"🪙 {balance}")


@router.callback_query(AdminCallback.filter(F.action == "queue"))
async def admin_queue(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    snapshot = await container.admin.queue_snapshot(limit=10)
    lines = [
        f"📦 <b>Queue</b>: {snapshot.waiting} waiting · {snapshot.running} running "
        f"(capacity {snapshot.capacity})",
        "",
    ]
    for entry in snapshot.entries:
        lines.append(
            f"#{entry['download_id']} · user {entry['user_id']} · p{entry['priority']} · "
            f"{escape_html(str(entry['title'] or '—'))[:40]}"
        )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "\n".join(lines) or t("common.empty"), reply_markup=admin_queue_actions(t)
        )
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.action == "queue_clear"))
async def admin_queue_clear(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    role = await _role(ctx, container)
    removed = await container.admin.clear_queue(actor=role, actor_id=ctx.user_id)
    await callback.answer(t("admin.queue_cleared", count=removed), show_alert=True)


@router.callback_query(AdminCallback.filter(F.action == "promos"))
async def admin_promos(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    promos = await container.admin.list_promos(limit=15)
    lines = [t("admin.promos"), ""]
    for promo in promos:
        used = f"{promo.activations_used}/{promo.max_activations or '∞'}"
        lines.append(
            f"<code>{promo.code}</code> · {promo.promo_type.value} {promo.value} · "
            f"{used} · {promo.status.value}"
        )
    lines.append("")
    lines.append("/newpromo &lt;type&gt; &lt;value&gt; [activations] [days]")
    if isinstance(callback.message, Message):
        await callback.message.edit_text("\n".join(lines), reply_markup=admin_menu(t))
    await callback.answer()


@router.message(Command("newpromo"))
async def admin_new_promo(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """``/newpromo premium_days 7 100 30`` — type, value, activations, TTL days."""
    role = await _role(ctx, container)
    parts = (message.text or "").split()[1:]
    if len(parts) < 2:
        await message.answer("Usage: /newpromo &lt;type&gt; &lt;value&gt; [activations] [days]")
        return
    try:
        promo_type = PromoType(parts[0])
        value = int(parts[1])
    except (ValueError, KeyError):
        await message.answer(
            "Unknown type. Available: " + ", ".join(item.value for item in PromoType)
        )
        return
    max_activations = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    expires_in_days = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None

    promo = await container.admin.create_promo(
        actor=role,
        actor_id=ctx.user_id,
        promo_type=promo_type,
        value=value,
        max_activations=max_activations,
        expires_in_days=expires_in_days,
    )
    await message.answer(t("admin.promo_created", code=promo.code))


@router.callback_query(AdminCallback.filter(F.action == "broadcast"))
async def admin_broadcast_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    if isinstance(callback.message, Message):
        await callback.message.answer(t("admin.broadcast_prompt"))
    await callback.answer()


@router.message(AdminStates.waiting_for_broadcast_text)
async def admin_broadcast(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    role = await _role(ctx, container)
    container.admin.require_role(role, AdminRole.ADMIN)
    await state.clear()
    result = await container.notifications.broadcast(
        body=message.html_text or message.text or "",
        broadcast_id=generate_broadcast_id(),
    )
    await message.answer(t("admin.broadcast_queued", count=result.recipients))


@router.callback_query(AdminCallback.filter(F.action == "errors"))
async def admin_errors(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    rows = await container.admin.error_log(limit=10)
    lines = [t("admin.errors"), ""]
    for row in rows:
        lines.append(
            f"#{row['id']} · <code>{row['code']}</code> · {row['component']}\n"
            f"   {escape_html(str(row['message']))[:120]}"
        )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "\n".join(lines) if rows else t("common.empty"), reply_markup=admin_menu(t)
        )
    await callback.answer()


@router.callback_query(AdminCallback.filter(F.action == "logs"))
async def admin_logs(
    callback: CallbackQuery,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    await _role(ctx, container)
    rows = await container.admin.audit_log(limit=15)
    lines = [t("admin.logs"), ""]
    for row in rows:
        lines.append(
            f"{row['created_at'].strftime('%d.%m %H:%M')} · {row['action']} · "
            f"{row['actor_name']} → {row['target']}"
        )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "\n".join(lines) if rows else t("common.empty"), reply_markup=admin_menu(t)
        )
    await callback.answer()
