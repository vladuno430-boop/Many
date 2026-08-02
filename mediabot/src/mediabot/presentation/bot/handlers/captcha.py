"""Human verification shown after repeated abuse-control violations."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from mediabot.application.dto import UserContext
from mediabot.core.container import Container
from mediabot.presentation.bot.callbacks import CaptchaCallback
from mediabot.presentation.bot.i18n.translator import Translator
from mediabot.presentation.bot.keyboards import captcha_keyboard
from mediabot.presentation.bot.states import CaptchaStates

router = Router(name="captcha")


async def send_challenge(
    message: Message,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    """Issue a challenge and park the user in the CAPTCHA state."""
    challenge = await container.security.issue_captcha(ctx.user_id)
    await state.set_state(CaptchaStates.waiting_for_answer)
    await message.answer(
        t("captcha.title", question=challenge.question),
        reply_markup=captcha_keyboard(challenge.options),
    )


@router.callback_query(CaptchaCallback.filter())
async def verify(
    callback: CallbackQuery,
    callback_data: CaptchaCallback,
    state: FSMContext,
    ctx: UserContext,
    t: Translator,
    container: Container,
) -> None:
    solved = await container.security.verify_captcha(ctx.user_id, callback_data.answer)
    if solved:
        await state.clear()
        await callback.answer(t("captcha.correct"))
        if isinstance(callback.message, Message):
            await callback.message.edit_text(t("captcha.correct"))
        return

    await callback.answer(t("captcha.wrong"), show_alert=True)
    if isinstance(callback.message, Message):
        challenge = await container.security.issue_captcha(ctx.user_id)
        await callback.message.edit_text(
            t("captcha.title", question=challenge.question),
            reply_markup=captcha_keyboard(challenge.options),
        )
