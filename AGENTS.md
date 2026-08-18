# AGENTS.md

Instructions for coding agents (Claude Code, etc.) working in this repo.

## What this is

FastAPI backend that reads Fitbit/Pixel Watch data through the **Google
Health API** (`health.googleapis.com`), summarizes it with Claude, and
delivers it over a Telegram bot. See `README.md` for the full architecture
and setup instructions.

## Current state — read before touching OAuth or the Health API client

`services/google_health.py` was recently rewritten to target the Google
Health API. The old code targeted `fitness.googleapis.com` (Google Fit),
which is closed to new sign-ups and is being deprecated in 2026.

This rewrite is **not yet wired up** to the rest of the app:

- `routes/auth.py` imports a module-level `google_health_client` singleton
  and calls `.get_authorization_url()`. Neither exists anymore — the new
  `GoogleHealthClient` takes `client_id`/`client_secret`/`redirect_uri`
  explicitly in its constructor and exposes `authorization_url()` instead.
- `config.py` still defaults `GOOGLE_HEALTH_SCOPES` to the old
  `fitness.*.read` scopes, not the new `googlehealth.*.readonly` ones.
- The exact `filter` query grammar for `GET /v4/.../dataPoints` isn't fully
  documented by Google. `FILTER_TEMPLATES` in `services/google_health.py`
  holds untested candidates; `FILTER_TEMPLATE_IN_USE` is `None` until one is
  confirmed working.

Before extending OAuth or health-data fetching:

1. Run `python3 probe_health_api.py` (standalone, no FastAPI/DB needed — see
   `README_PROBE.md`) to confirm API access and discover the working filter
   grammar.
2. Set `FILTER_TEMPLATE_IN_USE` once known.
3. Update `routes/auth.py` and `config.py` to match the new client interface
   and scopes.

Don't assume `routes/auth.py` currently works — it will raise on import or
at call time until the above is done.

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
