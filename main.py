"""
Stripe Connect Referral App — Main FastAPI Application.

Endpoints:
  /affiliates       — CRUD for affiliates + Stripe Connect onboarding
  /campaigns        — CRUD for referral campaigns with commission tiers
  /webhooks/stripe  — Stripe webhook handler (payments, refunds, account updates)
  /webhooks/ghl     — GoHighLevel webhook handler (new orders)
  /admin            — Dashboard data, payout triggers, referral tree views
"""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from config import get_settings
from database import get_db, init_db
from models import (
    Affiliate, AffiliateStatus, Campaign, Sale, Commission,
    CommissionStatus, WebhookEvent,
)
from commission_engine import commission_engine
from payout_service import payout_service

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

app = FastAPI(
    title="Stripe Connect Referral System",
    description="Multilevel referral program with Stripe Connect payouts and GHL integration",
    version="1.0.0",
)

# CORS — allow your frontend to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple API key auth for admin endpoints
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_admin(api_key: str = Security(api_key_header)):
    if not api_key or api_key != settings.app_secret_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return api_key


@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("Database initialized")


@app.get("/")
def root():
    """Root endpoint — API overview."""
    return {
        "app": "Stripe Connect Referral System",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "affiliates": "/affiliates",
            "campaigns": "/campaigns",
            "webhooks_stripe": "/webhooks/stripe",
            "webhooks_ghl": "/webhooks/ghl",
            "admin_stats": "/admin/stats",
            "admin_payouts": "/admin/run-payouts",
        },
    }


# ===========================================================================
# SCHEMAS
# ===========================================================================

class AffiliateCreate(BaseModel):
    email: EmailStr
    name: str
    phone: Optional[str] = None
    parent_referral_code: Optional[str] = None  # referral code of the person who referred them


class AffiliateResponse(BaseModel):
    id: str
    email: str
    name: str
    referral_code: str
    stripe_account_id: Optional[str]
    stripe_onboarding_complete: bool
    status: str
    depth: int
    parent_id: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class CampaignCreate(BaseModel):
    name: str
    description: Optional[str] = None
    commission_tiers: list[dict]  # [{"level": 1, "percentage": 20.0}, ...]
    max_depth: int = 5
    hold_days: int = 30
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    ghl_product_id: Optional[str] = None


class CampaignResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    commission_tiers: list[dict]
    max_depth: int
    hold_days: int
    is_active: bool
    stripe_product_id: Optional[str]
    ghl_product_id: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class PayoutRunResponse(BaseModel):
    approved: int
    paid: int
    failed: int
    skipped: int
    total_amount_cents: int


# ===========================================================================
# AFFILIATE ENDPOINTS
# ===========================================================================

