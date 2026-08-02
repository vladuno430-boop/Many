"""Диагностика окружения MediaBot в Termux.

Проверяет всё, что чаще всего ломается на телефоне: наличие ffmpeg, права на
каталоги, доступность базы, корректность .env и связь с Telegram.

Запуск: ``bash termux/run.sh doctor``
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
from pathlib import Path

GREEN, RED, YELLOW, RESET = "\033[1;32m", "\033[1;31m", "\033[1;33m", "\033[0m"

_failures = 0
_warnings = 0


def ok(message: str) -> None:
    print(f"  {GREEN}✔{RESET} {message}")


def fail(message: str) -> None:
    global _failures
    _failures += 1
    print(f"  {RED}✘{RESET} {message}")


def warn(message: str) -> None:
    global _warnings
    _warnings += 1
    print(f"  {YELLOW}!{RESET} {message}")


def section(title: str) -> None:
    print(f"\n{title}")


def check_python() -> None:
    section("Python")
    version = sys.version_info
    if version >= (3, 12):
        ok(f"Python {version.major}.{version.minor}.{version.micro}")
    elif version >= (3, 11):
        warn(f"Python {version.major}.{version.minor} — поддерживается, но рекомендуется 3.12")
    else:
        fail(f"Python {version.major}.{version.minor} слишком старый, нужен 3.11+")


def check_binaries() -> None:
    section("Внешние программы")
    for binary, required in (("ffmpeg", True), ("ffprobe", True), ("git", False)):
        path = shutil.which(binary)
        if path:
            ok(f"{binary}: {path}")
        elif required:
            fail(f"{binary} не найден — установите: pkg install ffmpeg")
        else:
            warn(f"{binary} не найден (необязательно)")


def check_packages() -> None:
    section("Python-пакеты")
    required = ["aiogram", "sqlalchemy", "alembic", "yt_dlp", "pydantic", "loguru", "jwt"]
    optional = {
        "bcrypt": "пароли админки будут хешироваться через PBKDF2 (это нормально)",
        "psutil": "детальные метрики CPU/RAM недоступны, health-check работает",
        "magic": "MIME определяется по расширению, а не по сигнатуре файла",
        "fastapi": "REST API и веб-панель не запустятся (боту не нужны)",
    }
    for name in required:
        try:
            __import__(name)
            ok(name)
        except ImportError:
            fail(f"{name} не установлен — переустановите: pip install -e '.[standalone]'")
    for name, consequence in optional.items():
        try:
            __import__(name)
            ok(f"{name} (опционально)")
        except ImportError:
            warn(f"{name} отсутствует: {consequence}")


def check_settings() -> None:
    section("Конфигурация")
    try:
        from mediabot.core.config import get_settings
    except ImportError as exc:
        fail(f"не удалось импортировать настройки: {exc}")
        return

    settings = get_settings()
    if settings.app.is_standalone:
        ok("режим: standalone (SQLite + встроенный воркер)")
    else:
        warn(
            "режим: distributed — для телефона поставьте APP__RUNTIME_MODE=standalone, "
            "иначе бот будет ждать PostgreSQL и Redis"
        )
    if settings.telegram.bot_token.get_secret_value():
        ok("TELEGRAM__BOT_TOKEN задан")
    else:
        fail("TELEGRAM__BOT_TOKEN пустой — бот не запустится")
    if settings.telegram.root_admin_ids:
        ok(f"админы: {settings.telegram.root_admin_ids}")
    else:
        warn("TELEGRAM__ROOT_ADMIN_IDS пуст — админ-панель в боте будет недоступна")
    if settings.redis.enabled and settings.app.is_standalone:
        warn("REDIS__ENABLED=true в standalone-режиме игнорируется")

    section("Каталоги")
    for label, directory in (
        ("storage", settings.app.storage_dir),
        ("tmp", settings.app.temp_dir),
        ("logs", settings.observability.log_dir),
    ):
        path = Path(directory)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            ok(f"{label}: {path} (запись доступна)")
        except OSError as exc:
            fail(f"{label}: {path} — нет доступа на запись ({exc})")

    section("База данных")
    if settings.db.is_sqlite:
        db_path = Path(settings.db.sqlite_path)
        if db_path.exists():
            try:
                connection = sqlite3.connect(db_path)
                tables = connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
                connection.close()
                if tables >= 20:
                    ok(f"SQLite: {db_path} ({tables} таблиц)")
                else:
                    warn(
                        f"SQLite: {db_path} — всего {tables} таблиц, "
                        "примените: bash termux/run.sh migrate"
                    )
            except sqlite3.Error as exc:
                fail(f"SQLite недоступна: {exc}")
        else:
            fail(f"файл базы не создан: {db_path} — выполните: bash termux/run.sh migrate")
    else:
        warn("настроен PostgreSQL — на телефоне обычно нужен SQLite (DB__BACKEND=sqlite)")


async def _check_telegram() -> None:
    from mediabot.core.config import get_settings

    settings = get_settings()
    token = settings.telegram.bot_token.get_secret_value()
    if not token:
        return
    try:
        from aiogram import Bot

        bot = Bot(token=token)
        try:
            me = await bot.get_me()
            ok(f"Telegram: подключение есть, бот @{me.username}")
        finally:
            await bot.session.close()
    except Exception as exc:
        fail(f"Telegram недоступен: {type(exc).__name__}: {exc}")


def check_telegram() -> None:
    section("Связь с Telegram")
    try:
        asyncio.run(_check_telegram())
    except Exception as exc:  # pragma: no cover - сетевые сбои
        fail(f"проверка не удалась: {exc}")


def check_disk() -> None:
    section("Свободное место")
    usage = shutil.disk_usage(os.path.expanduser("~"))
    free_gb = usage.free / 1024**3
    if free_gb >= 2:
        ok(f"свободно {free_gb:.1f} ГБ")
    elif free_gb >= 0.5:
        warn(f"свободно всего {free_gb:.1f} ГБ — крупные видео могут не поместиться")
    else:
        fail(f"свободно {free_gb:.1f} ГБ — недостаточно для загрузок")


def main() -> int:
    print("MediaBot — диагностика Termux")
    check_python()
    check_binaries()
    check_packages()
    check_settings()
    check_disk()
    check_telegram()

    print()
    if _failures:
        print(f"{RED}Найдено проблем: {_failures}{RESET} (предупреждений: {_warnings})")
        return 1
    if _warnings:
        print(f"{YELLOW}Всё работает, есть предупреждения: {_warnings}{RESET}")
        return 0
    print(f"{GREEN}Всё в порядке — можно запускать: bash termux/run.sh{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
