#!/data/data/com.termux/files/usr/bin/bash
# Configure the api.hcnsec.cn key and model without printing the secret.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"

[ -f "${ENV_FILE}" ] || { echo ".env не найден. Сначала: bash termux/install.sh"; exit 1; }

read -r -s -p "API-ключ с https://api.hcnsec.cn/keys: " AI_KEY
printf "\n"
[ -n "${AI_KEY}" ] || { echo "Ключ не введён"; exit 1; }

read -r -p "Модель [Enter = auto]: " AI_MODEL
AI_MODEL="${AI_MODEL:-auto}"

python - "${ENV_FILE}" "${AI_KEY}" "${AI_MODEL}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
model = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()

updates = {
    "AI__ENABLED": "true",
    "AI__BASE_URL": "https://api.hcnsec.cn/v1",
    "AI__API_KEY": key,
    "AI__MODEL": model,
}
seen = set()
out = []
for line in lines:
    name = line.split("=", 1)[0].strip() if "=" in line else ""
    if name in updates:
        out.append(f"{name}={updates[name]}")
        seen.add(name)
    else:
        out.append(line)

if seen != updates.keys():
    out.extend(["", "# --- ИИ-чат через api.hcnsec.cn ---"])
    for name, value in updates.items():
        if name not in seen:
            out.append(f"{name}={value}")

path.write_text("\n".join(out) + "\n", encoding="utf-8")
path.chmod(0o600)
PY

echo "Проверяю ключ и модель..."
set -a
source "${ENV_FILE}"
set +a

cd "${PROJECT_DIR}"
source "${PROJECT_DIR}/.venv/bin/activate"

python - <<'PY'
import asyncio
from mediabot.infrastructure.ai import AISettings, HcnsecAIClient

async def main() -> None:
    settings = AISettings()
    client = HcnsecAIClient(settings)
    models = await client.list_models()
    if not models:
        raise SystemExit("Сервис не вернул модели — проверь ключ")
    if settings.model != "auto" and settings.model not in models:
        raise SystemExit(f"Модель {settings.model!r} отсутствует в списке")
    answer = await client.complete(
        [{"role": "user", "content": "Ответь одним словом: ГОТОВО"}]
    )
    print(f"Готово. Модель: {settings.model}; проверка: {answer[:60]}")

asyncio.run(main())
PY
