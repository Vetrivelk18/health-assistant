"""
Tests for the internal daily-run endpoints (routes/internal.py).
Mocks OIDC verification, the Google Health fetch, Gemini, and Telegram send.

Two paths are covered: the inline fallback (no TASKS_QUEUE configured, which
is the default in tests) and the Cloud Tasks fan-out, plus the per-user
worker endpoint and its retry semantics.
"""

from datetime import date, datetime
from datetime import datetime as real_datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import routes.internal as internal_module
from app import app
from database import SessionLocal
from models import HealthSummary, OAuthToken, User
from services.gemini import GeminiError
from services.telegram_bot import TelegramError

client = TestClient(app)

TEST_CHAT_ID = "pytest_internal_chat_id"
VALID_CLAIMS = {"email": "scheduler@test.iam.gserviceaccount.com", "email_verified": True}


@pytest.fixture(autouse=True)
def scheduler_email():
    with patch.object(
        internal_module.settings, "SCHEDULER_SERVICE_ACCOUNT_EMAIL", VALID_CLAIMS["email"]
    ), patch.object(
        internal_module.settings, "TASKS_SERVICE_ACCOUNT_EMAIL", VALID_CLAIMS["email"]
    ):
        yield


@pytest.fixture(autouse=True)
def cleanup_test_user():
    yield
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == TEST_CHAT_ID).first()
        if user:
            db.query(HealthSummary).filter(HealthSummary.user_id == user.id).delete()
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
        timezone="UTC",
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


def _post():
    return client.post("/internal/run-daily", headers={"Authorization": "Bearer faketoken"})


# ---------------------------------------------------------------- auth ----

def test_missing_auth_header_rejected():
    response = client.post("/internal/run-daily")
    assert response.status_code == 403


def test_invalid_oidc_token_rejected():
    with patch("routes.internal.google_id_token.verify_oauth2_token", side_effect=ValueError("bad token")):
        response = _post()
    assert response.status_code == 403


def test_wrong_caller_rejected():
    with patch(
        "routes.internal.google_id_token.verify_oauth2_token",
        return_value={"email": "someone-else@evil.com", "email_verified": True},
    ):
        response = _post()
    assert response.status_code == 403


# ------------------------------------------------------------- the run ----

def test_no_connected_users_returns_empty_result():
    with patch("routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS):
        response = _post()
    assert response.status_code == 200
    assert response.json() == {
        "mode": "inline",
        "queued": [],
        "sent": [],
        "skipped": [],
        "failed": [],
    }


def test_user_without_oauth_token_marked_failed():
    db = SessionLocal()
    user = User(
        telegram_chat_id=TEST_CHAT_ID,
        google_user_id=f"google_{TEST_CHAT_ID}",
        email=f"telegram_{TEST_CHAT_ID}@local",
        connected=True,
    )
    db.add(user)
    db.commit()
    db.close()

    with patch("routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS):
        response = _post()

    assert response.status_code == 200
    assert len(response.json()["failed"]) == 1


def test_full_run_sends_and_stores_summary(connected_user):
    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), patch(
        "routes.internal.google_health_client.fetch_day",
        new=AsyncMock(return_value={"date": "2026-08-21", "metrics": {}, "errors": {}}),
    ), patch(
        "routes.internal.gemini.generate_daily_summary",
        new=AsyncMock(return_value="Great sleep last night!"),
    ), patch(
        "routes.internal.telegram_client.send_message", new=AsyncMock()
    ) as mock_send:
        response = _post()

    assert response.status_code == 200
    body = response.json()
    assert body["sent"] == [connected_user]
    mock_send.assert_awaited_once_with(TEST_CHAT_ID, "Great sleep last night!")

    db = SessionLocal()
    user = db.query(User).filter(User.id == connected_user).first()
    assert user.last_summary_sent is not None
    summary = db.query(HealthSummary).filter(HealthSummary.user_id == connected_user).first()
    assert summary is not None
    assert summary.summary == "Great sleep last night!"
    db.close()


def test_telegram_failure_still_keeps_stored_summary(connected_user):
    """Bug fix: a Telegram delivery failure must not lose the generated summary."""
    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), patch(
        "routes.internal.google_health_client.fetch_day",
        new=AsyncMock(return_value={"date": "2026-08-21", "metrics": {}, "errors": {}}),
    ), patch(
        "routes.internal.gemini.generate_daily_summary",
        new=AsyncMock(return_value="Great sleep last night!"),
    ), patch(
        "routes.internal.telegram_client.send_message",
        new=AsyncMock(side_effect=TelegramError("boom")),
    ):
        response = _post()

    assert response.status_code == 200
    body = response.json()
    assert body["sent"] == []
    assert body["failed"] and body["failed"][0]["user_id"] == connected_user

    db = SessionLocal()
    user = db.query(User).filter(User.id == connected_user).first()
    assert user.last_summary_sent is None  # not stamped — delivery failed
    summary = db.query(HealthSummary).filter(HealthSummary.user_id == connected_user).first()
    assert summary is not None  # but the summary row survives
    assert summary.summary == "Great sleep last night!"
    db.close()


