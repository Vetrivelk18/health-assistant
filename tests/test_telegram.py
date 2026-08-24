"""
Tests for the Telegram webhook (routes/telegram.py).
Mocks the outbound Telegram send and calls into Gemini/Google Health.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from database import SessionLocal
from models import OAuthToken, User

client = TestClient(app)

TEST_CHAT_ID = "pytest_telegram_chat_id"


def _update(text: str, chat_id: str = TEST_CHAT_ID, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture(autouse=True)
def cleanup_test_user():
    yield
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == TEST_CHAT_ID).first()
        if user:
            db.query(OAuthToken).filter(OAuthToken.user_id == user.id).delete()
            db.delete(user)
            db.commit()
    finally:
        db.close()


@pytest.fixture
def connected_user():
    db = SessionLocal()
    user = User(
        telegram_chat_id=TEST_CHAT_ID,
        google_user_id=f"google_{TEST_CHAT_ID}",
        email=f"telegram_{TEST_CHAT_ID}@local",
        connected=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    oauth_token = OAuthToken(
        user_id=user.id,
        access_token="fake_access_token",
        refresh_token="fake_refresh_token",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        scope="",
    )
    db.add(oauth_token)
    db.commit()

    user_id = user.id
    db.close()
    yield user_id


def test_webhook_ignores_update_without_message():
    with patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json={"update_id": 1})

    assert response.status_code == 200
    mock_send.assert_not_awaited()


def test_start_command_sends_connect_link():
    with patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json=_update("/start"))

    assert response.status_code == 200
    mock_send.assert_awaited_once()
    chat_id, text = mock_send.await_args.args
    assert chat_id == TEST_CHAT_ID
    assert "https://accounts.google.com/" in text


def test_status_not_connected():
    with patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json=_update("/status"))

    assert response.status_code == 200
    assert "not connected" in mock_send.await_args.args[1]


def test_status_connected(connected_user):
    with patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json=_update("/status"))

    assert response.status_code == 200
    assert "Connected" in mock_send.await_args.args[1]


def test_plain_text_query_answered_by_gemini(connected_user):
    with patch(
        "routes.telegram.gemini.answer_health_query",
        new=AsyncMock(return_value="You slept 7 hours last night."),
    ), patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json=_update("how did I sleep?"))

    assert response.status_code == 200
    mock_send.assert_awaited_once_with(TEST_CHAT_ID, "You slept 7 hours last night.")


def test_plain_text_query_from_unconnected_user_prompts_connect():
    with patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json=_update("how did I sleep?"))

    assert response.status_code == 200
    assert "/connect" in mock_send.await_args.args[1]


def test_disconnect_command(connected_user):
    with patch("routes.telegram.telegram_client.send_message", new=AsyncMock()) as mock_send:
        response = client.post("/webhook/telegram", json=_update("/disconnect"))

    assert response.status_code == 200
    assert "Disconnected" in mock_send.await_args.args[1]

    db = SessionLocal()
    user = db.query(User).filter(User.telegram_chat_id == TEST_CHAT_ID).first()
    assert user.connected is False
    db.close()


# ------------------------------------------------- webhook authentication ----

import routes.telegram as telegram_module  # noqa: E402

WEBHOOK_SECRET = "pytest-webhook-secret"


def _post_update(update: dict, secret=None):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {}
    return client.post("/webhook/telegram", json=update, headers=headers)


def test_forged_update_without_secret_is_rejected():
    """The webhook URL is public — the shared secret is the only thing
    separating a real update from a spoofed one."""
    with patch.object(telegram_module.settings, "TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET):
        response = _post_update(_update("/status"))
    assert response.status_code == 403


def test_update_with_wrong_secret_is_rejected():
    with patch.object(telegram_module.settings, "TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET):
        response = _post_update(_update("/status"), secret="not-the-secret")
    assert response.status_code == 403


def test_update_with_correct_secret_is_accepted():
    with patch.object(
        telegram_module.settings, "TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET
    ), patch.object(
        telegram_module.telegram_client, "send_message", new=AsyncMock()
    ):
        response = _post_update(_update("/status"), secret=WEBHOOK_SECRET)
    assert response.status_code == 200


def test_unset_secret_fails_closed_in_production():
    """Forgetting to configure the secret must not silently leave the
    endpoint open — in production that's a 403, not a fallback."""
    with patch.object(
        telegram_module.settings, "TELEGRAM_WEBHOOK_SECRET", None
    ), patch.object(
        telegram_module.settings, "FASTAPI_ENV", "production"
    ):
        response = _post_update(_update("/status"))
    assert response.status_code == 403


def test_unset_secret_allows_local_development():
    with patch.object(
        telegram_module.settings, "TELEGRAM_WEBHOOK_SECRET", None
    ), patch.object(
        telegram_module.settings, "FASTAPI_ENV", "development"
    ), patch.object(
        telegram_module.telegram_client, "send_message", new=AsyncMock()
    ):
        response = _post_update(_update("/status"))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_set_webhook_registers_the_secret():
    """Validating the header is useless if setWebhook never registered the
    secret with Telegram in the first place."""
    from services.telegram_bot import TelegramClient

    captured = {}

    async def fake_post(method, payload):
        captured["method"] = method
        captured["payload"] = payload
        import httpx
        return httpx.Response(200, json={"ok": True, "result": True})

    tg = TelegramClient("token")
    with patch.object(tg, "_post", new=fake_post):
        await tg.set_webhook("https://example.com/webhook/telegram", secret_token="s3cr3t")

    assert captured["method"] == "setWebhook"
    assert captured["payload"]["secret_token"] == "s3cr3t"
