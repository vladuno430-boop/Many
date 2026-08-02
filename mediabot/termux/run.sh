#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# MediaBot — запуск в Termux.
#
#   bash termux/run.sh           # бот (по умолчанию)
#   bash termux/run.sh api       # REST API + админ-панель
#   bash termux/run.sh migrate   # применить миграции
#   bash termux/run.sh doctor    # диагностика окружения
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
ROLE="${1:-bot}"

[ -d "${VENV_DIR}" ] || { echo "Окружение не найдено. Запустите: bash termux/install.sh"; exit 1; }
[ -f "${PROJECT_DIR}/.env" ] || { echo ".env не найден. Запустите: bash termux/install.sh"; exit 1; }

source "${VENV_DIR}/bin/activate"
cd "${PROJECT_DIR}"

set -a
source "${PROJECT_DIR}/.env"
set +a

# При первом запуске один раз просим ключ ИИ и проверяем его.
if [ "${ROLE}" = "bot" ] && [ -z "${AI__API_KEY:-}" ]; then
    echo "ИИ-чат ещё не настроен."
    bash "${PROJECT_DIR}/termux/configure-ai.sh"
    set -a
    source "${PROJECT_DIR}/.env"
    set +a
fi

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    trap 'termux-wake-unlock 2>/dev/null || true' EXIT
fi

case "${ROLE}" in
    bot)
        echo "Запускаю бота (скачивание + ИИ-чат)…  Ctrl+C — остановить"
        exec python -m mediabot bot
        ;;
    api)
        echo "API и панель: http://127.0.0.1:${API__PORT:-8000}/admin"
        exec python -m mediabot api
        ;;
    migrate)
        exec alembic upgrade head
        ;;
    doctor)
        exec python "${PROJECT_DIR}/termux/doctor.py"
        ;;
    *)
        echo "Неизвестный режим: ${ROLE}. Доступно: bot | api | migrate | doctor"
        exit 2
        ;;
esac