def test_already_sent_today_is_skipped(connected_user):
    db = SessionLocal()
    user = db.query(User).filter(User.id == connected_user).first()
    user.last_summary_sent = datetime.utcnow()
    db.commit()
    db.close()

    with patch("routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS):
        response = _post()

    assert response.status_code == 200
    assert response.json()["skipped"] == [connected_user]


# ----------------------------------------------------- Cloud Tasks fan-out ----

def _health_and_gemini_patches():
    """The three outbound calls the per-user path makes, all mocked."""
    return (
        patch(
            "routes.internal.google_health_client.fetch_day",
            new=AsyncMock(return_value={"date": "2026-08-21", "metrics": {}, "errors": {}}),
        ),
        patch(
            "routes.internal.gemini.generate_daily_summary",
            new=AsyncMock(return_value="Great sleep last night!"),
        ),
        patch("routes.internal.telegram_client.send_message", new=AsyncMock()),
    )


def test_run_daily_enqueues_one_task_per_user_and_does_no_work(connected_user):
    """The dispatcher must fan out, not summarise. If it ever calls Gemini or
    Telegram itself the whole point of the queue is lost."""
    fetch, summarise, send = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), patch(
        "routes.internal.tasks.is_configured", return_value=True
    ), patch(
        "routes.internal.tasks.enqueue", new=AsyncMock(return_value="task/name")
    ) as mock_enqueue, fetch as mock_fetch, summarise as mock_summarise, send as mock_send:
        response = _post()

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "queued"
    assert body["queued"] == [connected_user]
    assert body["sent"] == []

    mock_enqueue.assert_awaited_once()
    kwargs = mock_enqueue.await_args.kwargs
    args = mock_enqueue.await_args.args
    assert args[0] == "/internal/run-single-user"
    assert args[1]["user_id"] == connected_user
    assert "target_day" in args[1]
    # Deterministic name is what dedupes a retried scheduler attempt.
    assert kwargs["task_id"].startswith(f"daily-{connected_user}-")

    mock_fetch.assert_not_awaited()
    mock_summarise.assert_not_awaited()
    mock_send.assert_not_awaited()


def test_run_daily_reports_enqueue_failure_without_sinking_the_batch(connected_user):
    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), patch(
        "routes.internal.tasks.is_configured", return_value=True
    ), patch(
        "routes.internal.tasks.enqueue", new=AsyncMock(side_effect=RuntimeError("queue down"))
    ):
        response = _post()

    assert response.status_code == 200
    body = response.json()
    assert body["queued"] == []
    assert body["failed"][0]["user_id"] == connected_user
    assert "queue down" in body["failed"][0]["reason"]


# ------------------------------------------------- the per-user worker ----

def _post_single(user_id: str, target_day: str = "2026-08-21"):
    return client.post(
        "/internal/run-single-user",
        json={"user_id": user_id, "target_day": target_day},
        headers={"Authorization": "Bearer faketoken"},
    )


