# ИИ-чат в MediaBot

MediaBot сохраняет функции скачивания и дополнительно работает как групповой
ИИ-помощник через OpenAI-совместимый API `api.hcnsec.cn`.

## Настройка

На Android первый запуск автоматически вызывает:

```bash
bash termux/configure-ai.sh
```

Скрипт просит API-ключ со страницы `https://api.hcnsec.cn/keys`, модель
(по умолчанию `auto`), проверяет список моделей и отправляет тестовый запрос.
Ключ сохраняется только в локальном `.env` с правами `600`.

Для сервера добавьте:

```env
AI__ENABLED=true
AI__BASE_URL=https://api.hcnsec.cn/v1
AI__API_KEY=sk-...
AI__MODEL=auto
AI__TIMEOUT_SECONDS=120
AI__MAX_HISTORY_MESSAGES=12
AI__MAX_OUTPUT_TOKENS=1800
AI__DEFAULT_GROUP_MODE=mention
```

## Использование

В личном чате бот отвечает на обычный текст. Сообщения со ссылками остаются у
существующего загрузчика.

В группе доступны:

```text
/ask вопрос
/translate текст
/summary текст
/code задача
/idea тема
/ai_new
/ai_model
/ai_models
/ai_setmodel ID
/ai_mode mention
/ai_mode all
/ai_help
```

В режиме `mention` бот отвечает на команды, упоминание `@имя_бота` и ответы на
его сообщения. Режим `all` включает ответы на обычные сообщения, но ссылки без
явного обращения к ИИ всё равно передаются загрузчику.

Чтобы Telegram передавал боту все сообщения для `/ai_mode all`, отключите
Privacy Mode: `@BotFather` → `/setprivacy` → бот → **Disable**, затем удалите
бота из группы и добавьте снова.

Менять модель и режим группы может только администратор.
