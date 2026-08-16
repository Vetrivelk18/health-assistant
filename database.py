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

# Create engine
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQLALCHEMY_ECHO,
    poolclass=NullPool if settings.FASTAPI_ENV == "testing" else None,
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