def test_run_single_user_rejects_unauthenticated_caller(connected_user):
    response = client.post(
        "/internal/run-single-user",
        json={"user_id": connected_user, "target_day": "2026-08-21"},
    )
    assert response.status_code == 403


def test_run_single_user_sends_and_stores(connected_user):
    fetch, summarise, send = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, summarise, send as mock_send:
        response = _post_single(connected_user)

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    mock_send.assert_awaited_once_with(TEST_CHAT_ID, "Great sleep last night!")

    db = SessionLocal()
    summary = db.query(HealthSummary).filter(HealthSummary.user_id == connected_user).first()
    assert summary is not None
    db.close()


def test_run_single_user_returns_200_for_permanent_failure():
    """No OAuth token on file can never succeed on retry — must NOT return a
    non-2xx, or Cloud Tasks burns all five attempts on it."""
    db = SessionLocal()
    user = User(
        telegram_chat_id=TEST_CHAT_ID,
        google_user_id=f"google_{TEST_CHAT_ID}",
        email=f"telegram_{TEST_CHAT_ID}@local",
        connected=True,
    )
    db.add(user)
    db.commit()
    user_id = user.id
    db.close()

    with patch("routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS):
        response = _post_single(user_id)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_run_single_user_returns_503_for_transient_failure(connected_user):
    """A Telegram outage should earn a backed-off retry, i.e. a non-2xx."""
    fetch, summarise, _ = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, summarise, patch(
        "routes.internal.telegram_client.send_message",
        new=AsyncMock(side_effect=TelegramError("boom")),
    ):
        response = _post_single(connected_user)

    assert response.status_code == 503

    db = SessionLocal()
    # The summary row still survives the delivery failure.
    assert db.query(HealthSummary).filter(HealthSummary.user_id == connected_user).first()
    db.close()


def test_run_single_user_is_idempotent_across_retries(connected_user):
    """A retry after a successful send must not send twice, and must not
    accumulate a second summary row for the same day."""
    fetch, summarise, send = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, summarise, send as mock_send:
        first = _post_single(connected_user)
        second = _post_single(connected_user)

    assert first.json()["status"] == "sent"
    assert second.json()["status"] == "skipped"
    assert mock_send.await_count == 1

    db = SessionLocal()
    rows = db.query(HealthSummary).filter(HealthSummary.user_id == connected_user).count()
    assert rows == 1
    db.close()


def test_retry_after_telegram_failure_reuses_the_summary_row(connected_user):
    """The Telegram-failure path stores a row then 503s. The retry must
    update that row rather than inserting a duplicate."""
    fetch, summarise, _ = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, summarise, patch(
        "routes.internal.telegram_client.send_message",
        new=AsyncMock(side_effect=TelegramError("boom")),
    ):
        assert _post_single(connected_user).status_code == 503

    fetch2, summarise2, send2 = _health_and_gemini_patches()
    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch2, summarise2, send2:
        assert _post_single(connected_user).json()["status"] == "sent"

    db = SessionLocal()
    rows = db.query(HealthSummary).filter(HealthSummary.user_id == connected_user).count()
    assert rows == 1
    db.close()


def test_total_health_outage_retries_instead_of_sending_empty_summary(connected_user):
    """Every metric failing with a 503 is an outage, not a quiet day. Sending
    'you logged nothing yesterday' would tell the user something false about
    their own health, so this must retry instead."""
    outage = {
        "date": "2026-08-21",
        "metrics": {},
        "errors": {
            m: {"status": 503, "body": "unavailable", "transient": True}
            for m in ("sleep", "steps", "heart_rate", "active_minutes")
        },
    }

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), patch(
        "routes.internal.google_health_client.fetch_day", new=AsyncMock(return_value=outage)
    ), patch(
        "routes.internal.gemini.generate_daily_summary", new=AsyncMock()
    ) as mock_gemini, patch(
        "routes.internal.telegram_client.send_message", new=AsyncMock()
    ) as mock_send:
        response = _post_single(connected_user)

    assert response.status_code == 503
    mock_gemini.assert_not_awaited()  # don't even pay for the tokens
    mock_send.assert_not_awaited()


