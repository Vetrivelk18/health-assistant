# AGENTS.md

Instructions for coding agents (Claude Code, etc.) working in this repo.

## What this is

FastAPI backend that reads Fitbit/Pixel Watch data through the **Google
Health API** (`health.googleapis.com`), summarizes it with Gemini, and
delivers it over a Telegram bot. Production target is Cloud Run + Cloud
Scheduler + Neon Postgres — see `DEPLOY.md`. See `README.md` for the full
architecture and setup instructions.

## Current state — read before touching OAuth or the Health API client

`services/google_health.py` was rewritten to target the Google Health API.
The old code targeted `fitness.googleapis.com` (Google Fit), which is closed
to new sign-ups and is being deprecated in 2026. `routes/auth.py` and the
`GOOGLE_HEALTH_SCOPES` default in `config.py` have been updated to match the
new `GoogleHealthClient` interface (constructor takes `client_id`/
`client_secret`/`redirect_uri`/`scopes`; methods are `authorization_url()`,
`exchange_code()`, `refresh()`, returning a `TokenBundle` instead of a raw
dict) — `tests/test_auth.py` covers the OAuth routes and passes.

`services/gemini.py` (Gemini function-calling loop) and `routes/mcp_tools.py`
(`GET /mcp/tools`, `POST /mcp/query`) are built and wired into `app.py`.
`services/gemini.py` defines one tool, `get_health_metric`, that Gemini calls
to pull a Fitbit/Pixel Watch metric via `GoogleHealthClient.list_data_points`;
`tests/test_mcp_tools.py` covers the routes with mocked Gemini/Google calls.
It uses `google-genai` (not the deprecated `google-generativeai`) via the
async client (`client.aio.models.generate_content`); the system prompt goes
in `GenerateContentConfig(system_instruction=...)`, not a top-level `system=`
kwarg the way Anthropic did it. `_client` is constructed lazily and stays
`None` if `GEMINI_API_KEY` is unset, so an unconfigured key doesn't crash
module import — it only raises (`_require_client()`) when something actually
tries to call the API.

Swapping Claude for Gemini also forced a dependency cleanup: `anthropic` and
the unused `python-telegram-bot`/`mcp` packages are gone, and `fastapi` (→
`0.115.0`), `httpx` (→ `0.28.1`), and `pydantic`/`pydantic-settings` (→
`2.9.2`/`2.5.2`) were bumped because `google-genai` needs `anyio>=4.8` /
`httpx>=0.28.1` / `pydantic>=2.9`, which the old pins couldn't satisfy.

`config.py`'s `GEMINI_MODEL` default is `gemini-3.5-flash-lite`, not
`gemini-2.5-flash-lite` — pricing docs say 2.5-flash-lite is cheaper, but the
live API 404s it for this account ("no longer available to new users") and
names 3.5-flash-lite as the replacement. Both `generate_daily_summary` and
`answer_health_query` have been run against the real API with a real key and
work end-to-end (confirmed function calling actually round-trips: Gemini
calls `get_health_metric`, gets real tool output, answers from it). One
cosmetic-only issue: the SDK logs a pydantic `UserWarning` about
`Content`/`Part`/`File` type mismatches when re-appending a `types.Content`
returned from a previous turn back into the `contents` list for the next
function-calling round — harmless, doesn't affect the result, not worth
chasing.

`routes/telegram.py` (webhook: `/start`, `/connect`, `/disconnect`,
`/status`, plus plain-text queries routed through `services/gemini.py`) and
`routes/internal.py` (`POST /internal/run-daily`, called by Cloud Scheduler)
are built and wired in. `routes/health.py` (the separate `/api/health/*`
endpoints, distinct from the existing `GET /health` liveness check) doesn't
exist yet.

One thing still open:

- The exact `filter` query grammar for `GET /v4/.../dataPoints` isn't fully
  documented by Google. `FILTER_TEMPLATES` in `services/google_health.py`
  holds untested candidates; `FILTER_TEMPLATE_IN_USE` is `None` until one is
  confirmed working. Run `python3 probe_health_api.py` (standalone, no
  FastAPI/DB needed — see `README_PROBE.md`) against a real account to find
  it, then set `FILTER_TEMPLATE_IN_USE`. Until then, `get_health_metric` tool
  calls go out unfiltered (fine for `sleep`/`steps`/etc. which default to a
  single day, just less efficient).

## Commands

```bash
# Install deps (project uses a venv/ directory — do not commit it)
pip install -r requirements.txt

# Run the app
python app.py                  # or: uvicorn app:app --reload

# Tests
pytest
pytest --cov=.

# Lint / format
black .
flake8 .

# Standalone Google Health API probe (no server/DB required)
python3 probe_health_api.py
```

## Conventions

- Config is centralized in `config.py` (`Settings`, loaded from `.env` via
  `python-dotenv`). Don't read `os.environ` directly elsewhere.
- Business logic lives in `services/`; `routes/` should stay thin.
- Secrets belong in `.env`, never `.env.example`. Never commit `.env`,
  `.probe_tokens.json`, or anything under `fixtures/` (all gitignored) —
  the probe script writes real OAuth tokens and raw API responses to those.
- `venv/` is a real virtualenv (~200MB) and must stay gitignored; don't
  `git add -A` without checking `git status` first.

## Gotchas

- **macOS `localhost` vs `127.0.0.1`**: `localhost` can resolve to IPv6 `::1`
  while a plain `HTTPServer` binds IPv4 only, causing
  `ERR_CONNECTION_REFUSED` right after a successful consent screen. Use
  literal `127.0.0.1` in redirect URIs during local dev.
- **7-day refresh tokens**: while the OAuth consent screen is in *Testing*,
  Google expires refresh tokens after 7 days. Not a bug — either re-auth
  weekly or move the consent screen to *Production* (which needs OAuth
  verification for real users).
- **Restricted scopes**: the `googlehealth.*.readonly` scopes require adding
  yourself as a test user on the OAuth consent screen while in Testing mode;
  no verification needed until you leave Testing.
- **No in-process scheduler, on purpose**: Cloud Run containers don't stay
  alive between requests, so anything like APScheduler (previously listed in
  `requirements.txt`, never actually wired up) would silently never fire.
  The daily job is triggered externally — Cloud Scheduler calls
  `POST /internal/run-daily` and the whole run (refresh → fetch → summarise
  → store → send) happens synchronously inside that one request. Don't defer
  any of it with a background task — Cloud Run freezes CPU the instant the
  response is sent, so anything deferred past that point never completes.
- **`/internal/run-daily` auth**: the Cloud Run service is deployed
  `--allow-unauthenticated` (Telegram has to reach the webhook), so this one
  endpoint verifies the Google-signed OIDC token Cloud Scheduler sends
  instead, checking both `audience` and the exact service-account email. See
  `DEPLOY.md`.
