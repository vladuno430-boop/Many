"""Finite-state machine groups used by the bot's multi-step flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class DownloadStates(StatesGroup):
    """Link → format → quality → confirmation."""

    waiting_for_link = State()
    choosing_format = State()
    choosing_quality = State()


class PromoStates(StatesGroup):
    """Promo redemption."""

    waiting_for_code = State()


class HistoryStates(StatesGroup):
    """History search."""

    waiting_for_query = State()


class FavoriteStates(StatesGroup):
    """Collection creation."""

    waiting_for_collection_name = State()


class CaptchaStates(StatesGroup):
    """Human verification."""

    waiting_for_answer = State()


class AdminStates(StatesGroup):
    """Admin panel flows."""

    waiting_for_user_query = State()
    waiting_for_broadcast_text = State()
    waiting_for_broadcast_confirm = State()
    waiting_for_promo_value = State()
    waiting_for_ban_reason = State()
    waiting_for_grant_days = State()
    waiting_for_balance_amount = State()
