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
    """Initialize database - create all tables"""
    logger.info("Initializing database...")
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Database initialized")

def drop_db():
    """Drop all tables - USE WITH CAUTION"""
    logger.warning("🚨 Dropping all tables...")
    Base.metadata.drop_all(bind=engine)
    logger.warning("✅ All tables dropped")
