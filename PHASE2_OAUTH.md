# Phase 2: OAuth 2.0 Flow - Setup & Testing

## What We Built

✅ **Google Health API Client** (`services/google_health.py`)
- OAuth 2.0 authorization flow
- Token exchange (code → access/refresh tokens)
- Token refresh logic
- Health data fetching (sleep, activity, heart rate)

✅ **OAuth Routes** (`routes/auth.py`)
- `GET /auth/login?telegram_chat_id=123` - Start OAuth flow
- `GET /auth/callback?code=...&state=...` - OAuth callback
- `POST /auth/refresh-token` - Refresh expired tokens
- `GET /auth/status/{user_id}` - Check token status
- `GET /auth/disconnect/{user_id}` - Disconnect account

---

## Setup Steps

### 1. Update `.env` File

Copy the credentials from the JSON you provided:

```bash
cp .env.example .env
```

Then update your `.env` with:

```
# Google Cloud - OAuth 2.0 (Google Health API)
GOOGLE_CLIENT_ID=your_google_client_id_here.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_google_client_secret_here
GOOGLE_REDIRECT_URI=http://localhost:5000/auth/callback
GOOGLE_PROJECT_ID=health-assistant-505718
GOOGLE_TOKEN_URI=https://oauth2.googleapis.com/token

# Claude API
CLAUDE_API_KEY=your_claude_api_key_here

# Database
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/health_assistant

# Telegram (we'll set this up in Phase 4)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_WEBHOOK_URL=https://yourdomain.com/webhook/telegram
```

### 2. Install Dependencies

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 3. Set Up PostgreSQL Database

```bash
# Create database
createdb health_assistant

# Alternatively, if using Docker:
docker run --name postgres -e POSTGRES_PASSWORD=postgres -d -p 5432:5432 postgres:15
docker exec postgres psql -U postgres -c "CREATE DATABASE health_assistant"
```

### 4. Run the FastAPI Server

```bash
python app.py
# OR with uvicorn for development
uvicorn app:app --reload --host 0.0.0.0 --port 5000
```

You should see:
```
✅ Database initialized
📡 Uvicorn running on http://0.0.0.0:5000
```

---

## Testing the OAuth Flow

### Test 1: Start OAuth Flow

Open in browser or curl:

```bash
curl "http://localhost:5000/auth/login?telegram_chat_id=123456789"
```

**Response:**
```json
{
  "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=...",
  "message": "Click the link above to authorize with Google Health (Fitbit)"
}
```

### Test 2: Get Authorization URL (Manual Test)

1. **Copy the auth_url** from the response
2. **Open it in your browser**
3. **Sign in with your Google account** (the one connected to Fitbit)
4. **Approve** the permission request for:
   - Sleep data access
   - Activity data access
   - Heart rate data access
5. **You'll be redirected** to `http://localhost:5000/auth/callback?code=...&state=...`
6. **Check the response** - should say "Successfully connected!"

### Test 3: Check Token Status

After authorization, use the returned `user_id`:

```bash
curl "http://localhost:5000/auth/status/{user_id}"
```

**Response:**
```json
{
  "authenticated": true,
  "expired": false,
  "expiring_soon": false,
  "expires_at": "2024-12-20T15:30:45.123456",
  "scope": "https://www.googleapis.com/auth/fitness.sleep.read ...",
  "created_at": "2024-12-20T14:30:45.123456"
}
```

### Test 4: Disconnect Account

```bash
curl "http://localhost:5000/auth/disconnect/{user_id}"
```

---

## How OAuth Flow Works

```
1. User sends: GET /auth/login?telegram_chat_id=123456789
   ↓
2. Backend generates random state & stores it
   ↓
3. Backend returns Google OAuth auth_url
   ↓
4. User clicks link, redirected to Google's consent screen
   ↓
5. User approves access to Fitbit data
   ↓
6. Google redirects to: GET /auth/callback?code=ABC123&state=XYZ789
   ↓
7. Backend validates state (CSRF check)
   ↓
8. Backend exchanges code for tokens (access + refresh)
   ↓
9. Backend stores tokens in PostgreSQL (encrypted)
   ↓
10. Backend responds with success + user_id
   ↓
11. User saved! Can now fetch health data.
```

---

## What Happens Next (Phase 3)

- Build Claude integration for health summaries
- Create system prompt for 3-bullet format
- Implement Telegram bot webhook
- Test end-to-end flow

---

## Troubleshooting

### Issue: "Invalid OAuth state"
**Cause:** State expired or invalid
**Fix:** Generate new auth_url with fresh telegram_chat_id

### Issue: "SQLALCHEMY_ECHO: connection refused"
**Cause:** PostgreSQL not running
**Fix:** Start PostgreSQL: `docker run ... postgres:15` or ensure psql service is running

### Issue: "Token exchange failed"
**Cause:** Invalid code or wrong credentials
**Fix:** Make sure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET match your Google Cloud console

### Issue: "Redirect URI mismatch"
**Cause:** Redirect URI in code doesn't match Google Cloud console
**Fix:** Ensure `GOOGLE_REDIRECT_URI=http://localhost:5000/auth/callback` matches your console

---

## Next: Phase 3 - Claude Integration

Ready? Let me know and I'll build:
- ✅ Claude client integration
- ✅ Health summary generation (3-bullet format)
- ✅ System prompt design
