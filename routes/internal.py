"""
Internal endpoints — not reachable by end users.

The Cloud Run service is deployed --allow-unauthenticated (Telegram has to
reach POST /webhook/telegram), so the endpoints here authenticate their own
callers: Cloud Scheduler and Cloud Tasks both attach a Google-signed OIDC
token, which we verify (audience + the exact service-account email) before
doing anything.

Fan-out, rather than one long request:

    Cloud Scheduler --07:00--> POST /internal/run-daily
                                   |  enqueues one Cloud Task per user,
                                   |  responds in well under a second
                                   v
                              Cloud Tasks queue (dispatch capped at ~5/sec)
                                   |
                                   v  one short request per user
                              POST /internal/run-single-user

Running the whole batch inside the Scheduler request meant a single instance
held CPU and RAM for its entire duration, and one slow user could push the
run past the attempt deadline. One request per user at a fixed dispatch rate
keeps every request short, keeps peak memory flat regardless of user count,
and lets Cloud Run scale back to zero in between. Cloud Tasks' first 1M
operations/month are free, so the fan-out adds no cost at this scale.

Cloud Run still freezes CPU the instant a response is sent, so each
/run-single-user request does its work synchronously — the queue is what
provides the "later", never a background task.

Retry semantics matter here, because Cloud Tasks retries any non-2xx
response. A permanent failure (no token on file, dead refresh token) returns
200 with a failure body, since retrying it would never help; a transient one
(Gemini, Telegram, the Health API) returns 503 to earn a backed-off retry.

Without a queue configured (local dev, tests) /internal/run-daily falls back
to running the batch inline, so the endpoint still works end to end.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import HealthSummary, OAuthToken, User
from routes.mcp_tools import _valid_access_token, google_health_client
from routes.telegram import telegram_client
from services import gemini, tasks
from services.gemini import GeminiError
from services.google_health import GoogleHealthError, is_total_outage
from services.telegram_bot import TelegramError
from utils.logging_config import log_event

logger = logging.getLogger(__name__)

router = APIRouter()

_google_auth_request = google_requests.Request()

RUN_SINGLE_USER_PATH = "/internal/run-single-user"


class RunSingleUserRequest(BaseModel):
    user_id: str
    # Passed down from the dispatcher rather than recomputed here: a task can
    # be dispatched (or retried) after midnight in the user's own timezone,
    # and recomputing would then silently summarise a different day than the
    # one the run was for.
    target_day: date


def _verify_oidc(authorization: str | None, expected_email: str) -> None:
    """Reject anything that isn't a valid Google-signed OIDC token minted for
    us by the expected service account."""
    # Fail closed. If the expected email isn't configured there is nothing
    # meaningful to compare against, and a token carrying no email claim
    # would otherwise match None == None and sail through.
    if not expected_email:
        logger.error("🚨 internal endpoint has no expected service account configured — refusing")
        raise HTTPException(status_code=403, detail="Endpoint not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Missing bearer token")

    token = authorization[len("Bearer "):]
    try:
        claims = google_id_token.verify_oauth2_token(
            token, _google_auth_request, audience=settings.RUN_DAILY_AUDIENCE
        )
    except ValueError as e:
        logger.warning(f"🚨 internal endpoint rejected an invalid OIDC token: {e}")
        raise HTTPException(status_code=403, detail="Invalid token")

    caller_email = claims.get("email")
    if not claims.get("email_verified") or caller_email != expected_email:
        logger.warning(f"🚨 internal endpoint rejected unexpected caller: {caller_email}")
        raise HTTPException(status_code=403, detail="Unexpected caller")


def verify_scheduler_token(authorization: str = Header(default=None)) -> None:
    """Caller must be the Cloud Scheduler service account."""
    _verify_oidc(authorization, settings.SCHEDULER_SERVICE_ACCOUNT_EMAIL)


def verify_task_token(authorization: str = Header(default=None)) -> None:
    """Caller must be the service account Cloud Tasks mints tokens as.

    Defaults to the scheduler's SA (see config.py), so this is the same check
    unless the two are deliberately split.
    """
    _verify_oidc(authorization, settings.TASKS_SERVICE_ACCOUNT_EMAIL)


def _target_day(user: User) -> date:
    """'Yesterday' in the user's own timezone, not the container's (UTC)."""
    try:
        tz = ZoneInfo(user.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return (datetime.now(tz) - timedelta(days=1)).date()


def _already_summarised(user: User, target_day: date) -> bool:
    return bool(user.last_summary_sent and user.last_summary_sent.date() >= target_day)


# While the OAuth consent screen is in Testing, Google expires refresh
# tokens 7 days after they're issued. Warn a day early so a reconnect can
# happen before summaries actually stop.
REFRESH_TOKEN_LIFETIME_DAYS = 7
WARN_BEFORE_EXPIRY_DAYS = 1
RENAG_AFTER_DAYS = 3

RECONNECT_EXPIRED_MESSAGE = (
    "⚠️ Your Google Health connection has expired, so I couldn't put together "
    "your summary this morning.\n\n"
    "Tap /connect to reconnect — it takes a few seconds and you'll be back to "
    "daily summaries tomorrow."
)

RECONNECT_SOON_MESSAGE = (
    "🔔 Heads up — your Google Health connection expires tomorrow.\n\n"
    "Tap /connect now to reconnect and your summaries won't miss a day.\n"
    "(Google expires these weekly while the app is unverified.)"
)


async def _notify_once(oauth_token: OAuthToken, user: User, message: str, db: Session,
                       *, event: str) -> None:
    """Send a connection notice, at most once every RENAG_AFTER_DAYS.

    Without the throttle a user whose consent lapsed would be messaged every
    single morning — which reads as spam and trains them to ignore exactly
    the message they need to act on.
    """
    last = oauth_token.reconnect_notified_at
    if last and (datetime.utcnow() - last) < timedelta(days=RENAG_AFTER_DAYS):
        return

    try:
        await telegram_client.send_message(user.telegram_chat_id, message)
    except TelegramError as e:
        # Best-effort: failing to deliver a nag must not change the outcome
        # of the run that triggered it.
        log_event(logger, logging.WARNING, f"Could not notify {user.id} to reconnect",
                  event="reconnect_notify_failed", user_id=user.id, error_code=e.error_code)
        return

    oauth_token.reconnect_notified_at = datetime.utcnow()
    db.commit()
    log_event(logger, logging.INFO, f"Told {user.id} to reconnect",
              event=event, user_id=user.id)


def _refresh_token_expires_on(oauth_token: OAuthToken) -> date | None:
    """Best estimate of when this refresh token stops working.

    Google exposes no expiry for refresh tokens, so this is derived from
    when the last full consent happened. Returns None for tokens issued
    before that column existed — better to stay quiet than to warn on a
    guess.
    """
    issued = oauth_token.refresh_token_issued_at
    if not issued:
        return None
    return (issued + timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS)).date()


# ----------------------------------------------------------- dispatcher ----

@router.post("/run-daily")
async def run_daily(
    _: None = Depends(verify_scheduler_token),
    db: Session = Depends(get_db),
):
    """
    Fan out one task per connected user and return immediately.

    Deliberately does no per-user work beyond the cheap "already sent?" check
    — the whole point is that this responds in well under a second so the
    Scheduler attempt can never time out, however many users exist.
    """
    queued_mode = tasks.is_configured()
    results: dict = {
        "mode": "queued" if queued_mode else "inline",
        "queued": [],
        "sent": [],
        "skipped": [],
        "failed": [],
    }

    if not queued_mode:
        logger.warning(
            "TASKS_QUEUE not configured — running the daily batch inline. "
            "Fine for local dev; in production this should fan out."
        )

    users = db.query(User).filter(User.connected == True).all()  # noqa: E712
    for user in users:
        target_day = _target_day(user)

        if _already_summarised(user, target_day):
            results["skipped"].append(user.id)
            continue

        if not queued_mode:
            outcome = await _summarise_user(user, target_day, db)
            results[outcome["bucket"]].append(outcome["detail"])
            continue

        try:
            await tasks.enqueue(
                RUN_SINGLE_USER_PATH,
                {"user_id": user.id, "target_day": target_day.isoformat()},
                task_id=tasks.daily_task_id(user.id, target_day),
            )
        except Exception as e:
            # One user's enqueue failing must not abandon the rest of the
            # batch — Cloud Scheduler's retry will pick this user back up,
            # and the deterministic task name stops the others doubling.
            log_event(logger, logging.ERROR, f"Failed to enqueue daily task for {user.id}",
                      exc_info=True, event="enqueue_failed", user_id=user.id,
                      error_type=type(e).__name__)
            results["failed"].append({"user_id": user.id, "reason": f"enqueue failed: {e}"})
            continue

        results["queued"].append(user.id)

    return results


# --------------------------------------------------------------- worker ----

@router.post("/run-single-user")
async def run_single_user(
    body: RunSingleUserRequest,
    _: None = Depends(verify_task_token),
    db: Session = Depends(get_db),
):
    """
    Generate and deliver one user's summary. Called by Cloud Tasks, one
    request per user.

    Returns 200 for anything a retry can't fix, and 503 for anything it can.
    """
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user or not user.connected:
        # Disconnected between dispatch and delivery — nothing to retry.
        return {"status": "skipped", "user_id": body.user_id, "reason": "user not connected"}

    # Authoritative idempotency guard. The dispatcher checks this too, but a
    # task can be retried after a successful send whose response was lost.
    if _already_summarised(user, body.target_day):
        return {"status": "skipped", "user_id": user.id, "reason": "already summarised"}

    outcome = await _summarise_user(user, body.target_day, db)

    if outcome["bucket"] == "sent":
        return {"status": "sent", "user_id": user.id}
    if outcome["bucket"] == "skipped":
        return {"status": "skipped", "user_id": user.id}

    reason = outcome["detail"]["reason"]
    if outcome["retryable"]:
        # Non-2xx is what tells Cloud Tasks to retry with backoff.
        raise HTTPException(status_code=503, detail=reason)
    return {"status": "failed", "user_id": user.id, "reason": reason}


# ------------------------------------------------------- shared per-user ----

async def _summarise_user(user: User, target_day: date, db: Session) -> dict:
    """
    The actual work for one user: refresh token → fetch → summarise → store
    → send.

    Returns a dict describing the outcome rather than raising, so both the
    inline path and the task path can decide for themselves what to do with
    it (a bucket in a response body vs. an HTTP status that drives retries).
    `retryable` distinguishes "this might work next time" from "this never
    will".
    """

    def failed(reason: str, retryable: bool) -> dict:
        return {
            "bucket": "failed",
            "detail": {"user_id": user.id, "reason": reason},
            "retryable": retryable,
        }

    oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == user.id).first()
    if not oauth_token:
        return failed("no oauth token on file", retryable=False)

    try:
        access_token = await _valid_access_token(oauth_token, db)
    except HTTPException as e:
        # A refresh failure is the user's consent being gone (Testing-mode
        # refresh tokens expire after 7 days) — retrying won't recover it.
        #
        # Tell them. Before this, the daily run logged the failure and moved
        # on, so the user's summaries just stopped with no explanation and
        # no indication that /connect would fix it. Since the consent screen
        # is staying in Testing, this isn't an edge case — it's what happens
        # every seven days.
        log_event(logger, logging.WARNING, f"Auth expired for {user.id}",
                  event="auth_expired", user_id=user.id, reason=str(e.detail))
        await _notify_once(oauth_token, user, RECONNECT_EXPIRED_MESSAGE, db,
                           event="reconnect_prompted")
        return failed(str(e.detail), retryable=False)

    # fetch_day never raises for a single failed metric — it records each one
    # under "errors" and returns what it did get, so one dead data type still
    # yields a shorter summary rather than no summary. Only a failure of the
    # call itself reaches here.
    try:
        day_data = await google_health_client.fetch_day(access_token, target_day)
    except GoogleHealthError as e:
        log_event(logger, logging.ERROR, f"Health fetch failed for {user.id}",
                  event="health_fetch_failed", user_id=user.id,
                  status=e.status, transient=e.is_transient)
        return failed(f"health fetch failed: {e}", retryable=e.is_transient)

    # A watch left on the charger and a Health API outage look identical once
    # per-metric errors are swallowed — both produce a day with no metrics.
    # Only the first should become a summary; telling a user "you logged
    # nothing yesterday" because Google was down is worse than being late.
    if is_total_outage(day_data):
        # report=True: every metric failing means the Health API (or our
        # access to it) is broken for everyone, not just this user — worth
        # surfacing in Error Reporting. Per-user failures deliberately are
        # not, or an expired consent would page someone every morning.
        log_event(logger, logging.ERROR, f"All health metrics failed for {user.id}",
                  event="health_total_outage", user_id=user.id, report=True,
                  errors={k: v.get("status") for k, v in day_data["errors"].items()})
        return failed("all health data types failed transiently", retryable=True)

    if day_data["errors"]:
        log_event(logger, logging.WARNING, f"Partial health data for {user.id}",
                  event="health_partial", user_id=user.id,
                  missing=sorted(day_data["errors"]))

    try:
        summary_text = await gemini.generate_daily_summary(day_data)
    except GeminiError as e:
        log_event(logger, logging.ERROR, f"Summary generation failed for {user.id}",
                  event="summary_failed", user_id=user.id, transient=e.is_transient)
        return failed(f"summary generation failed: {e}", retryable=e.is_transient)

    # Store before sending: a Telegram delivery failure below must not cost
    # us the summary row, or the next day's 7-day trend degrades too.
    #
    # Upsert rather than insert. A retried task has already stored a row for
    # this day (that's exactly the path a Telegram failure takes), and
    # blind inserts would pile up a duplicate per retry.
    summary_date = datetime.combine(target_day, datetime.min.time())
    summary_row = (
        db.query(HealthSummary)
        .filter(HealthSummary.user_id == user.id, HealthSummary.summary_date == summary_date)
        .first()
    )
    if summary_row:
        summary_row.raw_fitbit_data = day_data
        summary_row.summary = summary_text
    else:
        db.add(HealthSummary(
            user_id=user.id,
            raw_fitbit_data=day_data,
            summary=summary_text,
            summary_date=summary_date,
        ))
    db.commit()

    try:
        await telegram_client.send_message(user.telegram_chat_id, summary_text)
    except TelegramError as e:
        # 403 means the user blocked the bot — that never recovers on retry.
        blocked = e.error_code in (401, 403)
        log_event(logger, logging.ERROR, f"Telegram delivery failed for {user.id}",
                  event="telegram_send_failed", user_id=user.id,
                  error_code=e.error_code, transient=not blocked)
        return failed(f"telegram send failed: {e}", retryable=not blocked)

    user.last_summary_sent = datetime.utcnow()
    db.commit()
    log_event(logger, logging.INFO, f"Daily summary sent to {user.id}",
              event="summary_sent", user_id=user.id, day=target_day.isoformat(),
              partial=bool(day_data["errors"]))

    # Sent after the summary, not before: a warning that arrives ahead of
    # the thing it's warning about reads as an error. Sending it on a day
    # that otherwise worked is the point — the user reconnects before
    # anything breaks rather than after.
    expires_on = _refresh_token_expires_on(oauth_token)
    if expires_on and (expires_on - date.today()).days <= WARN_BEFORE_EXPIRY_DAYS:
        await _notify_once(oauth_token, user, RECONNECT_SOON_MESSAGE, db,
                           event="reconnect_warned")

    return {"bucket": "sent", "detail": user.id, "retryable": False}
