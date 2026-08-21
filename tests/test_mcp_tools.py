"""
Tests for the MCP tool endpoints (routes/mcp_tools.py).
Mocks calls to Gemini and to Google's token endpoint.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import app
from database import SessionLocal
from models import OAuthToken, User
from services.google_health import TokenBundle

client = TestClient(app)

TEST_CHAT_ID = "pytest_mcp_chat_id"


@pytest.fixture
def connected_user():
    db = SessionLocal()
    user = User(
        telegram_chat_id=TEST_CHAT_ID,
        google_user_id=f"google_{TEST_CHAT_ID}",
        email=f"telegram_{TEST_CHAT_ID}@local",
        connected=True,
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
    yield user_id

    db.query(OAuthToken).filter(OAuthToken.user_id == user_id).delete()
    db.query(User).filter(User.id == user_id).delete()
    db.commit()
    db.close()


def test_list_tools_returns_schema():
    response = client.get("/mcp/tools")
    assert response.status_code == 200
    tools = response.json()
    assert tools[0]["name"] == "get_health_metric"
    assert "sleep" in tools[0]["input_schema"]["properties"]["metric"]["enum"]


def test_query_unknown_user_returns_404():
    response = client.post("/mcp/query", json={"user_id": "does-not-exist", "message": "how did I sleep?"})
    assert response.status_code == 404


def test_query_connected_user_returns_gemini_answer(connected_user):
    with patch(
        "routes.mcp_tools.gemini.answer_health_query",
        new=AsyncMock(return_value="You slept 7 hours last night."),
    ):
        response = client.post(
            "/mcp/query", json={"user_id": connected_user, "message": "how did I sleep?"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "You slept 7 hours last night."


def test_query_refreshes_expiring_token(connected_user):
    db = SessionLocal()
    oauth_token = db.query(OAuthToken).filter(OAuthToken.user_id == connected_user).first()
    oauth_token.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()

    fresh_bundle = TokenBundle(
        access_token="refreshed_access_token",
        refresh_token="fake_refresh_token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scope="",
    )

    with patch(
        "routes.mcp_tools.google_health_client.refresh",
        new=AsyncMock(return_value=fresh_bundle),
    ), patch(
        "routes.mcp_tools.gemini.answer_health_query",
        new=AsyncMock(return_value="ok"),
    ) as mock_answer:
        response = client.post(
            "/mcp/query", json={"user_id": connected_user, "message": "how did I sleep?"}
        )

    assert response.status_code == 200
    mock_answer.assert_awaited_once()
    assert mock_answer.await_args.args[1] == "refreshed_access_token"
