"""Profile, statistics, achievements and the referral programme."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.presentation.bot.formatters import render_profile
from mediabot.presentation.bot.i18n.translator import Translator, all_translations
from mediabot.presentation.bot.keyboards import referral_keyboard

router = Router(name="profile")


@router.message(Command("profile"))
@router.message(F.text.in_(all_translations("menu.profile")))
async def show_profile(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    stats = await container.statistics.user_statistics(ctx.user_id)
    await message.answer(
        render_profile(stats, ctx.policy, ctx.usage.downloads_today, t),
    )
    await container.achievements.evaluate(ctx.user_id)


@router.message(Command("achievements"))
@router.message(F.text.in_(all_translations("menu.achievements")))
async def show_achievements(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    progress = await container.achievements.progress(ctx.user_id)
    unlocked = sum(1 for _definition, _value, done in progress if done)
    lines = [t("achievements.title", unlocked=unlocked, total=len(progress))]
    for definition, value, done in progress:
        if definition.hidden and not done:
            continue
        lines.append(
            t(
                "achievements.entry",
                icon=definition.icon,
                name=definition.code.replace("_", " ").title(),
                progress=min(value, definition.threshold),
                threshold=definition.threshold,
                done=" ✅" if done else "",
            )
        )
    await message.answer("\n".join(lines))


@router.message(Command("referral"))
@router.message(F.text.in_(all_translations("menu.referral")))
async def show_referral(
    message: Message,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    stats = await container.referrals.stats(ctx.user_id)
    link = container.referrals.link_for(ctx.referral_code)
    economy = container.settings.economy
    await message.answer(
        t(
            "referral.title",
            link=link,
            count=stats["total"],
            coins=stats["coins"],
            days=stats["premium_days"],
            bonus=economy.referral_bonus_coins,
            invitee_bonus=economy.referral_invitee_coins,
        ),
        reply_markup=referral_keyboard(link, t),
        disable_web_page_preview=True,
    )
