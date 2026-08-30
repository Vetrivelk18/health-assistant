# AI Health Assistant

An intelligent, app-less health assistant that connects your Fitbit or Pixel Watch data (via the Google Health API) with Gemini over Telegram. Get personalized daily health summaries at 7 AM and ask interactive health questions.

> **Status:** OAuth (`routes/auth.py`), the Gemini tool-use query pipeline
> (`services/gemini.py` + `routes/mcp_tools.py`), the Telegram webhook
> (`routes/telegram.py` + `services/telegram_bot.py`), and the daily-run
> endpoints (`routes/internal.py`) are built and wired up.
>
> The daily run now **fans out over Cloud Tasks** — `/internal/run-daily`
> enqueues one task per user and returns immediately, and Cloud Tasks
> dispatches each back to `/internal/run-single-user` at a capped rate.
> Failures are classified transient vs. permanent so bounded retry budgets
> are spent only on problems that might resolve, and logs are structured
> JSON for Cloud Logging. See [`DEPLOY.md`](DEPLOY.md).
>
> The Gemini integration has been exercised against the live API with a real
> key — both `generate_daily_summary` and the `answer_health_query`
> function-calling loop work end-to-end (`gemini-2.5-flash-lite` 404s as "no
> longer available to new users"; the default is `gemini-3.5-flash-lite`).
> The `/api/health` endpoints are not built yet — see the project structure
> below. The Google Health API `filter` grammar is now confirmed against a
> real account and baked into `FILTER_TEMPLATE_BY_TYPE` — see
> [Google Health API migration](#google-health-api-migration) — but that same
> probe run returned zero data points for every type over a 30-day window, so
> either the connected Google account has no Fitbit/Pixel Watch actively
> syncing to it, or there's a wearable-linkage step still missing; `calories`
> is separately and permanently broken via this client (`total-calories` only
> supports `rollup`/`dailyRollUp`, not `list`).
>
> **Not yet deployed or run against real infrastructure.** The Cloud Tasks
> fan-out, retry policies and structured logging are covered by tests
> (65 passing) but the queue, its IAM bindings and the log-based metric have
> not been created in a real project — `DEPLOY.md` §5–6 are written, not
> executed.

## Architecture

```
Telegram Bot → FastAPI Backend (Cloud Run) → Google Health API (health.googleapis.com)
                    ↓                    ↖
                Gemini (function calling)  Neon Postgres
                    ↓
         get_health_metric tool call

Cloud Scheduler → POST /internal/run-daily (OIDC-authenticated)
                       → enqueues 1 Cloud Task per user, returns in <1s
                       → Cloud Tasks (≤5/sec) → POST /internal/run-single-user
```

## Features

- **Daily Summaries** (7:00 AM): Personalized health insights from Fitbit data via Gemini, triggered by Cloud Scheduler
- **Interactive Queries**: Ask questions like "How was my deep sleep?" and get AI-powered answers
- **OAuth 2.0 Security**: Secure Google Health API integration
- **Telegram Interface**: No separate app needed
- **Graceful degradation**: A metric that fails still yields a summary from
  the rest of the data — but a full API outage retries rather than claiming
  you logged nothing
- **Bounded retries**: Failures are classified transient vs. permanent, so
  retry budgets are spent only on problems that might actually resolve
- **Structured logging**: JSON logs with per-user fields, queryable in Cloud
  Logging (see [`DEPLOY.md`](DEPLOY.md#6-logging--observability))

## Project Structure

```
.
├── app.py                    # Main FastAPI application
├── config.py                 # Configuration & environment
├── models.py                 # SQLAlchemy ORM models
├── database.py               # Database connection
├── alembic.ini               # Migration config (url injected from env)
├── alembic/                  # Schema migrations — see DEPLOY.md §1
│   └── versions/
├── requirements.txt          # Python dependencies
├── Dockerfile                # Cloud Run container build
├── .env.example              # Environment template
├── README.md                 # This file
├── DEPLOY.md                 # Cloud Run + Scheduler + Tasks + Neon deployment guide
├── AGENTS.md                 # Instructions for coding agents working in this repo
├── PHASE2_OAUTH.md           # Notes from the OAuth build-out
├── README_PROBE.md           # How to run the standalone API probe script
├── probe_health_api.py       # Standalone script: verify Google Health API access
│
├── routes/                   # API endpoints
│   ├── auth.py              # OAuth 2.0 login/callback (built)
│   ├── mcp_tools.py         # Tool schema + interactive query endpoint (built)
│   ├── telegram.py          # Telegram webhook handler (built)
│   ├── internal.py          # Daily-run dispatcher + per-user worker (built)
│   └── health.py            # Health data endpoints (not built yet)
│
├── services/                # Business logic
│   ├── google_health.py     # Google Health API client (built)
│   ├── gemini.py            # Gemini integration + function-calling loop (built)
│   ├── tasks.py             # Cloud Tasks fan-out for the daily run (built)
│   └── telegram_bot.py      # Telegram bot helper (built)
│
├── utils/
│   └── logging_config.py    # Structured JSON logging for Cloud Logging
│
└── tests/                   # Test suite
    └── test_*.py
```

## Setup Instructions

### 1. Prerequisites
- Python 3.10+
- PostgreSQL 12+ (Neon in production — see [`DEPLOY.md`](DEPLOY.md))
- Google Cloud Console account
- Telegram Bot (@BotFather)
- Gemini API key (from [Google AI Studio](https://aistudio.google.com/apikey), free tier available)

### 2. Clone & Install

```bash
# Clone repository
git clone <your-repo>
cd health-assistant

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Setup

```bash
# Copy template
cp .env.example .env

# Fill in your credentials
# GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
# GEMINI_API_KEY
# TELEGRAM_BOT_TOKEN
# DATABASE_URL
```

### 4. Database Initialization

```bash
# Create database (PostgreSQL)
createdb health_assistant

# Run migrations (or initialize schema)
python -c "from database import init_db; init_db()"
```

### 5. Google Cloud Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project
3. Enable **"Google Health API"** — *not* "Google Fit API". Google Fit closed
   to new sign-ups on 1 May 2024 and is being deprecated in 2026; the Google
   Health API (`health.googleapis.com`) is the supported successor and reads
   from the same Fitbit/Pixel Watch devices. See
   [Google Health API migration](#google-health-api-migration).
4. Create OAuth 2.0 credentials (Web Application)
5. Set redirect URI to match `GOOGLE_REDIRECT_URI` in `.env` (default
   `http://127.0.0.1:8765/auth/callback` for the probe script;
   `http://localhost:5000/auth/callback` for the FastAPI app)
6. Add the scopes from `.env.example` (`GOOGLE_HEALTH_SCOPES`) on the OAuth
   consent screen — they are **Restricted** scopes, so add yourself as a test
   user rather than going through verification
7. Copy Client ID & Secret to `.env`

#### Google Health API migration

`services/google_health.py` was rewritten to target `health.googleapis.com`
instead of the old `fitness.googleapis.com` (Google Fit) endpoints, and
`routes/auth.py`/`config.py` have been updated to match the new
`GoogleHealthClient` interface.

The `filter` grammar is confirmed (run against a real account via
`probe_health_api.py` on 2026-08-22 — see [`README_PROBE.md`](README_PROBE.md))
and is **per data type**, not one shared grammar — each restricts on a
different member path, always prefixed with the type's snake_case name:

| Type | Member path |
|---|---|
| `sleep` | `sleep.interval.end_time` |
| `steps` | `steps.interval.start_time` |
| `heart-rate` | `heart_rate.sample_time.physical_time` |
| `active-minutes` | `active_minutes.interval.start_time` |
| `total-calories` | none — `list` 400s unconditionally; only `rollup`/`dailyRollUp` are supported, which this client doesn't implement |

`FILTER_TEMPLATE_BY_TYPE` in `services/google_health.py` holds these, and
`list_data_points` applies the right one automatically — no per-call setup
needed. What's still open: that same probe run returned zero data points for
every type over a 30-day window against the connected test account, so the
plumbing is confirmed correct but real device data hasn't been confirmed yet
— re-run `probe_health_api.py` against an account with a Fitbit/Pixel Watch
that's synced recently.

### 6. Telegram Bot Setup

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Create new bot (get token)
3. Generate a webhook secret: `openssl rand -hex 32` → `TELEGRAM_WEBHOOK_SECRET`
4. Register the webhook **with that secret**:

   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=https://yourdomain.com/webhook/telegram" \
     -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```

   Telegram echoes it back in the `X-Telegram-Bot-Api-Secret-Token` header
   on every delivery, and the endpoint rejects anything without it. The
   webhook URL is public, so this is what stops a stranger POSTing a forged
   update carrying your `chat_id`. Required in production; optional locally.
5. Copy token to `.env`

### 7. Run Locally

```bash
# Start the server
python app.py

# Or with uvicorn
uvicorn app:app --reload

# Server runs at http://localhost:5000
```

## API Endpoints

### Authentication (built)
- `GET /auth/login` - Start OAuth flow
- `GET /auth/callback?code=...&state=...` - OAuth callback
- `POST /auth/refresh-token` - Manually refresh an OAuth token
- `GET /auth/status/{user_id}` - Check token status
- `GET /auth/disconnect/{user_id}` - Disconnect account

### MCP / Gemini tool use (built)
- `GET /mcp/tools` - The tool schema Gemini is given for health queries
- `POST /mcp/query` - Ask a natural-language health question for a connected user

### Telegram (built)
- `POST /webhook/telegram` - Receive Telegram updates. Handles `/start`,
  `/connect`, `/disconnect`, `/status`, and plain-text health questions
  (routed through the same Gemini/MCP query pipeline as `/mcp/query`).

### Internal (built)
- `POST /internal/run-daily` - Called by Cloud Scheduler. Enqueues one Cloud
  Task per connected user and returns immediately. Falls back to running the
  batch inline when `TASKS_QUEUE` is unset (local dev).
- `POST /internal/run-single-user` - Called by Cloud Tasks, once per user.
  Generates, stores and delivers that user's summary.

Both verify a Google-signed OIDC token rather than being open to the public.
See [`DEPLOY.md`](DEPLOY.md).

### Health Check (built)
- `GET /health` - Service status; returns 503 if the database is unreachable
- `GET /` - API info

### Not built yet
- `GET /api/health/summary/{user_id}` - Get latest daily summary
- `POST /api/health/query` - Interactive health query via a direct API call

## Usage

### Connect Fitbit Account

1. Open Telegram bot
2. Send `/connect`
3. Click auth link
4. Approve Google Health access
5. You're ready!

### Receive Daily Summaries

Every day at 7:00 AM (configurable), you'll receive:

```
🌅 Good morning! Here's your daily health snapshot:

🛌 Solid Sleep Recovery: You logged 6 hrs 52 mins of sleep with excellent 92% efficiency!

⚡ Goal Smashed: You hit 10,450 steps—surpassing your 10k daily goal—along with 42 Active Zone Minutes.

❤️ Heart Rate Trend: Your Resting Heart Rate dropped to 58 bpm, down 2 bpm, showing excellent cardiovascular recovery overnight.

Keep up the fantastic momentum today! 🚀
```

### Ask Questions

Send any health question:

```
How was my deep sleep last night?
```

Gemini will:
1. Recognize the query needs Fitbit data
2. Call the `get_health_metric` tool to fetch it
3. Provide personalized insight

## Deployment

Production target is Cloud Run + Cloud Scheduler + Neon Postgres — see
[`DEPLOY.md`](DEPLOY.md) for the full walkthrough (Neon setup, `gcloud run
deploy`, the OIDC-authenticated Cloud Scheduler job, secrets, budget alert).

For a local container smoke test:

```bash
docker build -t health-assistant .
docker run -p 8080:8080 --env-file .env -e PORT=8080 health-assistant
```

## Architecture Details

### Daily Summary Pipeline

```
Cloud Scheduler (07:00 in each region's config) → POST /internal/run-daily
       → Verify Google-signed OIDC token (audience + service-account email)
       → For each connected user:
           Skip if already summarised for their "yesterday"
           Enqueue one Cloud Task, named daily-{user_id}-{date}
       → Respond immediately (no per-user work happens here)

Cloud Tasks (dispatch capped at 5/sec) → POST /internal/run-single-user
       → Verify OIDC token, re-check last_summary_sent (idempotency)
       → Refresh OAuth token if needed
       → Fetch sleep/activity/heart rate data for "yesterday" in the
           user's own timezone (not the container's UTC clock)
       → Pass to Gemini with system prompt → 3-bullet markdown summary
       → Upsert the summary row
       → Send to Telegram (a delivery failure here is logged, not thrown
           away — the stored summary survives it)
       → 200 if done or permanently failed; 503 if worth retrying
```

The fan-out is what keeps this free. Doing the whole batch inline held one
instance's CPU and RAM for the entire run and risked blowing the Scheduler
attempt deadline; one short request per user keeps peak memory flat
regardless of user count and lets Cloud Run scale to zero in between. Cloud
Tasks' first 1M operations/month are free — see the cost table in
[`DEPLOY.md`](DEPLOY.md#9-cost-what-actually-stays-free).

Every step is synchronous within its own request: Cloud Run freezes CPU the
instant a response is sent, so the queue provides the "later", never a
background task.

### Interactive Query Pipeline

```
User: "How was my deep sleep?"
       → Telegram webhook receives message
       → Extract user_id & message
       → Send to Gemini with the get_health_metric tool available
       → Gemini identifies it needs sleep data
       → Gemini calls get_health_metric
       → MCP server hits Google Health API
       → Returns data to Gemini
       → Gemini synthesizes response
       → Send to Telegram
```

## Development

### Run Tests

```bash
pytest
pytest --cov=.  # With coverage
```

Tests run against a throwaway `<your database>_test` database that
`tests/conftest.py` derives from `DATABASE_URL`, creates if missing, and
wipes between tests — **your development data is never touched**. Point it
elsewhere with `TEST_DATABASE_URL` if you'd rather.

It stays on Postgres rather than SQLite deliberately: the app depends on
Postgres behaviour (JSON columns, the summary upsert), so a suite green on
SQLite while production runs Postgres would be testing the wrong engine.

Before pushing, it's worth running what CI is intended to run:

```bash
pytest                                    # on 3.11 (production) and 3.9 (dev venv)
alembic upgrade head && alembic check     # models vs migrations must not drift
alembic downgrade base && alembic upgrade head   # every migration must reverse
```

> **CI workflow not yet added.** The `.github/workflows/ci.yml` for this is
> written and verified but couldn't be committed — pushing anything under
> `.github/workflows/` needs a credential with the `workflow` scope. Add it
> via GitHub's web editor (Actions → New workflow), or grant the scope with
> `gh auth refresh -h github.com -s workflow && gh auth setup-git` and
> commit it normally.

### Code Style

```bash
black .
flake8 .
```

### Database Migrations

Alembic is set up, with the current schema captured as a baseline revision.
**Never change the schema with `create_all()`** — it silently ignores tables
that already exist, so a new column appears to succeed while doing nothing.
`init_db()` is development-only and raises elsewhere.

```bash
# After editing models.py
alembic revision --autogenerate -m "add users.device_pref"

# Read the generated file — autogenerate renders renames as drop+create
# (data loss) and misses server defaults and check constraints
alembic upgrade head --sql   # preview the exact SQL
alembic upgrade head         # apply

# For a database that already has the tables (pre-Alembic), baseline once:
alembic stamp head
```

The connection URL comes from `DATABASE_URL` via `alembic/env.py`, not from
`alembic.ini` — that file is committed and the URL is a secret. Full
workflow, including rehearsing against a Neon branch, is in
[`DEPLOY.md`](DEPLOY.md#schema-changes-after-the-first-deploy).

## Troubleshooting

### OAuth Token Expired

Tokens auto-refresh. Check `oauth_tokens` table for expiration.

### Daily Summary Not Sending

1. Check the Neon/PostgreSQL connection (`GET /health`)
2. Confirm the Cloud Scheduler job ran: `gcloud scheduler jobs describe health-assistant-daily`
3. Check Cloud Run logs for `/internal/run-daily` — a 403 there usually means
   `RUN_DAILY_AUDIENCE` or `SCHEDULER_SERVICE_ACCOUNT_EMAIL` doesn't match
   what the job is actually configured with
4. Confirm Telegram token is valid

### Google Health API 401

1. Refresh OAuth token (auto-handled)
2. Verify `GOOGLE_CLIENT_SECRET` is correct
3. Check redirect URI matches console

### Refresh token stopped working after ~7 days

While the OAuth consent screen is in **Testing**, Google expires refresh
tokens after 7 days. This is expected, and the bot now handles it: it warns
you the day before expiry and, if a summary does fail, messages you to
`/connect` instead of going quiet. Reconnecting takes a few seconds and
resets the clock.

Publishing to **Production** removes the expiry but requires OAuth
verification — and the `googlehealth.*` scopes are Restricted, which adds a
paid annual security assessment. See
[`DEPLOY.md`](DEPLOY.md#10-going-public-oauth-consent-screen) for the full
tradeoff and a cheaper alternative.

### "Invalid state parameter" after a successful consent screen

The `state` token expired (10-minute TTL — just retry `/connect`), or
`SECRET_KEY` changed between starting the login and finishing it. If you're
running multiple instances, confirm they all share the same `SECRET_KEY`;
state is verified by signature rather than by shared storage, so a
mismatched key on one instance breaks only the callbacks that land there.

## Security Notes

- Store `.env` securely (never commit) — in production, use Secret Manager (see [`DEPLOY.md`](DEPLOY.md))
- Use HTTPS in production (Cloud Run terminates TLS for you)
- `/internal/run-daily` verifies a Google-signed OIDC token rather than being open to the public
- Rotate the Gemini API key regularly

## License

MIT

## Support

Need help? Check:
- Issues on GitHub
- Logs in `logs/` directory
- Database state in PostgreSQL

---

**Made with ❤️ for your health**
