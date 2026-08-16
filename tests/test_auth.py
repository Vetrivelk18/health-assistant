"""
Tests for Phase 2 OAuth 2.0 flow (routes/auth.py)
Uses the configured DATABASE_URL directly; mocks calls to Google's token endpoint.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from database import SessionLocal
from models import User, OAuthToken

client = TestClient(app)

TEST_CHAT_ID = "pytest_chat_id"


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


def start_oauth_flow():
    response = client.get("/auth/login", params={"telegram_chat_id": TEST_CHAT_ID})
    assert response.status_code == 200
    body = response.json()
    assert "auth_url" in body
    state = body["auth_url"].split("state=")[1].split("&")[0]
    return state


def test_login_returns_auth_url():
    response = client.get("/auth/login", params={"telegram_chat_id": TEST_CHAT_ID})
    assert response.status_code == 200
    assert response.json()["auth_url"].startswith("https://accounts.google.com/")


def test_callback_invalid_state_returns_400():
    response = client.get("/auth/callback", params={"code": "x", "state": "not-a-real-state"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid state parameter"


def test_callback_creates_user_and_token():
    state = start_oauth_flow()

    with patch(
        "routes.auth.google_health_client.exchange_code_for_tokens",
        new=AsyncMock(return_value={
            "access_token": "fake_access_token",
            "refresh_token": "fake_refresh_token",
            "expires_in": 3600,
        }),
    ):
        response = client.get("/auth/callback", params={"code": "fakecode", "state": state})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["connected"] is True
    assert body["telegram_chat_id"] == TEST_CHAT_ID


def test_status_unknown_user_not_authenticated():
    response = client.get("/auth/status/does-not-exist")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "message": "No OAuth token found"}


def test_disconnect_unknown_user_returns_404():
    response = client.get("/auth/disconnect/does-not-exist")
    assert response.status_code == 404


def test_refresh_token_unknown_user_returns_404():
    response = client.post("/auth/refresh-token", params={"user_id": "does-not-exist"})
    assert response.status_code == 404


def test_full_oauth_lifecycle():
    state = start_oauth_flow()

    with patch(
        "routes.auth.google_health_client.exchange_code_for_tokens",
        new=AsyncMock(return_value={
            "access_token": "fake_access_token",
            "refresh_token": "fake_refresh_token",
            "expires_in": 3600,
        }),
    ):
        callback_response = client.get("/auth/callback", params={"code": "fakecode", "state": state})
    user_id = callback_response.json()["user_id"]

    status_response = client.get(f"/auth/status/{user_id}")
    assert status_response.json()["authenticated"] is True

    with patch(
        "routes.auth.google_health_client.refresh_access_token",
        new=AsyncMock(return_value={"access_token": "new_fake_token", "expires_in": 3600}),
    ):
        refresh_response = client.post("/auth/refresh-token", params={"user_id": user_id})
    assert refresh_response.status_code == 200
    assert refresh_response.json()["access_token"] == "new_fake_token"

    disconnect_response = client.get(f"/auth/disconnect/{user_id}")
    assert disconnect_response.status_code == 200

    final_status = client.get(f"/auth/status/{user_id}")
    assert final_status.json()["authenticated"] is False
