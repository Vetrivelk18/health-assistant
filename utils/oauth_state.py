"""
Signed, self-contained OAuth state tokens.

The state parameter's job is CSRF protection: prove that the callback we're
handling belongs to a login *we* started, for the Telegram chat we think it
belongs to.

This used to be a module-level dict mapping state -> chat_id. That works on
one long-lived process and fails in exactly the environment this deploys to:

  - Cloud Run scales to zero, so a cold start between /auth/login and the
    user finishing the consent screen empties the dict.
  - With --concurrency=80 and up to 3 instances, the callback can land on a
    different instance than the login did, which never had the entry.

Both surface identically and confusingly, as "Invalid state parameter"
immediately after a *successful* Google consent screen.

The fix is to stop storing anything: put the chat id and an expiry in the
token itself and sign it with SECRET_KEY, so any instance can verify a
state it never issued. The signature is what makes it unforgeable; without
it a user could hand us any chat id and have tokens written against
someone else's account.

Deliberately not single-use. Enforcing that would need shared storage
again, which is the thing being removed — and replay is harmless here,
because the authorization code accompanying it is single-use at Google's
end. A replayed state with a spent code fails at token exchange.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

DEFAULT_TTL_SECONDS = 600  # 10 minutes: generous for a consent screen


class OAuthStateError(ValueError):
    """The state parameter was missing, malformed, forged, or expired."""


def _b64encode(raw: bytes) -> str:
    # URL-safe and unpadded: this travels in a query string.
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64encode(digest)


def issue(telegram_chat_id: str, secret: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a signed state token binding this flow to one Telegram chat."""
    if not secret:
        raise OAuthStateError("SECRET_KEY is not configured")

    payload = {
        "cid": telegram_chat_id,
        "exp": int(time.time()) + ttl_seconds,
        # Makes each token unique even for the same chat in the same second,
        # so a token can't be confused with a concurrent one.
        "n": secrets.token_urlsafe(8),
    }
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify(state: str, secret: str) -> str:
    """Return the Telegram chat id from a valid state token.

    Raises OAuthStateError if the token is malformed, has a bad signature,
    or has expired.
    """
    if not secret:
        raise OAuthStateError("SECRET_KEY is not configured")
    if not state or "." not in state:
        raise OAuthStateError("Malformed state")

    payload_b64, _, signature = state.partition(".")

    # compare_digest, not ==, so a forged signature can't be brute-forced a
    # byte at a time by timing the response.
    if not hmac.compare_digest(signature, _sign(payload_b64, secret)):
        raise OAuthStateError("Bad signature")

    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as e:
        raise OAuthStateError("Malformed payload") from e

    # Checked only after the signature passes — an expiry read out of an
    # unverified payload would be attacker-controlled.
    if int(payload.get("exp", 0)) < time.time():
        raise OAuthStateError("State expired")

    chat_id = payload.get("cid")
    if not chat_id:
        raise OAuthStateError("State missing chat id")
    return str(chat_id)
