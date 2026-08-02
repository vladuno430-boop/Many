"""Group and private AI chat handlers backed by api.hcnsec.cn."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict, deque
from typing import Final, Literal

from aiogram import F, Bot, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, Message

from mediabot.infrastructure.ai import AIAPIError, AISettings, HcnsecAIClient

router = Router(name="ai")

_SYSTEM_PROMPT: Final[str] = (
    "Ты полезный ИИ-помощник в Telegram. Отвечай понятно, по существу и на "
    "языке пользователя. В общей группе учитывай имя автора сообщения, не "
    "выдавай догадки за факты и не раскрывай данные других участников."
)
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://\S+", re.IGNORECASE)

_settings = AISettings()
_client = HcnsecAIClient(_settings)
_histories: dict[str, deque[dict[str, str]]] = {}
_chat_modes: dict[int, Literal["mention", "all"]] = {}
_chat_models: dict[int, str] = {}
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _history_key(message: Message) -> str:
    return f"{message.chat.id}:{message.message_thread_id or 0}"


def _history(message: Message) -> deque[dict[str, str]]:
    key = _history_key(message)
    if key not in _histories:
        _histories[key] = deque(maxlen=_settings.max_history_messages)
    return _histories[key]


def _command_argument(message: Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _bot_mentioned(message: Message, username: str) -> bool:
    text = message.text or ""
    return bool(
        re.search(
            rf"(?<!\w)@{re.escape(username)}(?!\w)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _strip_mention(text: str, username: str) -> str:
    return re.sub(
        rf"(?<!\w)@{re.escape(username)}(?!\w)",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" \n,:;-—")


def _reply_to_bot(message: Message, bot_id: int) -> bool:
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot_id)


def _mode(chat_id: int) -> Literal["mention", "all"]:
    return _chat_modes.get(chat_id, _settings.default_group_mode)


def _model(chat_id: int) -> str:
    return _chat_models.get(chat_id, _settings.model)


async def _is_admin(message: Message, bot: Bot) -> bool:
    if message.chat.type == "private" or message.from_user is None:
        return True
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))


async def _reply_chunks(message: Message, text: str) -> None:
    remaining = text.strip() or "ИИ вернул пустой ответ."
    first = True
    while remaining:
        if len(remaining) <= 3900:
            if first:
                await message.reply(remaining)
            else:
                await message.answer(remaining)
            return
        split_at = remaining.rfind("\n\n", 0, 3900)
        if split_at < 1000:
            split_at = remaining.rfind("\n", 0, 3900)
        if split_at < 1000:
            split_at = remaining.rfind(" ", 0, 3900)
        if split_at < 1000:
            split_at = 3900
        chunk = remaining[:split_at].strip()
        if first:
            await message.reply(chunk)
            first = False
        else:
            await message.answer(chunk)
        remaining = remaining[split_at:].strip()


def _friendly_error(error: AIAPIError) -> str:
    if error.status in {401, 403}:
        return "❌ API-ключ отклонён. Проверь AI__API_KEY в файле .env."
    if error.status == 429:
        return "⏳ Лимит API или квота закончились. Попробуй позже."
    if error.status == 404:
        return "⚠️ Модель не найдена. Используй /ai_models и /ai_setmodel."
    return f"⚠️ Ошибка ИИ: {error.detail}"


async def _answer_ai(message: Message, prompt: str) -> None:
    if not _settings.configured:
        await message.reply(
            "⚙️ ИИ ещё не настроен. В Termux запусти:\n"
            "bash termux/configure-ai.sh"
        )
        return

    key = _history_key(message)
    async with _locks[key]:
        history = _history(message)
        author = message.from_user.full_name if message.from_user else "участник Telegram"
        user_prompt = (
            f"Сообщение от участника «{author}»:\n{prompt}"
            if message.chat.type != "private"
            else prompt
        )
        history.append({"role": "user", "content": user_prompt})

        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action="typing",
            message_thread_id=message.message_thread_id,
        )
        try:
            answer = await _client.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    *list(history),
                ],
                model=_model(message.chat.id),
            )
        except AIAPIError as error:
            history.pop()
            await message.reply(_friendly_error(error))
            return

        history.append({"role": "assistant", "content": answer})
        await _reply_chunks(message, answer)


@router.message(Command("ask"))
async def ask_command(message: Message) -> None:
    prompt = _command_argument(message)
    if not prompt:
        await message.reply("Напиши вопрос после команды: /ask твой вопрос")
        return
    await _answer_ai(message, prompt)


@router.message(Command("translate"))
async def translate_command(message: Message) -> None:
    prompt = _command_argument(message)
    if not prompt:
        await message.reply("Добавь текст после команды /translate")
        return
    await _answer_ai(
        message,
        "Переведи текст на русский. Если он уже русский — переведи на английский. "
        f"Сохрани смысл и оформление:\n\n{prompt}",
    )


@router.message(Command("summary"))
async def summary_command(message: Message) -> None:
    prompt = _command_argument(message)
    if not prompt:
        await message.reply("Добавь текст после команды /summary")
        return
    await _answer_ai(
        message,
        f"Кратко и понятно перескажи текст, выделив главное:\n\n{prompt}",
    )


@router.message(Command("code"))
async def code_command(message: Message) -> None:
    prompt = _command_argument(message)
    if not prompt:
        await message.reply("Опиши задачу после команды /code")
        return
    await _answer_ai(
        message,
        "Помоги решить задачу по программированию. Дай рабочий код, объясни "
        f"запуск и важные ошибки:\n\n{prompt}",
    )


@router.message(Command("idea"))
async def idea_command(message: Message) -> None:
    prompt = _command_argument(message)
    if not prompt:
        await message.reply("Напиши тему после команды /idea")
        return
    await _answer_ai(message, f"Предложи 7 разнообразных идей по теме:\n\n{prompt}")


@router.message(Command("ai_new"))
async def new_command(message: Message) -> None:
    _histories.pop(_history_key(message), None)
    await message.reply("🧹 Память ИИ для этого чата очищена.")


@router.message(Command("ai_model"))
async def model_command(message: Message) -> None:
    await message.reply(
        f"Сервис: api.hcnsec.cn\nМодель этого чата: {_model(message.chat.id)}"
    )


@router.message(Command("ai_models"))
async def models_command(message: Message) -> None:
    try:
        models = await _client.list_models()
    except AIAPIError as error:
        await message.reply(_friendly_error(error))
        return
    if not models:
        await message.reply("Сервис не вернул список моделей.")
        return
    shown = models[:60]
    lines = "\n".join(f"{index}. {model}" for index, model in enumerate(shown, 1))
    suffix = f"\n\nПоказаны первые {len(shown)} из {len(models)}." if len(models) > 60 else ""
    await message.reply(
        f"Доступные модели:\n\n{lines}{suffix}\n\n"
        "Выбор: /ai_setmodel ТОЧНЫЙ_ID"
    )


@router.message(Command("ai_setmodel"))
async def setmodel_command(message: Message, bot: Bot) -> None:
    if not await _is_admin(message, bot):
        await message.reply("⛔ В группе менять модель может только администратор.")
        return
    model = _command_argument(message)
    if not model:
        await message.reply("Пример: /ai_setmodel auto")
        return
    try:
        models = await _client.list_models()
    except AIAPIError as error:
        await message.reply(_friendly_error(error))
        return
    if model != "auto" and model not in models:
        await message.reply("⛔ Такого ID нет в /ai_models.")
        return
    _chat_models[message.chat.id] = model
    _histories.pop(_history_key(message), None)
    await message.reply(f"✅ Модель изменена на {model}. Память чата очищена.")


@router.message(Command("ai_mode"))
async def mode_command(message: Message, bot: Bot) -> None:
    if message.chat.type == "private":
        await message.reply("В личном чате бот и так отвечает на обычные сообщения.")
        return
    if not await _is_admin(message, bot):
        await message.reply("⛔ Менять режим может только администратор группы.")
        return

    requested = _command_argument(message).lower()
    if requested not in {"mention", "all"}:
        await message.reply(
            f"Текущий режим: {_mode(message.chat.id)}\n\n"
            "/ai_mode mention — команды, упоминания и ответы боту\n"
            "/ai_mode all — отвечать на все сообщения, кроме ссылок"
        )
        return

    _chat_modes[message.chat.id] = "all" if requested == "all" else "mention"
    extra = (
        "\n\nДля режима all отключи Privacy Mode в @BotFather через /setprivacy, "
        "затем удали бота из группы и добавь снова."
        if requested == "all"
        else ""
    )
    await message.reply(f"✅ Режим ИИ: {requested}{extra}")


@router.message(Command("ai_help"))
async def help_command(message: Message) -> None:
    await message.reply(
        "🤖 ИИ-команды:\n"
        "/ask вопрос\n"
        "/translate текст\n"
        "/summary текст\n"
        "/code задача\n"
        "/idea тема\n"
        "/ai_new\n"
        "/ai_model\n"
        "/ai_models\n"
        "/ai_setmodel ID\n"
        "/ai_mode mention|all\n\n"
        "В группе также можно упомянуть бота или ответить на его сообщение."
    )


@router.message(F.text)
async def conversational_message(message: Message, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        raise SkipHandler

    # Links are deliberately left to the existing multimedia downloader unless
    # the user explicitly addresses the AI.
    me = await bot.get_me()
    username = me.username or ""
    mentioned = bool(username and _bot_mentioned(message, username))
    replied = _reply_to_bot(message, bot.id)

    if message.chat.type == "private":
        if _URL_RE.search(text):
            raise SkipHandler
        await _answer_ai(message, text)
        return

    if _mode(message.chat.id) == "all":
        if _URL_RE.search(text) and not mentioned and not replied:
            raise SkipHandler
        await _answer_ai(message, _strip_mention(text, username) if mentioned else text)
        return

    if mentioned:
        prompt = _strip_mention(text, username)
        if prompt:
            await _answer_ai(message, prompt)
        else:
            await message.reply("Напиши вопрос после упоминания.")
        return

    if replied:
        await _answer_ai(message, text)
        return

    raise SkipHandler
