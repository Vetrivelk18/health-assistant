"""
Telegram Bot API client.

Thin async wrapper around the subset of the Bot API this project needs:
sending messages, and registering the webhook URL so Telegram knows where
to POST updates.
"""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API responds with ok: false."""

    def __init__(self, description: str, error_code: int | None = None):
        super().__init__(f"{error_code}: {description}")
        self.error_code = error_code
        self.description = description


class TelegramClient:
    def __init__(self, bot_token: str, timeout: float = 15.0):
        self.bot_token = bot_token
        self.timeout = timeout
        self._base = f"{API_BASE}/bot{bot_token}"

    async def send_message(
        self, chat_id: str | int, text: str, *, parse_mode: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        return self._result(await self._post("sendMessage", payload))

    async def set_webhook(self, url: str, *, secret_token: str | None = None) -> dict[str, Any]:
        """Register the webhook URL.

        `secret_token` is echoed back by Telegram in the
        X-Telegram-Bot-Api-Secret-Token header on every delivery, which is
        what lets the endpoint tell a real update from a forged one. Telegram
        restricts it to 1-256 chars of A-Z, a-z, 0-9, _ and -.
        """
        payload: dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._result(await self._post("setWebhook", payload))

    async def _post(self, method: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                return await c.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as e:
            # Surfaced as a TelegramError so callers need only one except
            # clause — a timeout here would otherwise escape as raw httpx.
            raise TelegramError(f"{type(e).__name__}: {e}", None) from e

    @staticmethod
    def _result(r: httpx.Response) -> dict[str, Any]:
        try:
            data = r.json()
        except ValueError as e:
            # Telegram returns HTML on some gateway errors; treat that as a
            # normal API failure rather than letting a JSON error escape.
            raise TelegramError(f"non-JSON response (HTTP {r.status_code})", r.status_code) from e

        if not data.get("ok"):
            raise TelegramError(data.get("description", "unknown error"), data.get("error_code"))
        return data.get("result", data)
