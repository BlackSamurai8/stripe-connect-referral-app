"""
Database models for the Stripe Connect Referral System.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, ForeignKey,
    Text, JSON, Enum as SAEnum, UniqueConstraint, Index, create_engine
)
from sqlalchemy.orm import declarative_base, relationship, Session
import enum
import uuid

Base = declarative_base()

def generate_uuid():
    return str(uuid.uuid4())

class AffiliateStatus(str, enum.Enum):
    PENDING = "pending"
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"

class CommissionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    PAID = "paid"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"

class PayoutStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class Affiliate(Base):
    __tablename__ = "affiliates"
    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    parent_id = Column(String, ForeignKey("affiliates.id"), nullable=True, index=True)
    depth = Column(Integer, default=0)
    referral_code = Column(String, unique=True, nullable=False, index=True)
    stripe_account_id = Column(String, nullable=True)
    stripe_onboarding_complete = Column(Boolean, default=False)
    status = Column(SAEnum(AffiliateStatus), default=AffiliateStatus.PENDING)
    ghl_contact_id = Column(String, nullable=True, index=True)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    parent = relationship("Affiliate", remote_side=[id], backref="children")
    sales = relationship("Sale", back_populates="affiliate")
    commissions = relationship("Commission", back_populates="affiliate")
    payouts = relationship("Payout", back_populates="affiliate")

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    commission_tiers = Column(JSON, nullable=False)  # e.g. [{"level":1,"rate":0.10},...]
    max_depth = Column(Integer, default=5)
    hold_days = Column(Integer, default=14)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    sales = relationship("Sale", back_populates="campaign")
    tiers = relationship("CommissionTier", back_populates="campaign", cascade="all, delete-orphan")

class Sale(Base):
    __tablename__ = "sales"
    id = Column(String, primary_key=True, default=generate_uuid)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String, default="usd")
    stripe_payment_intent_id = Column(String, nullable=True, unique=True)
    ghl_order_id = Column(String, nullable=True, unique=True)
    customer_email = Column(String, nullable=True)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    affiliate = relationship("Affiliate", back_populates="sales")
    campaign = relationship("Campaign", back_populates="sales")
    commissions = relationship("Commission", back_populates="sale")

class Commission(Base):
    __tablename__ = "commissions"
    id = Column(String, primary_key=True, default=generate_uuid)
    sale_id = Column(String, ForeignKey("sales.id"), nullable=False, index=True)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)
    level = Column(Integer, nullable=False)
    rate = Column(Float, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    status = Column(SAEnum(CommissionStatus), default=CommissionStatus.PENDING)
    hold_until = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    stripe_transfer_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    sale = relationship("Sale", back_populates="commissions")
    affiliate = relationship("Affiliate", back_populates="commissions")

class Payout(Base):
    __tablename__ = "payouts"
    id = Column(String, primary_key=True, default=generate_uuid)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String, default="usd")
    stripe_transfer_id = Column(String, nullable=True)
    status = Column(SAEnum(PayoutStatus), default=PayoutStatus.PENDING)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)

    affiliate = relationship("Affiliate", back_populates="payouts")

class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id = Column(String, primary_key=True)
    source = Column(String, nullable=False)  # "stripe" or "ghl"
    event_type = Column(String, nullable=False)
    payload_json = Column(JSON, nullable=True)
    processed = Column(Boolean, default=False)
    processing_time_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class CommissionTier(Base):
    __tablename__ = "commission_tiers"
    id = Column(String, primary_key=True, default=generate_uuid)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False, index=True)
    level = Column(Integer, nullable=False)
    rate = Column(Float, nullable=False)
    min_referrals_required = Column(Integer, default=0)
    bonus_rate = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('campaign_id', 'level', name='uq_campaign_level'),
    )

    campaign = relationship("Campaign", back_populates="tiers")

class DeadLetterQueue(Base):
    __tablename__ = "dead_letter_queue"
    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)  # "stripe" or "ghl"
    event_type = Column(String, nullable=False)
    payload_json = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    next_retry_at = Column(DateTime, nullable=True)
    resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=generate_uuid)
    action = Column(String, nullable=False)  # e.g. "payout_run", "commission_approve", "tier_update"
    actor = Column(String, default="system")
    details_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
