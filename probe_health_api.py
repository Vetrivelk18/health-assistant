#!/usr/bin/env python3
"""
probe_health_api.py — answer the one question that decides this project.

Does the Google Health API return real sleep / steps / heart-rate data for
your account, and what shape is it?

Standalone: no FastAPI, no database, no Telegram. It runs the OAuth flow in
your browser, catches the callback locally, then hits every data type —
trying each candidate filter grammar — and writes whatever comes back
to ./fixtures/.

    pip install httpx python-dotenv
    python3 probe_health_api.py

Requires in .env (or the environment):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REDIRECT_URI   default http://127.0.0.1:8765/auth/callback

The exact same redirect URI must be registered on the OAuth client.

Note on 127.0.0.1 vs localhost: on macOS `localhost` frequently resolves to
the IPv6 address ::1 while a default HTTPServer binds IPv4 only, which
surfaces as ERR_CONNECTION_REFUSED after an otherwise successful consent.
Using the literal 127.0.0.1 removes the ambiguity. The server below also
listens dual-stack when it can, so either form works.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from services.google_health import (  # noqa: E402
    DATA_TYPES, FILTER_TEMPLATE_BY_TYPE, GoogleHealthClient, GoogleHealthError,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN_CACHE = Path(__file__).parent / ".probe_tokens.json"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/auth/callback"

C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m",
     "b": "\033[1m", "d": "\033[2m", "x": "\033[0m"}


def say(msg: str, colour: str = "x") -> None:
    print(f"{C[colour]}{msg}{C['x']}", flush=True)


# --------------------------------------------------------------------------
# Local callback server
# --------------------------------------------------------------------------

class _Catcher(BaseHTTPRequestHandler):
    """Keeps listening until a request actually carries `code` or `error`.

    Browsers request /favicon.ico and sometimes probe the origin first; a
    handler that stops after the first request would consume one of those and
    then be gone when the real callback arrives.
    """

    result: dict = {}

    def do_GET(self):  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        params = {k: v[0] for k, v in query.items()}

        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        _Catcher.result = params
        body = (
            "<h2>Authorised.</h2><p>Close this tab and return to the terminal.</p>"
            if "code" in params else
            f"<h2>Authorisation failed</h2><pre>{params}</pre>"
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body) + 62))
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:system-ui;padding:3rem'>" + body + b"</body></html>"
        )

    def log_message(self, *_):  # silence the default access log
        pass


class _DualStackServer(HTTPServer):
    """Listen on IPv6 with v4-mapped addresses so ::1 and 127.0.0.1 both work."""

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def _make_server(port: int) -> HTTPServer:
    try:
        return _DualStackServer(("::", port), _Catcher)
    except OSError:
        return HTTPServer(("0.0.0.0", port), _Catcher)


def run_oauth(client: GoogleHealthClient, port: int, timeout: int = 300) -> str:
    """Blocking, synchronous. Never call asyncio from here."""
    _Catcher.result = {}
    state = secrets.token_urlsafe(24)
    url = client.authorization_url(state)

    try:
        server = _make_server(port)
    except OSError as e:
        say(f"\nCannot bind port {port}: {e}", "r")
        say("Something else is using it. Pick a free port, update .env, and add", "r")
        say("the matching redirect URI in the Google Cloud console.", "r")
        sys.exit(1)

    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
    )
    thread.start()
    say(f"Listening on port {port} …", "d")

    say("\nOpening the Google consent screen in your browser.", "b")
    say("If it does not open, paste this URL yourself:\n", "d")
    print(url + "\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline and not _Catcher.result:
        time.sleep(0.25)

    server.shutdown()
    server.server_close()

    res = _Catcher.result
    if not res:
        say("\nTimed out waiting for the callback.", "r")
        say("Did the browser land on an error page? Paste it and I'll read it.", "d")
        sys.exit(1)
    if "error" in res:
        say(f"\nGoogle refused: {res['error']} — {res.get('error_description', '')}", "r")
        sys.exit(1)
    if res.get("state") != state:
        say("\nState mismatch — aborting.", "r")
        sys.exit(1)

    say("✓ Callback received.", "g")
    return res["code"]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main() -> None:
    cid = os.getenv("GOOGLE_CLIENT_ID")
    csec = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect = os.getenv("GOOGLE_REDIRECT_URI", DEFAULT_REDIRECT)

    if not cid or not csec:
        say("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing from .env", "r")
        sys.exit(1)

    parsed = urlparse(redirect)
    port = parsed.port
    if not port:
        say(f"GOOGLE_REDIRECT_URI has no port: {redirect}", "r")
        sys.exit(1)

    say(f"Redirect URI : {redirect}", "d")
    client = GoogleHealthClient(cid, csec, redirect)
    FIXTURES.mkdir(exist_ok=True)

    # ---- token ----
    tokens = None
    if TOKEN_CACHE.exists():
        cached = json.loads(TOKEN_CACHE.read_text())
        if cached.get("refresh_token"):
            say("Reusing the cached refresh token …", "d")
            try:
                tokens = await client.refresh(cached["refresh_token"])
            except GoogleHealthError as e:
                say(f"Refresh failed ({e.status}). Re-authorising.", "y")
                if "invalid_grant" in e.body:
                    say("  invalid_grant usually means the consent screen is in", "y")
                    say("  'Testing', where refresh tokens die after 7 days.", "y")

    if tokens is None:
        # Synchronous on purpose — the event loop must not be re-entered.
        code = await asyncio.get_running_loop().run_in_executor(
            None, run_oauth, client, port
        )
        tokens = await client.exchange_code(code)

    TOKEN_CACHE.write_text(json.dumps({
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at.isoformat(),
        "scope": tokens.scope,
    }, indent=2))

    say("\n✓ Access token acquired.", "g")
    say(f"  refresh_token : {'yes' if tokens.refresh_token else 'NO — add access_type=offline'}",
        "g" if tokens.refresh_token else "r")
    say(f"  granted scope : {tokens.scope or '(not reported)'}", "d")

    # ---- probe every data type ----
    # Filter grammar is confirmed and per-type (FILTER_TEMPLATE_BY_TYPE in
    # services/google_health.py) — list_data_points applies it automatically,
    # so this just verifies each type actually returns data for the account.
    yesterday = date.today() - timedelta(days=1)
    say(f"\nProbing data types for {yesterday} …\n", "b")

    rows: list[tuple[str, str, str]] = []

    for friendly, path in DATA_TYPES.items():
        result, note = None, ""
        try:
            result = await client.list_data_points(tokens.access_token, path, yesterday, yesterday)
            note = "filtered" if path in FILTER_TEMPLATE_BY_TYPE else "unfiltered (no template for this type)"
        except GoogleHealthError as e:
            note = str(e.status)
            if e.status in (401, 403):
                (FIXTURES / f"{friendly}.error.txt").write_text(e.body)

        if result is None:
            rows.append((friendly, C["r"] + "FAIL" + C["x"], note))
            continue

        points = result.get("dataPoints", []) or []
        (FIXTURES / f"{friendly}.json").write_text(json.dumps(result, indent=2))
        mark = (C["g"] + f"{len(points)} points" + C["x"]) if points \
            else (C["y"] + "empty" + C["x"])
        rows.append((friendly, mark, note))

    # ---- report ----
    say("\n" + "=" * 58, "b")
    say(f"{'DATA TYPE':<18}{'RESULT':<24}{'VIA'}", "b")
    say("=" * 58, "b")
    for name, mark, note in rows:
        print(f"{name:<18}{mark:<33}{C['d']}{note}{C['x']}", flush=True)
    say("=" * 58, "b")

    got = [r for r in rows if "points" in r[1]]
    say(f"\nFixtures written to {FIXTURES}/", "d")
    if got:
        say(f"\n✓ {len(got)}/{len(rows)} data types returned real data.", "g")
        say("  The project is viable. Build the summary generator against these files.", "g")
    else:
        say("\n✗ Nothing returned data.", "r")
        say("  Check: is health.googleapis.com enabled in the Cloud project?", "r")
        say("  Is this Google account linked to a Fitbit or Pixel Watch that synced recently?", "r")
        say("  Any *.error.txt in fixtures/ holds the raw response — paste it to me.", "r")


if __name__ == "__main__":
    asyncio.run(main())
