"""Structured logging built on loguru.

Architecture note
-----------------
The specification asks for *separate* log streams (INFO, WARNING, ERROR,
DOWNLOAD, PAYMENTS, SECURITY, ADMIN).  Loguru has no notion of "loggers", so
we model channels with a bound ``channel`` extra field and route each channel
to its own rotating file with a ``filter``.

Usage::

    from mediabot.core.logging import LogChannel, get_logger

    log = get_logger(LogChannel.DOWNLOAD)
    log.info("download finished", extra={"download_id": 42})

Everything also lands on stdout, which is what Docker/Loki consume.
"""

from __future__ import annotations

import logging
import sys
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Any

from loguru import logger

from mediabot.core.config import ObservabilitySettings


class LogChannel(StrEnum):
    """Logical log destinations, each with its own file."""

    APP = "app"
    DOWNLOAD = "download"
    PAYMENTS = "payments"
    SECURITY = "security"
    ADMIN = "admin"
    QUEUE = "queue"
    API = "api"


#: Channels that get a dedicated file plus severity-split files.
_SEVERITY_FILES: dict[str, str] = {
    "info.log": "INFO",
    "warning.log": "WARNING",
    "error.log": "ERROR",
}

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[channel]}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

_configured = False


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging (aiogram, SQLAlchemy, uvicorn) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue code
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.bind(channel=LogChannel.APP.value).opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _channel_filter(channel: LogChannel) -> Any:
    def _filter(record: dict[str, Any]) -> bool:
        return bool(record["extra"].get("channel") == channel.value)

    return _filter


def _severity_filter(level: str) -> Any:
    threshold = logger.level(level).no

    def _filter(record: dict[str, Any]) -> bool:
        if level == "INFO":
            # info.log keeps INFO only, so operators can read a clean timeline.
            return bool(record["level"].no == threshold)
        return bool(record["level"].no >= threshold)

    return _filter


def configure_logging(settings: ObservabilitySettings) -> None:
    """Install console + per-channel file sinks. Safe to call more than once."""
    global _configured
    if _configured:
        return

    logger.remove()
    logger.configure(extra={"channel": LogChannel.APP.value})

    logger.add(
        sys.stdout,
        level=settings.log_level,
        format=_CONSOLE_FORMAT,
        serialize=settings.json_logs,
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )

    log_dir = Path(settings.log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - read-only FS in some test sandboxes
        _configured = True
        return

    common: dict[str, Any] = {
        "rotation": settings.log_rotation,
        "retention": settings.log_retention,
        "compression": "gz",
        "enqueue": True,
        "encoding": "utf-8",
        "serialize": settings.json_logs,
    }

    for filename, level in _SEVERITY_FILES.items():
        logger.add(log_dir / filename, level=level, filter=_severity_filter(level), **common)

    for channel in LogChannel:
        if channel is LogChannel.APP:
            continue
        logger.add(
            log_dir / f"{channel.value}.log",
            level="DEBUG",
            filter=_channel_filter(channel),
            **common,
        )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for noisy in ("aiogram", "asyncio", "sqlalchemy.engine", "uvicorn", "uvicorn.access", "celery"):
        stdlib_logger = logging.getLogger(noisy)
        stdlib_logger.handlers = [InterceptHandler()]
        stdlib_logger.propagate = False

    _configured = True


def get_logger(channel: LogChannel = LogChannel.APP, **context: Any) -> Any:
    """Return a loguru logger bound to ``channel`` and optional context."""
    return logger.bind(channel=channel.value, **context)
