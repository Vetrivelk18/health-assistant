"""
Telegram webhook handler.

Receives Telegram Update objects at POST /webhook/telegram, routes commands
(/start, /connect, /disconnect, /status) and plain-text questions into the
existing OAuth (routes/auth.py) and Gemini/MCP (routes/mcp_tools.py,
services/gemini.py) pipelines, and replies over the Bot API. This is the
"Interactive Query Pipeline" entry point described in README.md.
"""

import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import OAuthToken, User
from routes import auth as auth_routes
from routes.mcp_tools import _valid_access_token, google_health_client
from services import gemini
from services.telegram_bot import TelegramClient, TelegramError

logger = logging.getLogger(__name__)

router = APIRouter()

telegram_client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)

NOT_CONNECTED_MESSAGE = "You're not connected yet — send /connect to link your Fitbit/Pixel Watch account."


def verify_telegram_secret(
    x_telegram_bot_api_secret_token: str = Header(default=None),
) -> None:
    """Reject updates that didn't come from Telegram.

    This endpoint must stay publicly reachable — Telegram can't authenticate
    with OIDC the way Cloud Scheduler does — so the only thing separating a
    real update from a forged one is this shared secret, which Telegram
    echoes back on every delivery after setWebhook(secret_token=...).

    Without it, anyone who learns the URL can POST an update carrying
    another user's chat_id: burn their Gemini quota, read summaries back, or
    spoof /disconnect.

    Compared with compare_digest to avoid leaking the secret a byte at a
    time through response-timing differences.
    """
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected:
        # Fail open only outside production, so local dev and tests work
        # without a secret — but never silently in production.
        if settings.FASTAPI_ENV != "development":
            logger.error("🚨 TELEGRAM_WEBHOOK_SECRET is unset in production — refusing webhook")
            raise HTTPException(status_code=403, detail="Webhook not configured")
        return

    provided = x_telegram_bot_api_secret_token or ""
    if not secrets.compare_digest(provided, expected):
        logger.warning("🚨 Rejected Telegram webhook with bad or missing secret token")
        raise HTTPException(status_code=403, detail="Invalid secret token")


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    _: None = Depends(verify_telegram_secret),
    db: Session = Depends(get_db),
):
    """
    Telegram calls this for every update. Always return 200 — a non-2xx
    response makes Telegram retry, and repeated failures get the webhook
    disabled. (The 403 from the secret-token check above is deliberate and
    happens before this: a forged request should be rejected, not absorbed.)
    """
    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return {"ok": True}

    chat_id = str(message["chat"]["id"])
    text = message["text"].strip()

    try:
        reply = await _handle_message(chat_id, text, db)
        if reply:
            await telegram_client.send_message(chat_id, reply)
    except TelegramError as e:
        logger.error(f"🚨 Telegram send failed for {chat_id}: {e}")
    except Exception as e:
        logger.error(f"🚨 Telegram webhook error for {chat_id}: {e}")
        try:
            await telegram_client.send_message(
                chat_id, "Something went wrong on my end — try again in a moment."
            )
        except TelegramError:
            pass

    return {"ok": True}


async def _handle_message(chat_id: str, text: str, db: Session) -> str:
    command = text.split()[0].lower() if text else ""

    if command in ("/start", "/connect"):
        return await _start_connect(chat_id, db)
    if command == "/disconnect":
        return await _disconnect(chat_id, db)
    if command == "/status":
        return await _status(chat_id, db)

    return await _answer_query(chat_id, text, db)


async def _start_connect(chat_id: str, db: Session) -> str:
    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    if user and user.connected:
        return "You're already connected! Ask me a health question, or send /disconnect to unlink."

    response = await auth_routes.start_oauth_flow(telegram_chat_id=chat_id, db=db)
    return f"Click below to connect your Fitbit/Pixel Watch data:\n{response['auth_url']}"


async def _disconnect(chat_id: str, db: Session) -> str:
    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    if not user or not user.connected:
        return "You're not connected, so there's nothing to disconnect."

    await auth_routes.disconnect_account(user_id=user.id, db=db)
    return "Disconnected. Send /connect any time to link your account again."


async def _status(chat_id: str, db: Session) -> str:
    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    oauth_token = user and db.query(OAuthToken).filter(OAuthToken.user_id == user.id).first()
    if not user or not user.connected or not oauth_token:
        return NOT_CONNECTED_MESSAGE

    state = "expired, will refresh automatically" if oauth_token.is_expired else "active"
    return f"✅ Connected. Token status: {state}."


async def _answer_query(chat_id: str, text: str, db: Session) -> str:
    user = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    oauth_token = user and db.query(OAuthToken).filter(OAuthToken.user_id == user.id).first()
    if not user or not user.connected or not oauth_token:
        return NOT_CONNECTED_MESSAGE

    try:
        access_token = await _valid_access_token(oauth_token, db)
    except HTTPException as e:
        logger.warning(f"Telegram query auth issue for {chat_id}: {e.detail}")
        return "Your connection needs to be refreshed — send /connect to reconnect."

    return await gemini.answer_health_query(text, access_token, google_health_client)
