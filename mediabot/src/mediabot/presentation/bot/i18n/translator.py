"""Lightweight i18n for the bot.

Architecture note
-----------------
Locales are plain JSON files loaded once at import time.  A missing key falls
back to English and, failing that, to the key itself — a missing translation
therefore degrades into readable output instead of a ``KeyError`` in front of a
user.  Formatting uses ``str.format`` with keyword arguments only, so a locale
file can reorder placeholders freely.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from mediabot.core.logging import LogChannel, get_logger
from mediabot.domain.enums import Language

log = get_logger(LogChannel.APP, component="i18n")

LOCALES_DIR = Path(__file__).parent / "locales"


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, dict[str, str]]:
    """Read every ``<lang>.json`` file into memory."""
    catalog: dict[str, dict[str, str]] = {}
    for language in Language:
        path = LOCALES_DIR / f"{language.value}.json"
        if not path.exists():  # pragma: no cover - packaging issue
            log.warning("locale file missing: {}", path)
            catalog[language.value] = {}
            continue
        catalog[language.value] = json.loads(path.read_text(encoding="utf-8"))
    return catalog


class Translator:
    """Resolve translation keys for a given language."""

    def __init__(self, language: Language = Language.EN) -> None:
        self.language = language
        self._catalog = _load_catalog()

    def with_language(self, language: Language) -> Translator:
        return Translator(language)

    def get(self, key: str, **kwargs: Any) -> str:
        """Translate ``key``, formatting it with ``kwargs``."""
        template = (
            self._catalog.get(self.language.value, {}).get(key)
            or self._catalog.get(Language.EN.value, {}).get(key)
            or key
        )
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):  # pragma: no cover - bad locale data
            log.warning("could not format key={} lang={}", key, self.language.value)
            return template

    # Short alias used throughout the handlers: ``t("start.title")``.
    __call__ = get

    def has(self, key: str) -> bool:
        return key in self._catalog.get(self.language.value, {})


def translator_for(language: Language | str | None) -> Translator:
    """Build a translator for a language code, falling back to the default."""
    if isinstance(language, Language):
        return Translator(language)
    return Translator(Language.parse(language))


def all_translations(key: str) -> frozenset[str]:
    """Every translation of ``key`` across all locales.

    Used by handlers that react to reply-keyboard buttons: the caption arrives
    as plain text, so the filter must accept it in any supported language.
    """
    catalog = _load_catalog()
    values = {catalog.get(language.value, {}).get(key) for language in Language}
    return frozenset(value for value in values if value)


def available_languages() -> tuple[Language, ...]:
    """Languages that actually have a locale file with content."""
    catalog = _load_catalog()
    return tuple(language for language in Language if catalog.get(language.value))
