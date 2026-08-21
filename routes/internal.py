"""
Internal endpoints — not reachable by end users.

The Cloud Run service is deployed --allow-unauthenticated (Telegram has to
reach POST /webhook/telegram), so POST /internal/run-daily authenticates
its own caller instead: Cloud Scheduler is configured to attach a
Google-signed OIDC token, which we verify here (audience + the exact
service-account email the job runs as) before doing anything.

The whole run happens synchronously inside the request. Cloud Run freezes
CPU the moment a response is sent, so anything deferred to a background
task after that point would silently never run — there is no "fire and
forget" on this platform. Cloud Scheduler's attempt deadline (default 3
minutes, max 30) is what bounds this instead of a timeout here.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import HealthSummary, OAuthToken, User
from routes.mcp_tools import _valid_access_token, google_health_client
from routes.telegram import telegram_client
from services import gemini
from services.telegram_bot import TelegramError

logger = logging.getLogger(__name__)

router = APIRouter()

_google_auth_request = google_requests.Request()


def verify_scheduler_token(authorization: str = Header(default=None)) -> None:
    """Reject anything that isn't a valid OIDC token from the expected
    Cloud Scheduler service account."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Missing bearer token")

    token = authorization[len("Bearer "):]
    try:
        claims = google_id_token.verify_oauth2_token(
            token, _google_auth_request, audience=settings.RUN_DAILY_AUDIENCE
        )
    except ValueError as e:
        logger.warning(f"🚨 /internal/run-daily rejected an invalid OIDC token: {e}")
        raise HTTPException(status_code=403, detail="Invalid token")

    caller_email = claims.get("email")
    if not claims.get("email_verified") or caller_email != settings.SCHEDULER_SERVICE_ACCOUNT_EMAIL:
        logger.warning(f"🚨 /internal/run-daily rejected unexpected caller: {caller_email}")
        raise HTTPException(status_code=403, detail="Unexpected caller")


def _target_day(user: User) -> date:
    """'Yesterday' in the user's own timezone, not the container's (UTC)."""
    try:
        tz = ZoneInfo(user.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return (datetime.now(tz) - timedelta(days=1)).date()


@router.post("/run-daily")
async def run_daily(
    _: None = Depends(verify_scheduler_token),
    db: Session = Depends(get_db),
):
    """Generate and deliver today's summary for every connected user."""
    results = {"sent": [], "skipped": [], "failed": []}

    users = db.query(User).filter(User.connected == True).all()  # noqa: E712
    for user in users:
        target_day = _target_day(user)

        if user.last_summary_sent and user.last_summary_sent.date() >= target_day:
            results["skipped"].append(user.id)
            continue

        oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == user.id).first()
        if not oauth_token:
            results["failed"].append({"user_id": user.id, "reason": "no oauth token on file"})
            continue

        try:
            access_token = await _valid_access_token(oauth_token, db)
        except HTTPException as e:
            results["failed"].append({"user_id": user.id, "reason": str(e.detail)})
            continue

        day_data = await google_health_client.fetch_day(access_token, target_day)

        try:
            summary_text = await gemini.generate_daily_summary(day_data)
        except Exception as e:
            logger.error(f"🚨 Summary generation failed for {user.id}: {e}")
            results["failed"].append({"user_id": user.id, "reason": f"summary generation failed: {e}"})
            continue

        # Store before sending: a Telegram delivery failure below must not
        # cost us the summary row, or the next day's 7-day trend degrades too.
        summary_row = HealthSummary(
            user_id=user.id,
            raw_fitbit_data=day_data,
            summary=summary_text,
            summary_date=datetime.combine(target_day, datetime.min.time()),
        )
        db.add(summary_row)
        db.commit()

        try:
            await telegram_client.send_message(user.telegram_chat_id, summary_text)
        except TelegramError as e:
            logger.error(f"🚨 Telegram delivery failed for {user.id}: {e}")
            results["failed"].append({"user_id": user.id, "reason": f"telegram send failed: {e}"})
            continue

        user.last_summary_sent = datetime.utcnow()
        db.commit()
        results["sent"].append(user.id)

    return results