def test_partial_health_data_still_produces_a_summary(connected_user):
    """One dead metric is the documented degradation path — summarise what
    did arrive rather than failing the whole user."""
    partial = {
        "date": "2026-08-21",
        "metrics": {"sleep": {"dataPoints": []}, "steps": {"dataPoints": []}},
        "errors": {"heart_rate": {"status": 503, "body": "x", "transient": True}},
    }

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), patch(
        "routes.internal.google_health_client.fetch_day", new=AsyncMock(return_value=partial)
    ), patch(
        "routes.internal.gemini.generate_daily_summary",
        new=AsyncMock(return_value="Decent sleep, no heart data today."),
    ), patch(
        "routes.internal.telegram_client.send_message", new=AsyncMock()
    ) as mock_send:
        response = _post_single(connected_user)

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    mock_send.assert_awaited_once()


def test_permanent_gemini_failure_is_not_retried(connected_user):
    """A malformed-request error will fail identically on every retry."""
    fetch, _, send = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, patch(
        "routes.internal.gemini.generate_daily_summary",
        new=AsyncMock(side_effect=GeminiError("bad request", is_transient=False)),
    ), send:
        response = _post_single(connected_user)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_transient_gemini_failure_is_retried(connected_user):
    fetch, _, send = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, patch(
        "routes.internal.gemini.generate_daily_summary",
        new=AsyncMock(side_effect=GeminiError("503 overloaded", is_transient=True)),
    ), send:
        response = _post_single(connected_user)

    assert response.status_code == 503


def test_blocked_bot_is_not_retried(connected_user):
    """A 403 from Telegram means the user blocked the bot — retrying that
    every day forever is pure waste."""
    fetch, summarise, _ = _health_and_gemini_patches()

    with patch(
        "routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS
    ), fetch, summarise, patch(
        "routes.internal.telegram_client.send_message",
        new=AsyncMock(side_effect=TelegramError("bot was blocked by the user", 403)),
    ):
        response = _post_single(connected_user)

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_run_single_user_skips_disconnected_user(connected_user):
    db = SessionLocal()
    user = db.query(User).filter(User.id == connected_user).first()
    user.connected = False
    db.commit()
    db.close()

    with patch("routes.internal.google_id_token.verify_oauth2_token", return_value=VALID_CLAIMS):
        response = _post_single(connected_user)

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"


# --------------------------------------------------------- timezone fix ----

def _frozen_now(hour_utc: int, minute_utc: int = 0, day: int = 22):
    return real_datetime(2026, 8, day, hour_utc, minute_utc, tzinfo=dt_timezone.utc)


def test_target_day_crosses_midnight_for_ist_user():
    """20:00 UTC is already 01:30 IST the next day — 'yesterday' must
    account for that, not just subtract a day from the UTC date."""
    fixed_now = _frozen_now(20, 0)
    user = User(timezone="Asia/Kolkata")
    with patch("routes.internal.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: fixed_now.astimezone(tz) if tz else fixed_now
        assert internal_module._target_day(user) == date(2026, 8, 22)


def test_target_day_before_midnight_crossover_for_ist_user():
    fixed_now = _frozen_now(10, 0)
    user = User(timezone="Asia/Kolkata")
    with patch("routes.internal.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: fixed_now.astimezone(tz) if tz else fixed_now
        assert internal_module._target_day(user) == date(2026, 8, 21)


def test_target_day_invalid_timezone_falls_back_to_utc():
    fixed_now = _frozen_now(10, 0)
    user = User(timezone="Not/A_Real_Zone")
    with patch("routes.internal.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: fixed_now.astimezone(tz) if tz else fixed_now
        assert internal_module._target_day(user) == date(2026, 8, 21)
