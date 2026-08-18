"""
MCP tool definitions and the interactive health-query endpoint.

Exposes the tool schema Claude uses for health queries (services/claude.py)
for introspection, and /query which runs the full Claude + Google Health
tool-use loop for a connected user. This is what a future Telegram webhook
handler calls for the "Interactive Query Pipeline" described in README.md.
"""

import logging
from datetime import timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import OAuthToken
from services import claude
from services.google_health import GoogleHealthClient, GoogleHealthError

logger = logging.getLogger(__name__)

router = APIRouter()

google_health_client = GoogleHealthClient(
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    redirect_uri=settings.GOOGLE_REDIRECT_URI,
    scopes=settings.GOOGLE_HEALTH_SCOPES,
)


class QueryRequest(BaseModel):
    user_id: str
    message: str


@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """The tool schema Claude is given for interactive health queries."""
    return claude.tool_schema()


@router.post("/query")
async def run_query(body: QueryRequest, db: Session = Depends(get_db)):
    """
    Answer a natural-language health question for a connected user, letting
    Claude call back into the Google Health API via tool use as needed.
    """
    oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == body.user_id).first()
    if not oauth_token:
        raise HTTPException(status_code=404, detail="User has no connected Google Health account")

    access_token = await _valid_access_token(oauth_token, db)

    try:
        answer = await claude.answer_health_query(body.message, access_token, google_health_client)
    except GoogleHealthError as e:
        logger.error(f"🚨 MCP query error: {e}")
        raise HTTPException(status_code=502, detail=f"Google Health API error: {e}")

    return {"user_id": body.user_id, "message": body.message, "response": answer}


async def _valid_access_token(oauth_token: OAuthToken, db: Session) -> str:
    """Return a usable access token, refreshing it first if it's expiring soon."""
    if not oauth_token.is_expiring_soon:
        return oauth_token.access_token

    if not oauth_token.refresh_token:
        raise HTTPException(status_code=401, detail="Access token expired and no refresh token on file")

    try:
        tokens = await google_health_client.refresh(oauth_token.refresh_token)
    except GoogleHealthError as e:
        logger.error(f"🚨 Token refresh failed during MCP query: {e}")
        raise HTTPException(status_code=401, detail=f"Token refresh failed: {e}")

    oauth_token.access_token = tokens.access_token
    oauth_token.expires_at = tokens.expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    if tokens.refresh_token:
        oauth_token.refresh_token = tokens.refresh_token
    db.commit()

    return tokens.access_token
