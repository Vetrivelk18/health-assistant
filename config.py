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

    # Claude API
    CLAUDE_API_KEY: str = os.getenv("CLAUDE_API_KEY")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-opus-5")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_WEBHOOK_URL: str = os.getenv("TELEGRAM_WEBHOOK_URL")

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

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # MCP Server
    MCP_SERVER_HOST: str = os.getenv("MCP_SERVER_HOST", "localhost")
    MCP_SERVER_PORT: int = int(os.getenv("MCP_SERVER_PORT", 3000))

settings = Settings()

# Validate required settings
def validate_settings():
    required = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "CLAUDE_API_KEY", "TELEGRAM_BOT_TOKEN"]
    missing = [key for key in required if not getattr(settings, key)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

if settings.FASTAPI_ENV != "development":
    validate_settings()
