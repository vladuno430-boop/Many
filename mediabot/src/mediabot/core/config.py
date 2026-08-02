"""Typed application configuration.

Architecture note
-----------------
Configuration is the only place in the code base that reads the process
environment.  Everything else receives a fully validated :class:`Settings`
object through dependency injection, which keeps the domain and application
layers free of environment lookups and makes them trivially testable.

Settings are grouped into cohesive nested models.  ``pydantic-settings`` maps
them to environment variables with a double-underscore delimiter, e.g.::

    DB__HOST=postgres      ->  settings.db.host
    SECURITY__JWT_SECRET   ->  settings.security.jwt_secret

The object is cached (:func:`get_settings`) so that every worker, the bot and
the API share exactly one immutable instance per process.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "production"]


def _split_csv(raw: str | list[str] | None) -> list[str]:
    """Parse a comma separated environment value into a clean list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


class AppSettings(BaseModel):
    """Generic runtime settings that do not belong to a specific subsystem."""

    env: Environment = "local"
    debug: bool = False
    name: str = "MediaBot"
    timezone: str = "UTC"
    storage_dir: Path = Path("/var/lib/mediabot/storage")
    temp_dir: Path = Path("/var/lib/mediabot/tmp")
    artifact_ttl_minutes: int = Field(default=120, ge=5, le=10_080)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def ensure_directories(self) -> None:
        """Create the working directories if they are missing.

        Called once during start-up by each entry point; the workers and the
        bot share the same volume in Docker Compose.
        """
        for directory in (self.storage_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)


