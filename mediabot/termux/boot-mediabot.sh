#!/data/data/com.termux/files/usr/bin/sh
# =============================================================================
# Автозапуск MediaBot при загрузке телефона.
#
# Установка:
#   mkdir -p ~/.termux/boot
#   cp termux/boot-mediabot.sh ~/.termux/boot/mediabot.sh
#   chmod +x ~/.termux/boot/mediabot.sh
#
# Требуется приложение Termux:Boot (F-Droid), запущенное хотя бы один раз.
# =============================================================================
set -eu

# Не даём Android усыпить процесс сразу после загрузки.
termux-wake-lock 2>/dev/null || true

# Путь к проекту: поправьте, если клонировали в другое место.
PROJECT_DIR="${HOME}/mediabot-repo/mediabot"
LOG_DIR="${HOME}/mediabot/logs"

mkdir -p "${LOG_DIR}"

if [ ! -d "${PROJECT_DIR}" ]; then
    echo "$(date -Iseconds) проект не найден: ${PROJECT_DIR}" >> "${LOG_DIR}/boot.log"
    exit 1
fi

cd "${PROJECT_DIR}"

# Сеть после загрузки поднимается не мгновенно — даём ей время.
sleep 20

# Перезапускаем бота, если он упал: телефон — среда нестабильная.
while true; do
    echo "$(date -Iseconds) запуск бота" >> "${LOG_DIR}/boot.log"
    bash termux/run.sh bot >> "${LOG_DIR}/boot.log" 2>&1 || true
    echo "$(date -Iseconds) бот остановился, перезапуск через 30 с" >> "${LOG_DIR}/boot.log"
    sleep 30
done
