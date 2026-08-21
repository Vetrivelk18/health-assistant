"""
AI Health Assistant - Main FastAPI Application
Integrates Google Health API, Gemini AI, and Telegram Bot
"""

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import os
from dotenv import load_dotenv
from sqlalchemy import text

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Import routers
from routes import auth, internal, mcp_tools, telegram
# from routes import health

# Lifespan context for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 Starting Health Assistant API")
    from database import init_db
    init_db()
    logger.info("✅ Database initialized")
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
