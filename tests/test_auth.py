"""
Tests for Phase 2 OAuth 2.0 flow (routes/auth.py)
Uses the configured DATABASE_URL directly; mocks calls to Google's token endpoint.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from database import SessionLocal
from models import User, OAuthToken
from services.google_health import TokenBundle

client = TestClient(app)

TEST_CHAT_ID = "pytest_chat_id"


def _fake_bundle(access_token="fake_access_token", refresh_token="fake_refresh_token"):
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scope="",
    )


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
    assert response.json()["detail"].startswith("Invalid state parameter")


def test_callback_creates_user_and_token():
    state = start_oauth_flow()

    with patch(
        "routes.auth.google_health_client.exchange_code",
        new=AsyncMock(return_value=_fake_bundle()),
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
        "routes.auth.google_health_client.exchange_code",
        new=AsyncMock(return_value=_fake_bundle()),
    ):
        callback_response = client.get("/auth/callback", params={"code": "fakecode", "state": state})
    user_id = callback_response.json()["user_id"]

    status_response = client.get(f"/auth/status/{user_id}")
    assert status_response.json()["authenticated"] is True

    with patch(
        "routes.auth.google_health_client.refresh",
        new=AsyncMock(return_value=_fake_bundle(access_token="new_fake_token")),
    ):
        refresh_response = client.post("/auth/refresh-token", params={"user_id": user_id})
    assert refresh_response.status_code == 200
    assert refresh_response.json()["access_token"] == "new_fake_token"

    disconnect_response = client.get(f"/auth/disconnect/{user_id}")
    assert disconnect_response.status_code == 200

    final_status = client.get(f"/auth/status/{user_id}")
    assert final_status.json()["authenticated"] is False


# ------------------------------------------------------ real identity ----

def _id_token(sub: str, email: str) -> str:
    """A JWT-shaped id_token. Only the payload segment is read."""
    import base64
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg({'sub': sub, 'email': email})}.signature"


def test_id_token_claims_are_decoded():
    from services.google_health import decode_id_token_claims

    claims = decode_id_token_claims(_id_token("109876543210", "real@gmail.com"))
    assert claims["sub"] == "109876543210"
    assert claims["email"] == "real@gmail.com"


def test_malformed_id_token_does_not_raise():
    """Identity is useful metadata, not worth failing a good connection."""
    from services.google_health import decode_id_token_claims

    assert decode_id_token_claims("not-a-jwt") == {}
    assert decode_id_token_claims("") == {}


def test_bundle_extracts_identity_from_token_response():
    from services.google_health import GoogleHealthClient

    bundle = GoogleHealthClient._bundle({
        "access_token": "at",
        "refresh_token": "rt",
        "expires_in": 3600,
        "id_token": _id_token("109876543210", "real@gmail.com"),
    })
    assert bundle.google_user_id == "109876543210"
    assert bundle.email == "real@gmail.com"


def test_bundle_without_id_token_has_no_identity():
    """A refresh response carries no id_token — that's expected, not an error."""
    from services.google_health import GoogleHealthClient

    bundle = GoogleHealthClient._bundle({
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
    })
    assert bundle.google_user_id is None
    assert bundle.email is None


def test_openid_and_email_scopes_are_requested():
    """Without these the token response carries no id_token at all."""
    from services.google_health import SCOPES

    assert "openid" in SCOPES
    assert "email" in SCOPES


def test_identity_scopes_survive_an_env_override():
    """A pre-existing .env sets GOOGLE_HEALTH_SCOPES and would otherwise win,
    silently leaving out openid/email and giving us no id_token."""
    from config import settings

    assert "openid" in settings.GOOGLE_HEALTH_SCOPES
    assert "email" in settings.GOOGLE_HEALTH_SCOPES
