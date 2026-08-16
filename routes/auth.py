"""
OAuth 2.0 Authentication Routes
Handles /login (start OAuth flow) and /callback (receive auth code)
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import httpx

from database import get_db, SessionLocal
from models import User, OAuthToken
from services.google_health import google_health_client
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Store OAuth state temporarily (in production, use Redis)
_oauth_states = {}


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
    # Generate random state for CSRF protection
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "telegram_chat_id": telegram_chat_id,
        "created_at": datetime.utcnow(),
    }

    # Get authorization URL from Google
    auth_url = google_health_client.get_authorization_url(state)

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
    # Verify state parameter
    if state not in _oauth_states:
        logger.error(f"🚨 Invalid OAuth state: {state}")
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    state_data = _oauth_states.pop(state)
    telegram_chat_id = state_data["telegram_chat_id"]

    # Check state expiration (5 minutes)
    if datetime.utcnow() - state_data["created_at"] > timedelta(minutes=5):
        logger.error(f"🚨 OAuth state expired for {telegram_chat_id}")
        raise HTTPException(status_code=400, detail="Authorization expired")

    try:
        # Exchange code for tokens
        tokens = await google_health_client.exchange_code_for_tokens(code)

        logger.info(f"✅ Got tokens from Google for {telegram_chat_id}")

        # Extract token info
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 3600)  # Default 1 hour

        # Calculate expiration time
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        # Get or create user
        user = db.query(User).filter(User.telegram_chat_id == telegram_chat_id).first()

        if not user:
            # Create new user
            user = User(
                telegram_chat_id=telegram_chat_id,
                google_user_id=f"google_{telegram_chat_id}",  # Placeholder
                email=f"telegram_{telegram_chat_id}@local",  # Placeholder
                connected=True,
            )
            db.add(user)
            db.commit()
            logger.info(f"👤 Created new user: {telegram_chat_id}")
        else:
            # Update existing user
            user.connected = True
            db.commit()
            logger.info(f"👤 Updated user: {telegram_chat_id}")

        # Store tokens in database
        oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == user.id).first()

        if oauth_token:
            # Update existing token record
            oauth_token.access_token = access_token
            oauth_token.refresh_token = refresh_token
            oauth_token.expires_at = expires_at
            oauth_token.updated_at = datetime.utcnow()
        else:
            # Create new token record
            oauth_token = OAuthToken(
                user_id=user.id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scope=" ".join(google_health_client.scopes),
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
        tokens = await google_health_client.refresh_access_token(oauth_token.refresh_token)

        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in", 3600)
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        # Update token in database
        oauth_token.access_token = access_token
        oauth_token.expires_at = expires_at
        oauth_token.updated_at = datetime.utcnow()
        db.commit()

        logger.info(f"✅ Refreshed OAuth token for user {user_id}")

        return {
            "status": "success",
            "message": "Token refreshed",
            "access_token": access_token,
            "expires_at": expires_at.isoformat(),
        }

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
