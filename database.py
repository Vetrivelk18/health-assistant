"""
Database connection and session management
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from config import settings
from models import Base
import logging

logger = logging.getLogger(__name__)

# On Cloud Run a fresh pool is created on every cold start, so there's no
# benefit to a large persistent pool the way there would be on a long-lived
# server — NullPool (one connection per checkout, no idle pool) avoids
# holding stale connections across scale-to-zero gaps. pool_pre_ping guards
# against Neon closing an idle connection out from under us.
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQLALCHEMY_ECHO,
    poolclass=NullPool if settings.FASTAPI_ENV != "development" else None,
    pool_pre_ping=True,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db() -> Session:
    """Dependency for FastAPI endpoints to get DB session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Create any missing tables — development convenience only.

    **This is not a migration tool and must not be treated as one.**
    `create_all()` creates tables that don't exist and then silently ignores
    every table that does. Add a column to models.py and this reports
    success while changing nothing; the app then fails at runtime on a
    column the database doesn't have. The instinctive fix — drop and
    recreate — destroys the OAuth tokens, which cannot be regenerated
    without every user re-authorising.

    Schema changes go through Alembic instead:

        alembic revision --autogenerate -m "add users.device_pref"
        alembic upgrade head

    Kept for local development and tests, where a throwaway database is
    genuinely faster to create this way. Refuses to run in production, so
    it can't quietly diverge from the migration history.
    """
    if settings.FASTAPI_ENV != "development":
        raise RuntimeError(
            "init_db() is development-only — it cannot alter existing tables. "
            "Use `alembic upgrade head` to apply schema changes."
        )

    logger.info("Initializing database (development create_all)...")
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Database initialized")

def drop_db():
    """Drop all tables - USE WITH CAUTION"""
    logger.warning("🚨 Dropping all tables...")
    Base.metadata.drop_all(bind=engine)
    logger.warning("✅ All tables dropped")
