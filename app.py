"""
AI Health Assistant - Main FastAPI Application
Integrates Google Health API, Gemini AI, and Telegram Bot
"""

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
import os
from dotenv import load_dotenv
from sqlalchemy import text

from utils.logging_config import configure_logging, log_event

# Load environment variables
load_dotenv()

# Structured JSON logs in production so Cloud Logging parses severity and
# per-user fields; human-readable lines locally. See utils/logging_config.py.
configure_logging(
    os.getenv("LOG_LEVEL", "INFO"),
    structured=os.getenv("FASTAPI_ENV", "development") != "development",
)
logger = logging.getLogger(__name__)

# Import routers
from routes import auth, internal, mcp_tools, telegram
# from routes import health

# Lifespan context for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 Starting Health Assistant API")

    # Schema management is Alembic's job, not the app's. Creating tables on
    # boot is a development convenience: in production it would race between
    # concurrent Cloud Run instances during a cold start, and it silently
    # can't apply column changes anyway (see database.init_db). Migrations
    # run as a deliberate step before deploying — see DEPLOY.md.
    if os.getenv("FASTAPI_ENV", "development") == "development":
        from database import init_db
        init_db()
    else:
        logger.info("Skipping create_all — schema is managed by Alembic")

    yield
    # Shutdown
    logger.info("🛑 Shutting down Health Assistant API")

# Create FastAPI app
app = FastAPI(
    title="AI Health Assistant",
    description="Fitbit + Gemini + Telegram integration for personalized health insights",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last line of defence: log unhandled exceptions with a stack trace and
    the request path attached, then return a generic 500.

    Without this, an unhandled exception is logged by uvicorn as a bare
    traceback with no request context — and the exception text can leak
    internals (connection strings, tokens) into the response body.
    """
    log_event(
        logger,
        logging.ERROR,
        f"Unhandled exception on {request.method} {request.url.path}",
        exc_info=True,
        event="unhandled_exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Health check endpoint
@app.get("/health")
async def health_check(response: Response):
    from database import SessionLocal

    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception as e:
        logger.error(f"🚨 /health DB check failed: {e}")
        response.status_code = 503
        return {"status": "unhealthy", "service": "AI Health Assistant", "detail": "database unreachable"}

    return {
        "status": "healthy",
        "service": "AI Health Assistant",
        "version": "1.0.0"
    }

# Root endpoint
@app.get("/")
async def root():
    return {
        "message": "AI Health Assistant API",
        "docs": "/docs",
        "health": "/health"
    }

# Include routers
app.include_router(auth.router, prefix="/auth", tags=["authentication"])
app.include_router(mcp_tools.router, prefix="/mcp", tags=["mcp"])
app.include_router(telegram.router, prefix="/webhook", tags=["telegram"])
app.include_router(internal.router, prefix="/internal", tags=["internal"])
# app.include_router(health.router, prefix="/api/health", tags=["health"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=os.getenv("FASTAPI_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("FASTAPI_PORT", 5000))),
        reload=os.getenv("FASTAPI_ENV") == "development"
    )
