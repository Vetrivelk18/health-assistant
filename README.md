# AI Health Assistant

An intelligent, app-less health assistant that connects your Fitbit or Pixel Watch data (via the Google Health API) with Gemini over Telegram. Get personalized daily health summaries at 7 AM and ask interactive health questions.

> **Status:** OAuth (`routes/auth.py`), the Gemini tool-use query pipeline
> (`services/gemini.py` + `routes/mcp_tools.py`), the Telegram webhook
> (`routes/telegram.py` + `services/telegram_bot.py`), and the Cloud
> Scheduler daily-run endpoint (`routes/internal.py`) are built and wired up.
> The Gemini integration has been exercised against the live API with a real
> key — both `generate_daily_summary` and the `answer_health_query`
> function-calling loop work end-to-end (`gemini-2.5-flash-lite` 404s as "no
> longer available to new users"; the default is `gemini-3.5-flash-lite`).
> The `/api/health` endpoints are not built yet — see the project structure
> below. The one open item on the Google Health API client itself is the
> `filter` query grammar for fetching data points, which still needs a real
> account run through the probe script — see
> [Google Health API migration](#google-health-api-migration). Deployment
> (Cloud Run + Cloud Scheduler + Neon) is documented in
> [`DEPLOY.md`](DEPLOY.md).

## Architecture

```
Telegram Bot → FastAPI Backend (Cloud Run) → Google Health API (health.googleapis.com)
                    ↓                    ↖
                Gemini (function calling)  Neon Postgres
                    ↓
         get_health_metric tool call

Cloud Scheduler → POST /internal/run-daily (OIDC-authenticated) → daily summary per user
```

## Features

- **Daily Summaries** (7:00 AM): Personalized health insights from Fitbit data via Gemini, triggered by Cloud Scheduler
- **Interactive Queries**: Ask questions like "How was my deep sleep?" and get AI-powered answers
- **OAuth 2.0 Security**: Secure Google Health API integration
- **Telegram Interface**: No separate app needed

## Project Structure

```
.
├── app.py                    # Main FastAPI application
├── config.py                 # Configuration & environment
├── models.py                 # SQLAlchemy ORM models
├── database.py               # Database connection
├── requirements.txt          # Python dependencies
├── Dockerfile                # Cloud Run container build
├── .env.example              # Environment template
├── README.md                 # This file
├── DEPLOY.md                 # Cloud Run + Cloud Scheduler + Neon deployment guide
├── AGENTS.md                 # Instructions for coding agents working in this repo
├── PHASE2_OAUTH.md           # Notes from the OAuth build-out
├── README_PROBE.md           # How to run the standalone API probe script
├── probe_health_api.py       # Standalone script: verify Google Health API access
│
├── routes/                   # API endpoints
│   ├── auth.py              # OAuth 2.0 login/callback (built)
│   ├── mcp_tools.py         # Tool schema + interactive query endpoint (built)
│   ├── telegram.py          # Telegram webhook handler (built)
│   ├── internal.py          # POST /internal/run-daily, called by Cloud Scheduler (built)
│   └── health.py            # Health data endpoints (not built yet)
│
├── services/                # Business logic
│   ├── google_health.py     # Google Health API client (built)
│   ├── gemini.py            # Gemini integration + function-calling loop (built)
│   └── telegram_bot.py      # Telegram bot helper (built)
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
`GoogleHealthClient` interface. What's still open:

1. Run the standalone probe script to confirm the API returns real data for
   your account and to discover the working filter grammar — see
   [`README_PROBE.md`](README_PROBE.md).
2. Once the probe reports a working filter, set `FILTER_TEMPLATE_IN_USE` in
   `services/google_health.py` per the script's instructions.

### 6. Telegram Bot Setup

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Create new bot (get token)
3. Set webhook URL: `https://yourdomain.com/webhook/telegram`
4. Copy token to `.env`

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
- `POST /internal/run-daily` - Generate and deliver the daily summary for
  every connected user. Called by Cloud Scheduler; verifies a Google-signed
  OIDC token rather than being open to the public. See [`DEPLOY.md`](DEPLOY.md).

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
           Refresh OAuth token if needed
           Fetch sleep/activity/heart rate data for "yesterday" in the
             user's own timezone (not the container's UTC clock)
           Pass to Gemini with system prompt → 3-bullet markdown summary
           Store the summary row
           Send to Telegram (a delivery failure here is logged, not
             thrown away — the stored summary survives it)
       → Respond to Cloud Scheduler (all synchronous; nothing deferred
         past the response, since Cloud Run freezes CPU after that point)
```

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

### Code Style

```bash
black .
flake8 .
```

### Database Migrations

```bash
alembic init alembic
alembic revision --autogenerate -m "Add users table"
alembic upgrade head
```

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
tokens after 7 days. Either re-authorize weekly or publish the OAuth consent
screen to **Production** (real users additionally require OAuth verification —
a security review plus a published privacy policy and terms of service).

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
