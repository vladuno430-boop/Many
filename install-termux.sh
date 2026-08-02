#!/data/data/com.termux/files/usr/bin/bash
# One-command installer for MediaBot on Android/Termux.
set -euo pipefail

REPOSITORY="https://github.com/vladuno430-boop/Many.git"
BRANCH="agent/add-ai-group-chat-hcnsec"
REPO_DIR="${HOME}/Many"
PROJECT_DIR="${REPO_DIR}/mediabot"
CONFIG_DIR="${HOME}/.config/mediabot"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$1" >&2; exit 1; }

[ -n "${PREFIX:-}" ] || die "Запускай этот файл внутри Termux."

say "Обновляю Termux и ставлю Git, curl и tmux"
pkg update -y >/dev/null 2>&1 || warn "pkg update вернул ошибку, продолжаю"
pkg install -y git curl tmux

if [ -d "${REPO_DIR}/.git" ]; then
    say "Репозиторий уже есть — обновляю ветку ${BRANCH}"
    git -C "${REPO_DIR}" fetch origin "${BRANCH}"
    git -C "${REPO_DIR}" checkout "${BRANCH}"
    git -C "${REPO_DIR}" pull --ff-only origin "${BRANCH}"
elif [ -e "${REPO_DIR}" ]; then
    die "${REPO_DIR} существует, но это не Git-репозиторий. Переименуй или удали папку."
else
    say "Скачиваю MediaBot"
    git clone --depth 1 --branch "${BRANCH}" "${REPOSITORY}" "${REPO_DIR}"
fi

[ -f "${PROJECT_DIR}/termux/install.sh" ] || die "Не найден установщик MediaBot."

say "Устанавливаю MediaBot"
cd "${PROJECT_DIR}"
bash termux/install.sh

say "Добавляю короткую команду mediabot"
mkdir -p "${CONFIG_DIR}"
printf '%s\n' "${PROJECT_DIR}" > "${CONFIG_DIR}/project_dir"
chmod 700 "${CONFIG_DIR}"
chmod 600 "${CONFIG_DIR}/project_dir"
install -m 755 "${PROJECT_DIR}/termux/mediabot" "${PREFIX}/bin/mediabot"

# ИИ нужно настроить до фонового запуска: в tmux без открытого терминала
# невозможно безопасно спросить ключ.
if ! grep -Eq '^AI__API_KEY=.+$' "${PROJECT_DIR}/.env"; then
    say "Настраиваю ИИ-чат"
    bash "${PROJECT_DIR}/termux/configure-ai.sh"
fi

say "Запускаю бота в фоне"
mediabot start

cat <<'EOF'

Готово. Бот продолжит работать после закрытия окна Termux.

Полезные команды:
  mediabot status    проверить работу
  mediabot logs      смотреть логи
  mediabot stop      остановить
  mediabot start     запустить снова
  mediabot restart   перезапустить
  mediabot update    обновить бота
  mediabot setup-ai  заменить ключ или модель ИИ
  mediabot doctor    проверить установку
EOF
