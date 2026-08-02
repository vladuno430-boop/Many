#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# MediaBot — установка в Termux (Android), одной командой.
#
#   bash termux/install.sh
#
# Ставит системные пакеты, создаёт виртуальное окружение, устанавливает бота
# в standalone-режиме (SQLite + встроенный воркер, без PostgreSQL, Redis и
# Celery), применяет миграции и создаёт .env.
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
ENV_FILE="${PROJECT_DIR}/.env"
DATA_DIR="${HOME}/mediabot"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$1" >&2; exit 1; }

[ -d "/data/data/com.termux" ] || warn "Похоже, это не Termux — скрипт всё равно попробует отработать."

# --------------------------------------------------------------------------- #
# 1. Системные пакеты
# --------------------------------------------------------------------------- #
say "Обновляю списки пакетов Termux"
pkg update -y >/dev/null 2>&1 || warn "pkg update завершился с ошибкой, продолжаю"

say "Ставлю python, ffmpeg и инструменты сборки"
pkg install -y python python-pip ffmpeg git libmagic clang binutils libffi openssl

# У Termux есть готовые сборки некоторых «тяжёлых» пакетов — они экономят
# десятки минут компиляции. Если пакета нет в репозитории, ставим из pip.
say "Ставлю системные сборки cryptography и lxml (если доступны)"
pkg install -y python-cryptography 2>/dev/null || warn "python-cryptography нет в репозитории, соберётся через pip"

# --------------------------------------------------------------------------- #
# 2. Виртуальное окружение
# --------------------------------------------------------------------------- #
say "Создаю виртуальное окружение: ${VENV_DIR}"
[ -d "${VENV_DIR}" ] || python -m venv --system-site-packages "${VENV_DIR}"
# --system-site-packages: чтобы venv видел python-cryptography из pkg.

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

say "Обновляю pip"
pip install --upgrade pip wheel setuptools >/dev/null

say "Устанавливаю MediaBot (профиль standalone)"
cd "${PROJECT_DIR}"
# Без extras postgres/worker/metrics/security: они требуют компилятора Rust
# или заголовков PostgreSQL, которых в Termux нет.
pip install -e ".[standalone]"

# --------------------------------------------------------------------------- #
# 3. Каталоги данных
# --------------------------------------------------------------------------- #
say "Создаю каталоги данных в ${DATA_DIR}"
mkdir -p "${DATA_DIR}/storage" "${DATA_DIR}/tmp" "${DATA_DIR}/logs"

# --------------------------------------------------------------------------- #
# 4. Конфигурация
# --------------------------------------------------------------------------- #
if [ -f "${ENV_FILE}" ]; then
    say ".env уже существует — оставляю как есть"
else
    say "Создаю .env"
    JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
    FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(12))')"

    read -r -p "Токен бота от @BotFather: " BOT_TOKEN
    read -r -p "Юзернейм бота без @ (например my_media_bot): " BOT_USERNAME
    read -r -p "Ваш Telegram ID (узнать: @userinfobot): " ADMIN_ID

    sed \
        -e "s|__BOT_TOKEN__|${BOT_TOKEN}|" \
        -e "s|__BOT_USERNAME__|${BOT_USERNAME}|" \
        -e "s|__ADMIN_ID__|${ADMIN_ID}|" \
        -e "s|__JWT_SECRET__|${JWT_SECRET}|" \
        -e "s|__FERNET_KEY__|${FERNET_KEY}|" \
        -e "s|__ADMIN_PASSWORD__|${ADMIN_PASSWORD}|" \
        -e "s|__DATA_DIR__|${DATA_DIR}|g" \
        "${PROJECT_DIR}/termux/env.template" > "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    say "Пароль админ-панели: ${ADMIN_PASSWORD} (сохранён в .env)"
fi

# --------------------------------------------------------------------------- #
# 5. База данных
# --------------------------------------------------------------------------- #
say "Применяю миграции (SQLite)"
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
alembic upgrade head

# --------------------------------------------------------------------------- #
# 6. Проверка
# --------------------------------------------------------------------------- #
say "Проверяю окружение"
python - <<'PY'
import shutil
import sys

problems = []
if shutil.which("ffmpeg") is None:
    problems.append("ffmpeg не найден — конвертация и склейка видео не будут работать")
try:
    import yt_dlp  # noqa: F401
except ImportError:
    problems.append("yt-dlp не установлен")
try:
    import aiogram  # noqa: F401
except ImportError:
    problems.append("aiogram не установлен")

if problems:
    for item in problems:
        print(f"  [!] {item}")
    sys.exit(1)
print("  ok: ffmpeg, yt-dlp и aiogram на месте")
PY

cat <<EOF

$(printf '\033[1;32m✔ Установка завершена\033[0m')

Запуск бота:      bash termux/run.sh
Админ-панель:     bash termux/run.sh api   (потом http://127.0.0.1:8000/admin)
Автозапуск:       см. TERMUX.md, раздел «Автозапуск»

Конфигурация:     ${ENV_FILE}
База данных:      ${DATA_DIR}/mediabot.db
Файлы и логи:     ${DATA_DIR}

Совет: включите блокировку сна (в шторке Termux → «Acquire wakelock»),
иначе Android усыпит процесс при выключенном экране.
EOF
