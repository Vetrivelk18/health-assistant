"""
OAuth 2.0 Authentication Routes
Handles /login (start OAuth flow) and /callback (receive auth code)
"""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from models import User, OAuthToken
from services.google_health import GoogleHealthClient, GoogleHealthError
from config import settings
from utils import oauth_state

logger = logging.getLogger(__name__)

router = APIRouter()

google_health_client = GoogleHealthClient(
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    redirect_uri=settings.GOOGLE_REDIRECT_URI,
    scopes=settings.GOOGLE_HEALTH_SCOPES,
)

# No server-side state store on purpose — the state token is signed and
# self-contained, so any instance can verify one it didn't issue. See
# utils/oauth_state.py for why a dict was wrong here.


def _naive_utc(dt: datetime) -> datetime:
    """Strip tzinfo after normalizing to UTC — DB columns store naive UTC."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@router.get("/login")
async def start_oauth_flow(telegram_chat_id: str, db: Session = Depends(get_db)):
    """
    Initiate OAuth 2.0 authorization flow
    User calls this endpoint, gets redirected to Google's auth page

    Args:
        telegram_chat_id: User's Telegram chat ID

    Returns:
        auth_url: URL for user to visit for authorization
    """
    # Signed, self-contained state for CSRF protection — carries the chat id
    # and its own expiry, so the callback can be verified by any instance.
    state = oauth_state.issue(telegram_chat_id, settings.SECRET_KEY)

    # Get authorization URL from Google
    auth_url = google_health_client.authorization_url(state)

    logger.info(f"📍 OAuth flow initiated for Telegram chat {telegram_chat_id}")
    logger.info(f"🔗 Auth URL: {auth_url}")

    return {
        "auth_url": auth_url,
        "message": "Click the link above to authorize with Google Health (Fitbit)",
    }


@router.get("/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    OAuth callback endpoint
    Google redirects user here after they authorize

    Args:
        code: Authorization code from Google
        state: State parameter (for CSRF protection)

    Returns:
        success message with user info
    """
    # Verify the signature and expiry, and recover which chat started this.
    # The signature is what stops a caller supplying someone else's chat id
    # and having tokens written against their account.
    try:
        telegram_chat_id = oauth_state.verify(state, settings.SECRET_KEY)
    except oauth_state.OAuthStateError as e:
        logger.error(f"🚨 Rejected OAuth callback state: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid state parameter: {e}")

    try:
        # Exchange code for tokens
        tokens = await google_health_client.exchange_code(code)

        logger.info(f"✅ Got tokens from Google for {telegram_chat_id}")

        access_token = tokens.access_token
        refresh_token = tokens.refresh_token
        expires_at = _naive_utc(tokens.expires_at)

        # Real identity from the id_token, with the old synthesized values
        # as a fallback so a missing id_token can't fail the connection.
        # Both columns are UNIQUE, so placeholders derived from the chat id
        # were a latent collision waiting for a second account.
        google_user_id = tokens.google_user_id or f"google_{telegram_chat_id}"
        email = tokens.email or f"telegram_{telegram_chat_id}@local"

        # Get or create user
        user = db.query(User).filter(User.telegram_chat_id == telegram_chat_id).first()

        if not user:
            # Create new user
            user = User(
                telegram_chat_id=telegram_chat_id,
                google_user_id=google_user_id,
                email=email,
                connected=True,
            )
            db.add(user)
            db.commit()
            logger.info(f"👤 Created new user: {telegram_chat_id}")
        else:
            # Update existing user, upgrading any placeholder identity
            # written before the openid/email scopes were requested.
            user.connected = True
            if tokens.google_user_id:
                user.google_user_id = tokens.google_user_id
            if tokens.email:
                user.email = tokens.email
            db.commit()
            logger.info(f"👤 Updated user: {telegram_chat_id}")

        # Store tokens in database
        oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == user.id).first()

        # This is a full consent flow, so the 7-day Testing-mode clock on
        # the refresh token restarts now — and any earlier "please
        # reconnect" nag is resolved.
        now = datetime.utcnow()

        if oauth_token:
            # Update existing token record
            oauth_token.access_token = access_token
            oauth_token.refresh_token = refresh_token
            oauth_token.expires_at = expires_at
            oauth_token.updated_at = now
            oauth_token.refresh_token_issued_at = now
            oauth_token.reconnect_notified_at = None
        else:
            # Create new token record
            oauth_token = OAuthToken(
                user_id=user.id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scope=tokens.scope or " ".join(google_health_client.scopes),
                refresh_token_issued_at=now,
            )
            db.add(oauth_token)

        db.commit()
        logger.info(f"✅ Stored OAuth tokens for {telegram_chat_id}, expires at {expires_at}")

        return {
            "status": "success",
            "message": f"✅ Successfully connected! You can now receive daily health summaries.",
            "user_id": user.id,
            "telegram_chat_id": telegram_chat_id,
            "connected": True,
            "token_expires_at": expires_at.isoformat(),
        }

    except GoogleHealthError as e:
        logger.error(f"🚨 OAuth callback error: {e}")
        status = e.status if 400 <= e.status < 500 else 502
        raise HTTPException(status_code=status, detail=f"OAuth error: {e}")
    except Exception as e:
        logger.error(f"🚨 OAuth callback error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"OAuth error: {str(e)}")


@router.post("/refresh-token")
async def refresh_token_endpoint(user_id: str, db: Session = Depends(get_db)):
    """
    Manually refresh an OAuth token
    Called when access token is expired or expiring soon

    Args:
        user_id: User ID in database

    Returns:
        New access token info
    """
    oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()

    if not oauth_token or not oauth_token.refresh_token:
        logger.error(f"🚨 No refresh token found for user {user_id}")
        raise HTTPException(status_code=404, detail="Refresh token not found")

    try:
        # Exchange refresh token for new access token
        tokens = await google_health_client.refresh(oauth_token.refresh_token)

        access_token = tokens.access_token
        expires_at = _naive_utc(tokens.expires_at)

        # Update token in database
        oauth_token.access_token = access_token
        oauth_token.expires_at = expires_at
        if tokens.refresh_token:
            oauth_token.refresh_token = tokens.refresh_token
        oauth_token.updated_at = datetime.utcnow()
        db.commit()

        logger.info(f"✅ Refreshed OAuth token for user {user_id}")

        return {
            "status": "success",
            "message": "Token refreshed",
            "access_token": access_token,
            "expires_at": expires_at.isoformat(),
        }

    except GoogleHealthError as e:
        logger.error(f"🚨 Token refresh error: {e}")
        if "invalid_grant" in e.body:
            logger.error("   invalid_grant usually means the consent screen is in "
                         "'Testing', where refresh tokens expire after 7 days.")
        status = e.status if 400 <= e.status < 500 else 502
        raise HTTPException(status_code=status, detail=f"Token refresh failed: {e}")
    except Exception as e:
        logger.error(f"🚨 Token refresh error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {str(e)}")


@router.get("/status/{user_id}")
async def auth_status(user_id: str, db: Session = Depends(get_db)):
    """
    Check if user is authenticated and token status

    Args:
        user_id: User ID in database

    Returns:
        Authentication status and token expiration info
    """
    oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()

    if not oauth_token:
        return {
            "authenticated": False,
            "message": "No OAuth token found",
        }

    return {
        "authenticated": True,
        "expired": oauth_token.is_expired,
        "expiring_soon": oauth_token.is_expiring_soon,
        "expires_at": oauth_token.expires_at.isoformat(),
        "scope": oauth_token.scope,
        "created_at": oauth_token.created_at.isoformat(),
    }


@router.get("/disconnect/{user_id}")
async def disconnect_account(user_id: str, db: Session = Depends(get_db)):
    """
    Disconnect user's Google Health account
    Deletes stored tokens and marks user as disconnected

    Args:
        user_id: User ID in database

    Returns:
        Disconnection confirmation
    """
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        # Delete OAuth token
        db.query(OAuthToken).filter(OAuthToken.user_id == user_id).delete()

        # Mark user as disconnected
        user.connected = False
        user.updated_at = datetime.utcnow()

        db.commit()
        logger.info(f"✅ Disconnected user {user_id}")

        return {
            "status": "success",
            "message": "Account disconnected successfully",
            "user_id": user_id,
        }

    except Exception as e:
        logger.error(f"🚨 Disconnection error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Disconnection failed: {str(e)}")