class TelegramSettings(BaseModel):
    """Bot transport settings (token, webhook, upload constraints)."""

    bot_token: SecretStr = SecretStr("")
    bot_username: str = "mediabot"
    root_admin_ids: list[int] = Field(default_factory=list)
    webhook_url: str = ""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr = SecretStr("")
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    api_server: str = ""

    @field_validator("root_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [int(item) for item in _split_csv(value)]
        if isinstance(value, list | tuple):
            return [int(item) for item in value]
        if isinstance(value, int):
            return [value]
        raise ValueError(f"Cannot parse admin ids from {value!r}")

    @property
    def use_webhook(self) -> bool:
        return bool(self.webhook_url)


class DatabaseSettings(BaseModel):
    """PostgreSQL connection settings."""

    host: str = "localhost"
    port: int = 5432
    name: str = "mediabot"
    user: str = "mediabot"
    password: SecretStr = SecretStr("mediabot")
    pool_size: int = Field(default=20, ge=1, le=200)
    max_overflow: int = Field(default=10, ge=0, le=200)
    pool_recycle_seconds: int = 1800
    echo: bool = False
    # Escape hatch used by the test-suite to point at SQLite/aiosqlite.
    url_override: str = ""

    @property
    def async_dsn(self) -> str:
        if self.url_override:
            return self.url_override
        return (
            f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def sync_dsn(self) -> str:
        """Synchronous DSN — required by Alembic and the Celery workers."""
        if self.url_override:
            return self.url_override.replace("+asyncpg", "").replace("+aiosqlite", "")
        return (
            f"postgresql+psycopg2://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class RedisSettings(BaseModel):
    """Redis connection settings; logical databases are separated by concern."""

    host: str = "localhost"
    port: int = 6379
    password: SecretStr = SecretStr("")
    db_cache: int = 0
    db_broker: int = 1
    db_result: int = 2
    db_fsm: int = 3

    def dsn(self, db: int) -> str:
        auth = f":{self.password.get_secret_value()}@" if self.password.get_secret_value() else ""
        return f"redis://{auth}{self.host}:{self.port}/{db}"

    @property
    def cache_dsn(self) -> str:
        return self.dsn(self.db_cache)

    @property
    def broker_dsn(self) -> str:
        return self.dsn(self.db_broker)

    @property
    def result_dsn(self) -> str:
        return self.dsn(self.db_result)

    @property
    def fsm_dsn(self) -> str:
        return self.dsn(self.db_fsm)


class SecuritySettings(BaseModel):
    """Authentication, encryption and abuse-prevention knobs."""

    jwt_secret: SecretStr = SecretStr("insecure-development-secret-change-me!!")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = Field(default=30, ge=1)
    refresh_token_ttl_days: int = Field(default=14, ge=1)
    encryption_key: SecretStr = SecretStr("")
    api_keys: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=30, ge=1)
    flood_threshold: int = Field(default=8, ge=2)
    flood_window_seconds: int = Field(default=5, ge=1)
    flood_ban_seconds: int = Field(default=300, ge=10)
    captcha_enabled: bool = True
    captcha_after_violations: int = Field(default=3, ge=1)
    allowed_admin_origins: list[str] = Field(default_factory=list)

    @field_validator("api_keys", "allowed_admin_origins", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value]
        return _split_csv(value if isinstance(value, str) else None)


class DownloaderSettings(BaseModel):
    """yt-dlp / FFmpeg execution parameters."""

    max_concurrent_jobs: int = Field(default=4, ge=1, le=64)
    job_timeout_seconds: int = Field(default=1800, ge=30)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: int = Field(default=10, ge=1)
    ffmpeg_path: str = "/usr/bin/ffmpeg"
    ffprobe_path: str = "/usr/bin/ffprobe"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    proxy: str = ""
    cookies_file: str = ""
    metadata_cache_ttl_seconds: int = Field(default=1800, ge=0)
    # Hard ceiling regardless of subscription — protects the disk.
    absolute_max_filesize_bytes: int = 8 * 1024**3


class PaymentSettings(BaseModel):
    """Payment provider configuration; each provider can be toggled."""

    currency: str = "RUB"
    stars_enabled: bool = True
    card_enabled: bool = False
    card_provider_token: SecretStr = SecretStr("")
    crypto_enabled: bool = False
    crypto_api_url: str = "https://pay.crypt.bot/api"
    crypto_api_token: SecretStr = SecretStr("")
    crypto_asset: str = "USDT"


class ApiSettings(BaseModel):
    """REST API and admin web panel settings."""

    host: str = "0.0.0.0"  # noqa: S104 — bound inside the container network
    port: int = 8000
    root_path: str = ""
    docs_enabled: bool = True
    cors_origins: list[str] = Field(default_factory=list)
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("admin")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value]
        return _split_csv(value if isinstance(value, str) else None)


class ObservabilitySettings(BaseModel):
    """Logging and metrics settings."""

    log_level: str = "INFO"
    log_dir: Path = Path("/var/log/mediabot")
    log_rotation: str = "100 MB"
    log_retention: str = "30 days"
    json_logs: bool = True
    metrics_enabled: bool = True


class EconomySettings(BaseModel):
    """Tunable numbers behind the coin economy and referral programme."""

    referral_bonus_coins: int = 50
    referral_invitee_coins: int = 25
    referral_premium_days_per_5: int = 1
    daily_bonus_coins: int = 10
    daily_streak_step_coins: int = 5
    daily_streak_max_coins: int = 100
    coins_per_extra_download: int = 15
    coins_per_premium_day: int = 100


class Settings(BaseSettings):
    """Root settings object — the single source of truth for configuration."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("MEDIABOT_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    app: Annotated[AppSettings, Field(default_factory=AppSettings)]
    telegram: Annotated[TelegramSettings, Field(default_factory=TelegramSettings)]
    db: Annotated[DatabaseSettings, Field(default_factory=DatabaseSettings)]
    redis: Annotated[RedisSettings, Field(default_factory=RedisSettings)]
    security: Annotated[SecuritySettings, Field(default_factory=SecuritySettings)]
    downloader: Annotated[DownloaderSettings, Field(default_factory=DownloaderSettings)]
    payments: Annotated[PaymentSettings, Field(default_factory=PaymentSettings)]
    api: Annotated[ApiSettings, Field(default_factory=ApiSettings)]
    observability: Annotated[ObservabilitySettings, Field(default_factory=ObservabilitySettings)]
    economy: Annotated[EconomySettings, Field(default_factory=EconomySettings)]

    @model_validator(mode="after")
    def _validate_production_invariants(self) -> Settings:
        """Fail fast on insecure production configuration.

        Booting a production deployment with development secrets is a security
        incident waiting to happen, so we refuse to start instead.
        """
        if self.app.env != "production":
            return self
        problems: list[str] = []
        if not self.telegram.bot_token.get_secret_value():
            problems.append("TELEGRAM__BOT_TOKEN is required")
        if len(self.security.jwt_secret.get_secret_value()) < 32:
            problems.append("SECURITY__JWT_SECRET must be at least 32 characters")
        if "change-me" in self.security.jwt_secret.get_secret_value():
            problems.append("SECURITY__JWT_SECRET still holds the placeholder value")
        if self.api.admin_password.get_secret_value() in {"admin", "change-me-admin-password"}:
            problems.append("API__ADMIN_PASSWORD still holds the placeholder value")
        if self.app.debug:
            problems.append("APP__DEBUG must be false in production")
        if problems:
            raise ValueError("Insecure production configuration: " + "; ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
