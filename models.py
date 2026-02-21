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
    FAILED = "failed"
    REFUNDED = "refunded"

class Affiliate(Base):
    __tablename__ = "affiliates"
    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    stripe_account_id = Column(String, unique=True, nullable=True, index=True)
    stripe_onboarding_complete = Column(Boolean, default=False)
    ghl_contact_id = Column(String, nullable=True, index=True)
    parent_id = Column(String, ForeignKey("affiliates.id"), nullable=True, index=True)
    referral_code = Column(String, unique=True, nullable=False, index=True)
    depth = Column(Integer, default=0)
    status = Column(SAEnum(AffiliateStatus), default=AffiliateStatus.PENDING)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    parent = relationship("Affiliate", remote_side=[id], backref="referrals")
    commissions = relationship("Commission", back_populates="affiliate")

    def get_upline(self, session: Session, max_depth: int = 10) -> list["Affiliate"]:
        upline = []
        current = self
        for _ in range(max_depth):
            if current.parent_id is None:
                break
            parent = session.get(Affiliate, current.parent_id)
            if parent is None:
                break
            upline.append(parent)
            current = parent
        return upline

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    commission_tiers = Column(JSON, nullable=False)
    max_depth = Column(Integer, default=5)
    hold_days = Column(Integer, default=30)
    stripe_product_id = Column(String, nullable=True)
    stripe_price_id = Column(String, nullable=True)
    ghl_product_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    sales = relationship("Sale", back_populates="campaign")

class Sale(Base):
    __tablename__ = "sales"
    id = Column(String, primary_key=True, default=generate_uuid)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False, index=True)
    stripe_payment_intent_id = Column(String, unique=True, nullable=True, index=True)
    stripe_charge_id = Column(String, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    ghl_contact_id = Column(String, nullable=True)
    ghl_order_id = Column(String, nullable=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String, default="usd")
    is_recurring = Column(Boolean, default=False)
    is_refunded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    affiliate = relationship("Affiliate")
    campaign = relationship("Campaign", back_populates="sales")
    commissions = relationship("Commission", back_populates="sale")

class Commission(Base):
    __tablename__ = "commissions"
    id = Column(String, primary_key=True, default=generate_uuid)
    sale_id = Column(String, ForeignKey("sales.id"), nullable=False, index=True)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)
    level = Column(Integer, nullable=False)
    percentage = Column(Float, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String, default="usd")
    status = Column(SAEnum(CommissionStatus), default=CommissionStatus.PENDING)
    stripe_transfer_id = Column(String, nullable=True, unique=True)
    paid_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    payable_after = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    sale = relationship("Sale", back_populates="commissions")
    affiliate = relationship("Affiliate", back_populates="commissions")
    __table_args__ = (
        UniqueConstraint("sale_id", "affiliate_id", name="uq_sale_affiliate"),
        Index("ix_commission_status_payable", "status", "payable_after"),
    )

class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id = Column(String, primary_key=True)
    source = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    processed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    payload_summary = Column(Text, nullable=True)