@app.post("/affiliates", response_model=AffiliateResponse)
def create_affiliate(data: AffiliateCreate, db: Session = Depends(get_db)):
    """Register a new affiliate. Optionally link to a referrer via their referral code."""

    # Check for duplicate email
    existing = db.query(Affiliate).filter(Affiliate.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Resolve parent
    parent = None
    depth = 0
    if data.parent_referral_code:
        parent = (
            db.query(Affiliate)
            .filter(Affiliate.referral_code == data.parent_referral_code)
            .first()
        )
        if not parent:
            raise HTTPException(status_code=404, detail="Referral code not found")
        depth = parent.depth + 1

    affiliate = Affiliate(
        email=data.email,
        name=data.name,
        phone=data.phone,
        parent_id=parent.id if parent else None,
        depth=depth,
        referral_code=secrets.token_urlsafe(8),
        status=AffiliateStatus.PENDING,
    )
    db.add(affiliate)
    db.commit()
    db.refresh(affiliate)

    logger.info(f"Created affiliate: {affiliate.id} ({affiliate.email}), depth={depth}")
    return affiliate


@app.get("/affiliates", response_model=list[AffiliateResponse])
def list_affiliates(
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """List all affiliates, optionally filtered by status."""
    query = db.query(Affiliate)
    if status:
        query = query.filter(Affiliate.status == status)
    return query.offset(skip).limit(limit).all()


@app.get("/affiliates/{affiliate_id}", response_model=AffiliateResponse)
def get_affiliate(affiliate_id: str, db: Session = Depends(get_db)):
    affiliate = db.get(Affiliate, affiliate_id)
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate not found")
    return affiliate


@app.post("/affiliates/{affiliate_id}/onboarding-link")
def create_onboarding_link(affiliate_id: str, db: Session = Depends(get_db)):
    """
    Create a Stripe Connect Express account (if needed) and return an
    onboarding link the affiliate can visit to complete setup.
    """
    affiliate = db.get(Affiliate, affiliate_id)
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate not found")

    # Create Stripe Express account if not yet created
    if not affiliate.stripe_account_id:
        account = stripe.Account.create(
            type="express",
            email=affiliate.email,
            metadata={"affiliate_id": affiliate.id},
            capabilities={
                "transfers": {"requested": True},
            },
        )
        affiliate.stripe_account_id = account.id
        affiliate.status = AffiliateStatus.ONBOARDING
        db.commit()
        logger.info(f"Created Stripe account {account.id} for affiliate {affiliate.id}")

    # Generate onboarding link
    account_link = stripe.AccountLink.create(
        account=affiliate.stripe_account_id,
        refresh_url=f"{settings.app_base_url}/affiliates/{affiliate.id}/onboarding-link",
        return_url=f"{settings.frontend_url}/onboarding-complete?affiliate_id={affiliate.id}",
        type="account_onboarding",
    )

    return {"url": account_link.url, "expires_at": account_link.expires_at}


@app.get("/affiliates/{affiliate_id}/dashboard-link")
def get_dashboard_link(affiliate_id: str, db: Session = Depends(get_db)):
    """Get a Stripe Express dashboard login link for the affiliate."""
    affiliate = db.get(Affiliate, affiliate_id)
    if not affiliate or not affiliate.stripe_account_id:
        raise HTTPException(status_code=404, detail="Affiliate not found or not onboarded")

    login_link = stripe.Account.create_login_link(affiliate.stripe_account_id)
    return {"url": login_link.url}


@app.get("/affiliates/{affiliate_id}/referral-tree")
def get_referral_tree(affiliate_id: str, max_depth: int = 5, db: Session = Depends(get_db)):
    """Get the downline tree for an affiliate (who they referred, recursively)."""
    affiliate = db.get(Affiliate, affiliate_id)
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate not found")

    def build_tree(aff: Affiliate, current_depth: int) -> dict:
        if current_depth > max_depth:
            return None
        children = db.query(Affiliate).filter(Affiliate.parent_id == aff.id).all()
        return {
            "id": aff.id,
            "name": aff.name,
            "email": aff.email,
            "referral_code": aff.referral_code,
            "status": aff.status.value if aff.status else None,
            "depth": aff.depth,
            "children": [
                build_tree(child, current_depth + 1)
                for child in children
                if build_tree(child, current_depth + 1) is not None
            ],
        }

    return build_tree(affiliate, 0)


@app.get("/affiliates/{affiliate_id}/earnings")
def get_affiliate_earnings(affiliate_id: str, db: Session = Depends(get_db)):
    """Get earnings summary for an affiliate."""
    affiliate = db.get(Affiliate, affiliate_id)
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate not found")

    commissions = db.query(Commission).filter(Commission.affiliate_id == affiliate_id).all()

    total_earned = sum(c.amount_cents for c in commissions if c.status == CommissionStatus.PAID)
    total_pending = sum(c.amount_cents for c in commissions if c.status == CommissionStatus.PENDING)
    total_approved = sum(c.amount_cents for c in commissions if c.status == CommissionStatus.APPROVED)

    return {
        "affiliate_id": affiliate_id,
        "total_earned_cents": total_earned,
        "pending_cents": total_pending,
        "approved_cents": total_approved,
        "commission_count": len(commissions),
        "by_level": _group_by_level(commissions),
    }


def _group_by_level(commissions: list[Commission]) -> list[dict]:
    levels = {}
    for c in commissions:
        if c.level not in levels:
            levels[c.level] = {"level": c.level, "count": 0, "total_cents": 0}
        levels[c.level]["count"] += 1
        levels[c.level]["total_cents"] += c.amount_cents
    return sorted(levels.values(), key=lambda x: x["level"])


# ===========================================================================
# CAMPAIGN ENDPOINTS
# ===========================================================================

@app.post("/campaigns", response_model=CampaignResponse)
def create_campaign(data: CampaignCreate, db: Session = Depends(get_db)):
    """Create a new referral campaign with custom commission tiers."""
    # Validate tiers
    for tier in data.commission_tiers:
        if "level" not in tier or "percentage" not in tier:
            raise HTTPException(status_code=400, detail="Each tier must have 'level' and 'percentage'")
        if tier["percentage"] < 0 or tier["percentage"] > 100:
            raise HTTPException(status_code=400, detail="Percentage must be between 0 and 100")

    # Check total doesn't exceed 100%
    total_pct = sum(t["percentage"] for t in data.commission_tiers)
    if total_pct > 100:
        raise HTTPException(status_code=400, detail=f"Total commission ({total_pct}%) exceeds 100%")

    campaign = Campaign(
        name=data.name,
        description=data.description,
        commission_tiers=data.commission_tiers,
        max_depth=data.max_depth,
        hold_days=data.hold_days,
        stripe_product_id=data.stripe_product_id,
        stripe_price_id=data.stripe_price_id,
        ghl_product_id=data.ghl_product_id,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@app.get("/campaigns", response_model=list[CampaignResponse])
def list_campaigns(active_only: bool = True, db: Session = Depends(get_db)):
    query = db.query(Campaign)
    if active_only:
        query = query.filter(Campaign.is_active == True)
    return query.all()


@app.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
def get_campaign(campaign_id: str, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@app.put("/campaigns/{campaign_id}", response_model=CampaignResponse)
def update_campaign(campaign_id: str, data: CampaignCreate, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.name = data.name
    campaign.description = data.description
    campaign.commission_tiers = data.commission_tiers
    campaign.max_depth = data.max_depth
    campaign.hold_days = data.hold_days
    campaign.stripe_product_id = data.stripe_product_id
    campaign.stripe_price_id = data.stripe_price_id
    campaign.ghl_product_id = data.ghl_product_id
    db.commit()
    db.refresh(campaign)
    return campaign


# ===========================================================================
# STRIPE WEBHOOK
# ===========================================================================

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle Stripe webhook events:
    - payment_intent.succeeded → create sale + calculate commissions
    - charge.refunded → mark commissions as refunded
    - account.updated → update affiliate onboarding status
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as e:
        logger.error(f"Stripe webhook verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Idempotency check
    existing = db.get(WebhookEvent, event.id)
    if existing:
        logger.info(f"Duplicate webhook event: {event.id}")
        return {"status": "already_processed"}

    # Log the event
    db.add(WebhookEvent(
        id=event.id,
        source="stripe",
        event_type=event.type,
        payload_summary=event.type,
    ))

    # Route events
    if event.type == "payment_intent.succeeded":
        _handle_payment_success(event.data.object, db)

    elif event.type == "charge.refunded":
        _handle_refund(event.data.object, db)

    elif event.type == "account.updated":
        _handle_account_updated(event.data.object, db)

    db.commit()
    return {"status": "ok"}


def _handle_payment_success(payment_intent: dict, db: Session):
    """Process a successful payment — create sale and calculate commissions."""
    pi_id = payment_intent.get("id") if isinstance(payment_intent, dict) else payment_intent.id
    metadata = payment_intent.get("metadata", {}) if isinstance(payment_intent, dict) else (payment_intent.metadata or {})
    amount = payment_intent.get("amount") if isinstance(payment_intent, dict) else payment_intent.amount
    currency = payment_intent.get("currency", "usd") if isinstance(payment_intent, dict) else getattr(payment_intent, "currency", "usd")
    customer = payment_intent.get("customer") if isinstance(payment_intent, dict) else payment_intent.customer

    referral_code = metadata.get("referral_code")
    campaign_id = metadata.get("campaign_id")

    if not referral_code or not campaign_id:
        logger.info(f"Payment {pi_id} has no referral metadata, skipping")
        return

    # Check if sale already exists
    existing_sale = db.query(Sale).filter(Sale.stripe_payment_intent_id == pi_id).first()
    if existing_sale:
        return

    # Find the referring affiliate
    affiliate = db.query(Affiliate).filter(Affiliate.referral_code == referral_code).first()
    if not affiliate:
        logger.warning(f"Referral code '{referral_code}' not found for payment {pi_id}")
        return

    # Create the sale record
    sale = Sale(
        affiliate_id=affiliate.id,
        campaign_id=campaign_id,
        stripe_payment_intent_id=pi_id,
        stripe_customer_id=customer,
        amount_cents=amount,
        currency=currency,
    )
    db.add(sale)
    db.flush()

    # Calculate commissions across the tree
    commissions = commission_engine.calculate_commissions(db, sale)
    logger.info(f"Created {len(commissions)} commissions for sale {sale.id}")


def _handle_refund(charge: dict, db: Session):
    """Handle a refund — mark related commissions as refunded."""
    pi_id = charge.get("payment_intent") if isinstance(charge, dict) else charge.payment_intent

    if not pi_id:
        return

    sale = db.query(Sale).filter(Sale.stripe_payment_intent_id == pi_id).first()
    if sale:
        refunded = commission_engine.handle_refund(db, sale)
        logger.info(f"Refunded {len(refunded)} commissions for sale {sale.id}")


def _handle_account_updated(account: dict, db: Session):
    """Update affiliate status when their Stripe account changes."""
    account_id = account.get("id") if isinstance(account, dict) else account.id
    charges_enabled = account.get("charges_enabled") if isinstance(account, dict) else account.charges_enabled
    payouts_enabled = account.get("payouts_enabled") if isinstance(account, dict) else account.payouts_enabled

    affiliate = (
        db.query(Affiliate)
        .filter(Affiliate.stripe_account_id == account_id)
        .first()
    )
    if not affiliate:
        return

    if charges_enabled and payouts_enabled:
        affiliate.stripe_onboarding_complete = True
        affiliate.status = AffiliateStatus.ACTIVE
        logger.info(f"Affiliate {affiliate.id} onboarding complete")
    else:
        affiliate.stripe_onboarding_complete = False
        if affiliate.status == AffiliateStatus.ACTIVE:
            affiliate.status = AffiliateStatus.ONBOARDING


# ===========================================================================
# STRIPE CONNECT WEBHOOK (for connected account events)
# ===========================================================================

@app.post("/webhooks/stripe-connect")
async def stripe_connect_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle events from connected Stripe accounts."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_connect_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as e:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Reuse same handlers — the event structure is the same
    existing = db.get(WebhookEvent, event.id)
    if existing:
        return {"status": "already_processed"}

    db.add(WebhookEvent(id=event.id, source="stripe_connect", event_type=event.type))

    if event.type == "account.updated":
        _handle_account_updated(event.data.object, db)

    db.commit()
    return {"status": "ok"}


# ===========================================================================
# GOHIGHLEVEL WEBHOOK
# ===========================================================================

@app.post("/webhooks/ghl")
async def ghl_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle GoHighLevel webhook events.

    GHL sends webhooks when orders are created. We match them to affiliates
    via contact ID or custom fields, then create sales and commissions.

    Expected GHL payload fields:
    - contact_id: the GHL contact who made the purchase
    - order_id: GHL order reference
    - amount: order total (in dollars, we convert to cents)
    - custom_fields.referral_code: the affiliate's referral code
    - custom_fields.campaign_id: which campaign this belongs to
    - stripe_payment_intent_id: if the payment went through Stripe
    """
    payload = await request.json()

    # Optional: verify webhook signature if GHL provides one
    if settings.ghl_webhook_secret:
        sig = request.headers.get("x-ghl-signature", "")
        body = await request.body()
        expected = hmac.HMAC(
            settings.ghl_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=400, detail="Invalid GHL signature")

    event_type = payload.get("type", "order.created")
    logger.info(f"GHL webhook received: {event_type}")

    if event_type in ("order.created", "OrderCreate", "InvoicePaymentReceived"):
        _handle_ghl_order(payload, db)

    db.commit()
    return {"status": "ok"}


def _handle_ghl_order(payload: dict, db: Session):
    """Process a GHL order event and create a sale with commissions."""

    # Extract fields — adapt these to match your actual GHL webhook payload
    contact_id = payload.get("contact_id") or payload.get("contactId", "")
    order_id = payload.get("order_id") or payload.get("orderId", "")

    # Try to get referral info from custom fields
    custom_fields = payload.get("custom_fields", {})
    if not custom_fields:
        # GHL sometimes nests differently
        custom_fields = payload.get("customFields", {})

    referral_code = custom_fields.get("referral_code", "")
    campaign_id = custom_fields.get("campaign_id", "")

    # Try to get amount
    amount_dollars = payload.get("amount") or payload.get("totalAmount", 0)
    try:
        amount_cents = int(float(amount_dollars) * 100)
    except (ValueError, TypeError):
        amount_cents = 0

    if not referral_code or not campaign_id or amount_cents <= 0:
        logger.info(f"GHL order {order_id} missing referral data, skipping")
        return

    # Find affiliate
    affiliate = db.query(Affiliate).filter(Affiliate.referral_code == referral_code).first()
    if not affiliate:
        logger.warning(f"GHL order referral code '{referral_code}' not found")
        return

    # Prevent duplicates
    existing = db.query(Sale).filter(Sale.ghl_order_id == order_id).first()
    if existing:
        return

    stripe_pi = payload.get("stripe_payment_intent_id") or payload.get("stripePaymentIntentId")

    sale = Sale(
        affiliate_id=affiliate.id,
        campaign_id=campaign_id,
        ghl_contact_id=contact_id,
        ghl_order_id=order_id,
        stripe_payment_intent_id=stripe_pi,
        amount_cents=amount_cents,
        currency="usd",
    )
    db.add(sale)
    db.flush()

    commissions = commission_engine.calculate_commissions(db, sale)
    logger.info(f"GHL order {order_id}: created {len(commissions)} commissions")


# ===========================================================================
# ADMIN / DASHBOARD ENDPOINTS
# ===========================================================================

@app.post("/admin/run-payouts", response_model=PayoutRunResponse)
def run_payouts(db: Session = Depends(get_db), _auth=Depends(require_admin)):
    """
    Manually trigger a payout run.
    1. Approves pending commissions past their hold period
    2. Sends Stripe transfers for all approved commissions
    """
    approved_count = payout_service.approve_pending_commissions(db)
    results = payout_service.process_payouts(db)

    return PayoutRunResponse(
        approved=approved_count,
        paid=results["paid"],
        failed=results["failed"],
        skipped=results["skipped"],
        total_amount_cents=results["total_amount_cents"],
    )


@app.get("/admin/commissions")
def list_commissions(
    status: Optional[str] = None,
    affiliate_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    _auth=Depends(require_admin),
):
    """List commissions with optional filters."""
    query = db.query(Commission)
    if status:
        query = query.filter(Commission.status == status)
    if affiliate_id:
        query = query.filter(Commission.affiliate_id == affiliate_id)
    query = query.order_by(Commission.created_at.desc())
    return query.offset(skip).limit(limit).all()


@app.get("/admin/sales")
def list_sales(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    _auth=Depends(require_admin),
):
    """List all tracked sales."""
    return (
        db.query(Sale)
        .order_by(Sale.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@app.get("/admin/stats")
def get_stats(db: Session = Depends(get_db), _auth=Depends(require_admin)):
    """Get overall program statistics."""
    total_affiliates = db.query(Affiliate).count()
    active_affiliates = (
        db.query(Affiliate).filter(Affiliate.status == AffiliateStatus.ACTIVE).count()
    )
    total_sales = db.query(Sale).count()
    total_commissions_paid = (
        db.query(Commission)
        .filter(Commission.status == CommissionStatus.PAID)
        .count()
    )

    # Sum up money
    all_commissions = db.query(Commission).all()
    paid_amount = sum(c.amount_cents for c in all_commissions if c.status == CommissionStatus.PAID)
    pending_amount = sum(c.amount_cents for c in all_commissions if c.status == CommissionStatus.PENDING)

    return {
        "total_affiliates": total_affiliates,
        "active_affiliates": active_affiliates,
        "total_sales": total_sales,
        "total_commissions_paid": total_commissions_paid,
        "total_paid_cents": paid_amount,
        "total_pending_cents": pending_amount,
    }


# ===========================================================================
# HEALTH CHECK
# ===========================================================================

@app.get("/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ===========================================================================
# RUN
# ===========================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
