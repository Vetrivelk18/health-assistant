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

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self._base}/sendMessage", json=payload)
        return self._result(r)

    async def set_webhook(self, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self._base}/setWebhook", json={"url": url})
        return self._result(r)

    @staticmethod
    def _result(r: httpx.Response) -> dict[str, Any]:
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(data.get("description", "unknown error"), data.get("error_code"))
        return data.get("result", data)
