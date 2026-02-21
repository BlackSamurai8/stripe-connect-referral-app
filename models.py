"""
Database models for the Stripe Connect Referral System.

Referral tree structure:
- Each Affiliate has an optional parent_id (their referrer)
- This creates an adjacency-list tree supporting unlimited depth
- Campaigns define commission structures with per-level percentages
- Commissions track every payout across the entire tree
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
    PENDING = "pending"           # Invited, not yet onboarded
    ONBOARDING = "onboarding"     # Started Stripe Connect onboarding
    ACTIVE = "active"             # Fully onboarded, can receive payouts
    SUSPENDED = "suspended"       # Temporarily disabled
    DEACTIVATED = "deactivated"   # Permanently removed


class CommissionStatus(str, enum.Enum):
    PENDING = "pending"           # Sale recorded, waiting hold period
    APPROVED = "approved"         # Hold period passed, ready to pay
    PAID = "paid"                 # Transfer sent via Stripe
    FAILED = "failed"             # Transfer failed
    REFUNDED = "refunded"         # Original sale was refunded


class Affiliate(Base):
    """
    A participant in the referral program.
    Each affiliate can refer others, creating a tree structure.
    """
    __tablename__ = "affiliates"

    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)

    # Stripe Connect
    stripe_account_id = Column(String, unique=True, nullable=True, index=True)
    stripe_onboarding_complete = Column(Boolean, default=False)

    # GoHighLevel
    ghl_contact_id = Column(String, nullable=True, index=True)

    # Referral tree (adjacency list)
    parent_id = Column(String, ForeignKey("affiliates.id"), nullable=True, index=True)
    referral_code = Column(String, unique=True, nullable=False, index=True)
    depth = Column(Integer, default=0)  # 0 = root, 1 = referred by root, etc.

    status = Column(SAEnum(AffiliateStatus), default=AffiliateStatus.PENDING)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    parent = relationship("Affiliate", remote_side=[id], backref="referrals")
    commissions = relationship("Commission", back_populates="affiliate")

    def get_upline(self, session: Session, max_depth: int = 10) -> list["Affiliate"]:
        """Walk up the tree and return ancestors (direct parent first)."""
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
    """
    A referral campaign with its own commission structure.
    Different products/plans can have different commission tiers.
    """
    __tablename__ = "campaigns"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    # Commission tiers: JSON array of {level: int, percentage: float}
    # Example: [{"level": 1, "percentage": 20.0}, {"level": 2, "percentage": 10.0}, ...]
    commission_tiers = Column(JSON, nullable=False)

    # How many levels deep commissions go (overrides global default)
    max_depth = Column(Integer, default=5)

    # Hold period before commissions become payable (in days)
    hold_days = Column(Integer, default=30)

    # Optional: link to a specific Stripe product/price
    stripe_product_id = Column(String, nullable=True)
    stripe_price_id = Column(String, nullable=True)

    # Optional: link to a GHL funnel or product
    ghl_product_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    sales = relationship("Sale", back_populates="campaign")


class Sale(Base):
    """
    A tracked sale/payment that triggers commission calculations.
    """
    __tablename__ = "sales"

    id = Column(String, primary_key=True, default=generate_uuid)

    # Who made the sale (the affiliate whose link was used)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)

    # Which campaign this sale belongs to
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False, index=True)

    # Stripe payment details
    stripe_payment_intent_id = Column(String, unique=True, nullable=True, index=True)
    stripe_charge_id = Column(String, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)

    # GHL details
    ghl_contact_id = Column(String, nullable=True)
    ghl_order_id = Column(String, nullable=True)

    # Money
    amount_cents = Column(Integer, nullable=False)  # Total sale amount in cents
    currency = Column(String, default="usd")

    is_recurring = Column(Boolean, default=False)
    is_refunded = Column(Boolean, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    affiliate = relationship("Affiliate")
    campaign = relationship("Campaign", back_populates="sales")
    commissions = relationship("Commission", back_populates="sale")


class Commission(Base):
    """
    A single commission entry for one affiliate on one sale.
    Multiple commissions are generated per sale (one per level in the tree).
    """
    __tablename__ = "commissions"

    id = Column(String, primary_key=True, default=generate_uuid)

    sale_id = Column(String, ForeignKey("sales.id"), nullable=False, index=True)
    affiliate_id = Column(String, ForeignKey("affiliates.id"), nullable=False, index=True)

    # Which level in the tree this commission is for
    level = Column(Integer, nullable=False)  # 1 = direct referrer, 2 = their referrer, etc.

    # Money
    percentage = Column(Float, nullable=False)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String, default="usd")

    status = Column(SAEnum(CommissionStatus), default=CommissionStatus.PENDING)

    # Stripe transfer details (filled when paid)
    stripe_transfer_id = Column(String, nullable=True, unique=True)
    paid_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    # When this commission becomes payable
    payable_after = Column(DateTime, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    sale = relationship("Sale", back_populates="commissions")
    affiliate = relationship("Affiliate", back_populates="commissions")

    __table_args__ = (
        UniqueConstraint("sale_id", "affiliate_id", name="uq_sale_affiliate"),
        Index("ix_commission_status_payable", "status", "payable_after"),
    )


class WebhookEvent(Base):
    """
    Log of processed webhook events for idempotency.
    Prevents double-processing if a webhook is delivered multiple times.
    """
    __tablename__ = "webhook_events"

    id = Column(String, primary_key=True)  # Stripe event ID or GHL event ID
    source = Column(String, nullable=False)  # "stripe" or "ghl"
    event_type = Column(String, nullable=False)
    processed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    payload_summary = Column(Text, nullable=True)
