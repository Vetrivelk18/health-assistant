"""
Configuration and environment setup
"""

import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Google Cloud
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI: str = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/callback")
    GOOGLE_PROJECT_ID: str = os.getenv("GOOGLE_PROJECT_ID", "health-assistant-505718")
    GOOGLE_AUTH_URI: str = os.getenv("GOOGLE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth")
    GOOGLE_TOKEN_URI: str = os.getenv("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")
    GOOGLE_HEALTH_SCOPES: list = os.getenv("GOOGLE_HEALTH_SCOPES", "").split() or [
        "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
        "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
        "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    ]

    # Gemini API
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_WEBHOOK_URL: str = os.getenv("TELEGRAM_WEBHOOK_URL")
    # Shared secret echoed back by Telegram in the
    # X-Telegram-Bot-Api-Secret-Token header on every webhook delivery. The
    # webhook URL is public and unauthenticated (it has to be — Telegram
    # can't do OIDC), so without this anyone who learns the URL can POST a
    # forged update carrying someone else's chat_id. Set it when registering
    # the webhook via setWebhook(secret_token=...). See DEPLOY.md.
    TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/health_assistant")
    SQLALCHEMY_ECHO: bool = os.getenv("FASTAPI_ENV") == "development"

    # FastAPI
    FASTAPI_ENV: str = os.getenv("FASTAPI_ENV", "development")
    FASTAPI_HOST: str = os.getenv("FASTAPI_HOST", "0.0.0.0")
    FASTAPI_PORT: int = int(os.getenv("FASTAPI_PORT", 5000))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

    # Scheduler
    SCHEDULER_HOUR: int = int(os.getenv("SCHEDULER_HOUR", 7))
    SCHEDULER_MINUTE: int = int(os.getenv("SCHEDULER_MINUTE", 0))
    TIMEZONE: str = os.getenv("TIMEZONE", "UTC")

    # Cloud Scheduler -> POST /internal/run-daily auth. The Cloud Run service
    # is deployed --allow-unauthenticated (Telegram must reach the webhook),
    # so this endpoint verifies the Google-signed OIDC token Cloud Scheduler
    # sends instead. See DEPLOY.md.
    RUN_DAILY_AUDIENCE: str = os.getenv("RUN_DAILY_AUDIENCE")  # the deployed Cloud Run service URL
    SCHEDULER_SERVICE_ACCOUNT_EMAIL: str = os.getenv("SCHEDULER_SERVICE_ACCOUNT_EMAIL")

    # Cloud Tasks fan-out: /internal/run-daily enqueues one task per user
    # instead of processing the whole batch inline. Tasks call back into
    # /internal/run-single-user carrying their own OIDC token, verified the
    # same way. First 1M operations/month are free. See DEPLOY.md.
    #
    # TASKS_QUEUE unset (the local-dev default) makes /internal/run-daily
    # fall back to running the batch inline, so the endpoint still works
    # without a queue to talk to.
    TASKS_QUEUE: str = os.getenv("TASKS_QUEUE")
    TASKS_LOCATION: str = os.getenv("TASKS_LOCATION", "us-central1")
    # Base URL tasks are delivered to — defaults to the same Cloud Run
    # service URL the scheduler already targets.
    TASKS_TARGET_BASE_URL: str = os.getenv("TASKS_TARGET_BASE_URL", os.getenv("RUN_DAILY_AUDIENCE"))
    # Service account Cloud Tasks mints its OIDC tokens as. Defaults to the
    # scheduler's, so a single SA works if you don't want to split them.
    TASKS_SERVICE_ACCOUNT_EMAIL: str = os.getenv(
        "TASKS_SERVICE_ACCOUNT_EMAIL", os.getenv("SCHEDULER_SERVICE_ACCOUNT_EMAIL")
    )

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # MCP Server
    MCP_SERVER_HOST: str = os.getenv("MCP_SERVER_HOST", "localhost")
    MCP_SERVER_PORT: int = int(os.getenv("MCP_SERVER_PORT", 3000))

settings = Settings()

# Validate required settings
def validate_settings():
    required = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN"]
    missing = [key for key in required if not getattr(settings, key)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

if settings.FASTAPI_ENV != "development":
    validate_settings()
