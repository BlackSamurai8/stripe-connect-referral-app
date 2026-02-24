"""
Database session management with model re-exports.
"""


from sqlalchemy import create_engine, text
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




def upgrade_db():
    """Add missing columns to existing tables."""
    alterations = [
        "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS metadata_json JSON",
        "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS ghl_contact_id VARCHAR",
        "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS phone VARCHAR",
        "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS depth INTEGER DEFAULT 0",
        "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS stripe_onboarding_complete BOOLEAN DEFAULT FALSE",
        "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS customer_email VARCHAR",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS ghl_order_id VARCHAR",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS metadata_json JSON",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS campaign_id VARCHAR",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS currency VARCHAR DEFAULT 'usd'",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS stripe_payment_intent_id VARCHAR",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS rate FLOAT",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS level INTEGER",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS amount_cents INTEGER",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'pending'",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS hold_until TIMESTAMP",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS stripe_transfer_id VARCHAR",
        "ALTER TABLE commissions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS commission_tiers JSON",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS max_depth INTEGER DEFAULT 5",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS hold_days INTEGER DEFAULT 30",
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP",
        "ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        "ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP",
        "ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS max_retries INTEGER DEFAULT 3",
        "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
        "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS processing_time_ms INTEGER",
        "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0",
                "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS payload_json JSON",
                        "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS processed BOOLEAN DEFAULT false",
                                "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()",
                                        "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS source VARCHAR",
                                                "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS event_type VARCHAR",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS details_json JSON",
                "ALTER TABLE affiliates ALTER COLUMN status TYPE VARCHAR USING status::text",
                        "ALTER TABLE commissions ALTER COLUMN status TYPE VARCHAR USING status::text",
                                "ALTER TABLE payouts ALTER COLUMN status TYPE VARCHAR USING status::text",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS entity_type VARCHAR",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS entity_id VARCHAR",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS changes JSON",
    ]
    with engine.connect() as conn:
        for sql in alterations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
