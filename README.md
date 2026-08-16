# AI Health Assistant

An intelligent, app-less health assistant that connects your Fitbit account with Claude AI via Telegram. Get personalized daily health summaries at 7 AM and ask interactive health questions.

## Architecture

```
Telegram Bot → FastAPI Backend → Google Health API (Fitbit)
                    ↓
                 Claude 3.5 Sonnet
                    ↓
              MCP Tool Calling
```

## Features

- **Daily Summaries** (7:00 AM): Personalized health insights from Fitbit data via Claude
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
├── .env.example              # Environment template
├── README.md                 # This file
│
├── routes/                   # API endpoints
│   ├── auth.py              # OAuth 2.0 login/callback
│   ├── telegram.py          # Telegram webhook handler
│   ├── health.py            # Health data endpoints
│   └── mcp_tools.py         # MCP tool definitions
│
├── services/                # Business logic
│   ├── google_health.py     # Google Health API client
│   ├── claude.py            # Claude AI integration
│   ├── telegram_bot.py      # Telegram bot helper
│   └── scheduler.py         # Daily scheduler
│
├── utils/                   # Utilities
│   ├── logger.py            # Logging setup
│   ├── security.py          # Token encryption
│   └── decorators.py        # Custom decorators
│
└── tests/                   # Test suite
    └── test_*.py
```

## Setup Instructions

### 1. Prerequisites
- Python 3.10+
- PostgreSQL 12+
- Google Cloud Console account
- Telegram Bot (@BotFather)
- Claude API key (from Anthropic)

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
# CLAUDE_API_KEY
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
3. Enable "Google Fit API"
4. Create OAuth 2.0 credentials (Web Application)
5. Set redirect URI: `http://localhost:5000/auth/callback`
6. Copy Client ID & Secret to `.env`

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

### Authentication
- `GET /auth/login` - Start OAuth flow
- `GET /auth/callback?code=...&state=...` - OAuth callback

### Telegram Webhook
- `POST /webhook/telegram` - Receive Telegram messages

### Health Data
- `GET /api/health/summary/{user_id}` - Get latest summary
- `POST /api/health/query` - Interactive health query

### Health Check
- `GET /health` - Service status
- `GET /` - API info

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

Claude will:
1. Recognize the query needs Fitbit data
2. Call MCP tool to fetch sleep metrics
3. Provide personalized insight

## Deployment

### Docker

```bash
# Build image
docker build -t health-assistant .

# Run container
docker run -p 5000:5000 --env-file .env health-assistant
```

### Cloud (Railway/Render)

See `DEPLOYMENT.md` for step-by-step cloud deployment.

## Architecture Details

### Daily Summary Pipeline

```
7:00 AM → Scheduler fires → Fetch Fitbit tokens from DB
       → Refresh tokens if needed
       → Fetch sleep/activity/heart rate data
       → Pass to Claude with system prompt
       → Claude generates 3-bullet markdown
       → Send to Telegram
```

### Interactive Query Pipeline

```
User: "How was my deep sleep?"
       → Telegram webhook receives message
       → Extract user_id & message
       → Send to Claude with MCP tools available
       → Claude identifies needs sleep data
       → Claude calls get_sleep_data tool
       → MCP server hits Google Health API
       → Returns data to Claude
       → Claude synthesizes response
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

1. Check PostgreSQL connection
2. Verify APScheduler is running
3. Check logs for errors
4. Confirm Telegram token is valid

### Google Health API 401

1. Refresh OAuth token (auto-handled)
2. Verify `GOOGLE_CLIENT_SECRET` is correct
3. Check redirect URI matches console

## Security Notes

- Store `.env` securely (never commit)
- Tokens encrypted in database
- Use HTTPS in production
- Validate all Telegram webhook signatures
- Rotate Claude API key regularly

## License

MIT

## Support

Need help? Check:
- Issues on GitHub
- Logs in `logs/` directory
- Database state in PostgreSQL

---

**Made with ❤️ for your health**
