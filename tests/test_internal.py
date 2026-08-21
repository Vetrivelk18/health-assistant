"""
Tests for the internal daily-run endpoint (routes/internal.py).
Mocks OIDC verification, the Google Health fetch, Gemini, and Telegram send.
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
from services.telegram_bot import TelegramError

client = TestClient(app)

TEST_CHAT_ID = "pytest_internal_chat_id"
VALID_CLAIMS = {"email": "scheduler@test.iam.gserviceaccount.com", "email_verified": True}


@pytest.fixture(autouse=True)
def scheduler_email():
    with patch.object(internal_module.settings, "SCHEDULER_SERVICE_ACCOUNT_EMAIL", VALID_CLAIMS["email"]):
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
    assert response.json() == {"sent": [], "skipped": [], "failed": []}


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
