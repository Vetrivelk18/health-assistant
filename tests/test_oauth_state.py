"""
Tests for signed OAuth state tokens (utils/oauth_state.py).

The point of these is that a state token issued by one process is verifiable
by another — the previous in-memory dict was not, which broke the callback
across Cloud Run cold starts and across instances.
"""

import time
from unittest.mock import patch

import pytest

from utils import oauth_state

SECRET = "test-secret-key"


def test_round_trip_recovers_the_chat_id():
    state = oauth_state.issue("12345", SECRET)
    assert oauth_state.verify(state, SECRET) == "12345"


def test_state_is_verifiable_without_any_shared_storage():
    """The whole reason for signing: the instance handling /auth/callback
    may never have seen the /auth/login that issued this token."""
    state = oauth_state.issue("12345", SECRET)

    # Nothing is carried over between these — a different process with the
    # same secret is all that's needed.
    import importlib
    fresh = importlib.reload(oauth_state)
    assert fresh.verify(state, SECRET) == "12345"


def test_tampered_chat_id_is_rejected():
    """Without a valid signature a caller could name someone else's chat and
    have OAuth tokens written against their account."""
    import base64
    import json

    forged_payload = base64.urlsafe_b64encode(
        json.dumps({"cid": "victim", "exp": int(time.time()) + 600, "n": "x"}).encode()
    ).decode().rstrip("=")

    legit = oauth_state.issue("attacker", SECRET)
    _, _, signature = legit.partition(".")
    forged = f"{forged_payload}.{signature}"

    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.verify(forged, SECRET)


def test_wrong_secret_is_rejected():
    state = oauth_state.issue("12345", SECRET)
    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.verify(state, "a-different-secret")


def test_expired_state_is_rejected():
    state = oauth_state.issue("12345", SECRET, ttl_seconds=1)
    with patch("utils.oauth_state.time.time", return_value=time.time() + 10):
        with pytest.raises(oauth_state.OAuthStateError, match="expired"):
            oauth_state.verify(state, SECRET)


@pytest.mark.parametrize("bad", ["", "garbage", "no-dot-here", "a.b", "."])
def test_malformed_states_are_rejected(bad):
    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.verify(bad, SECRET)


def test_each_token_is_unique():
    """Two logins from the same chat in the same second must not collide."""
    a = oauth_state.issue("12345", SECRET)
    b = oauth_state.issue("12345", SECRET)
    assert a != b


def test_missing_secret_refuses_to_issue_or_verify():
    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.issue("12345", "")
    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.verify("anything.here", "")
