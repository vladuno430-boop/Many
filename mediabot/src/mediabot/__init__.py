"""MediaBot — production-ready Telegram multimedia downloader.

The package is organised in Clean Architecture layers::

    core            cross-cutting concerns (config, logging, security, DI)
    domain          entities, enums, value objects and pure business rules
    application     use-case orchestration (services + DTOs)
    infrastructure  adapters: PostgreSQL, Redis, yt-dlp, FFmpeg, Celery, payments
    presentation    delivery mechanisms: aiogram bot, REST API, admin web panel

Dependencies always point inwards: ``presentation`` and ``infrastructure`` know
about ``domain``, never the other way round.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
