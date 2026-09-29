"""Telegram Bot API client with bounded timeouts and redacted observability."""

import logging
import time
from typing import Any

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

from bancaemdia.config import get_settings
from bancaemdia.observability.metrics import telegram_api_errors_total, telegram_api_latency_seconds


class TelegramApiError(Exception):
    def __init__(self, reason: str, *, retryable: bool, retry_after: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = min(max(retry_after, 0), 300)


class TelegramClient:
    def __init__(
        self, token: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        settings = get_settings()
        self._token = token or settings.TELEGRAM_BOT_TOKEN
        if not self._token:
            raise TelegramApiError("not_configured", retryable=True)
        # httpx normally logs the complete request URL, which contains the bot token.
        logging.getLogger("httpx").setLevel(logging.ERROR)
        logging.getLogger("httpcore").setLevel(logging.ERROR)
        self._client = httpx.AsyncClient(
            base_url=settings.TELEGRAM_API_BASE_URL.rstrip("/"),
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, payload: dict[str, Any]) -> Any:
        start = time.perf_counter()
        outcome = "error"
        try:
            # Suppress OTel httpx URL attributes: Bot API URLs embed the token.
            with suppress_instrumentation():
                response = await self._client.post(f"/bot{self._token}/{method}", json=payload)
            if response.status_code == 429:
                try:
                    parameters = response.json().get("parameters", {})
                    retry_after = int(parameters.get("retry_after", 1))
                except (ValueError, TypeError, AttributeError):
                    retry_after = 1
                raise TelegramApiError("rate_limited", retryable=True, retry_after=retry_after)
            if response.status_code >= 500:
                raise TelegramApiError("server", retryable=True)
            if response.status_code >= 400:
                raise TelegramApiError("client", retryable=False)
            try:
                body = response.json()
            except ValueError as exc:
                raise TelegramApiError("invalid_response", retryable=True) from exc
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise TelegramApiError("invalid_response", retryable=True)
            outcome = "success"
            return body.get("result")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TelegramApiError("network", retryable=True) from exc
        except TelegramApiError as exc:
            telegram_api_errors_total.labels(method=method, reason=exc.reason).inc()
            raise
        finally:
            telegram_api_latency_seconds.labels(method=method, outcome=outcome).observe(
                time.perf_counter() - start
            )

    async def send_message(self, chat_id: int, message: str) -> int:
        if not message or len(message) > 4096:
            raise TelegramApiError("invalid_message", retryable=False)
        result = await self._request("sendMessage", {"chat_id": chat_id, "text": message})
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramApiError("invalid_response", retryable=True)
        return int(result["message_id"])

    async def webhook_info(self) -> dict[str, Any]:
        result = await self._request("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramApiError("invalid_response", retryable=True)
        return result

    async def get_updates(self, offset: int, *, timeout: int = 20) -> list[dict[str, Any]]:
        result = await self._request(
            "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100}
        )
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramApiError("invalid_response", retryable=True)
        return result
