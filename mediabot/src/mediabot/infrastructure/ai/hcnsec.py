"""OpenAI-compatible client for the api.hcnsec.cn gateway."""

from __future__ import annotations

import os
from typing import Any, Literal

import aiohttp
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AISettings(BaseSettings):
    """AI chat settings loaded from ``AI__*`` environment variables."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("MEDIABOT_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        env_prefix="AI__",
        extra="ignore",
        case_sensitive=False,
    )

    enabled: bool = True
    base_url: str = "https://api.hcnsec.cn/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "auto"
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    max_history_messages: int = Field(default=12, ge=2, le=40)
    max_output_tokens: int = Field(default=1800, ge=128, le=8192)
    default_group_mode: Literal["mention", "all"] = "mention"

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key.get_secret_value())

    @property
    def normalized_base_url(self) -> str:
        value = self.base_url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/models"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
        return value.rstrip("/")


class AIAPIError(RuntimeError):
    """A safe, structured upstream API failure."""

    def __init__(self, status: int | None, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}" if status else detail)


def extract_answer(payload: dict[str, Any]) -> str:
    """Extract text from an OpenAI-compatible Chat Completions response."""
    choices = payload.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message", {})
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                pieces.append(text)
            elif isinstance(text, dict) and isinstance(text.get("value"), str):
                pieces.append(text["value"])
        return "\n".join(pieces).strip()
    return ""


class HcnsecAIClient:
    """Small asynchronous client with no dependency on an OpenAI SDK."""

    def __init__(self, settings: AISettings | None = None) -> None:
        self.settings = settings or AISettings()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def list_models(self) -> list[str]:
        if not self.settings.configured:
            return []
        payload = await self._request("GET", "/models")
        raw = payload.get("data", [])
        if not isinstance(raw, list):
            return []
        models = [
            str(item["id"]).strip()
            for item in raw
            if isinstance(item, dict) and item.get("id")
        ]
        return list(dict.fromkeys(model for model in models if model))

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> str:
        if not self.settings.configured:
            raise AIAPIError(None, "AI__API_KEY не настроен")

        payload = await self._request(
            "POST",
            "/chat/completions",
            json={
                "model": model or self.settings.model,
                "messages": messages,
                "max_tokens": self.settings.max_output_tokens,
            },
        )
        answer = extract_answer(payload)
        if not answer:
            raise AIAPIError(None, "нейросеть вернула пустой ответ")
        return answer

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_seconds)
        url = f"{self.settings.normalized_base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
                async with session.request(method, url, json=json) as response:
                    try:
                        payload: Any = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = {"error": {"message": (await response.text())[:500]}}

                    if response.status >= 400:
                        raise AIAPIError(response.status, _error_detail(payload))
                    if not isinstance(payload, dict):
                        raise AIAPIError(response.status, "сервер вернул неверный JSON")
                    return payload
        except TimeoutError as exc:
            raise AIAPIError(None, "истекло время ожидания API") from exc
        except aiohttp.ClientError as exc:
            raise AIAPIError(None, f"ошибка сети: {exc}") from exc


def _error_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:500]
        if isinstance(error, str):
            return error[:500]
        if isinstance(payload.get("message"), str):
            return str(payload["message"])[:500]
    return "API отклонил запрос"
