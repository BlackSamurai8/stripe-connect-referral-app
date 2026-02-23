"""
Database session management with model re-exports.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import get_settings
from models import (
    Base, Affiliate, AffiliateStatus, Campaign, Sale, Commission, CommissionStatus,
    CommissionTier, AuditLog, DeadLetterQueue, WebhookEvent, Payout, PayoutStatus
)

settings = get_settings()

# For SQLite, need check_same_thread=False for FastAPI's async
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
