"""
Stripe Connect Referral App - FastAPI Backend (Part 1)
Enhanced with commission tiers, audit logging, and advanced error handling.
"""

from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

import sentry_sdk
import stripe
import json
import hashlib
import hmac
from fastapi import FastAPI, Header, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from loguru import logger
import time

# Import database models
from database import (
    SessionLocal,
    init_db,
    Affiliate,
    Campaign,
    CommissionTier,
    AuditLog,
    DeadLetterQueue,
    WebhookEvent,
    Payout,
    Sale,
    Commission,
)
from settings import settings
from commission_engine import CommissionEngine

# Configure Sentry if DSN is provided
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.1,
        environment=settings.environment,
    )
    logger.info("Sentry initialized with DSN")

# Configure loguru for structured logging
logger.remove()  # Remove default handler
logger.add(
    "logs/app.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="INFO",
    rotation="500 MB",
    retention="10 days",
)
logger.add(
    lambda msg: print(msg, end=""),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    level="DEBUG",
)

logger.info("Stripe Connect Referral App v2.0.0 starting up")


# ======================
# PYDANTIC SCHEMAS
# ======================

class AffiliateCreate(BaseModel):
    """Schema for creating a new affiliate."""
    email: EmailStr
    name: str
    phone: Optional[str] = None
    parent_referral_code: Optional[str] = None
    ghl_contact_id: Optional[str] = None


class AffiliateResponse(BaseModel):
    """Schema for affiliate response."""
    id: str
    email: str
    name: str
    phone: Optional[str]
    parent_id: Optional[str]
    depth: int
    referral_code: str
    stripe_account_id: Optional[str]
    stripe_onboarding_complete: bool
    status: str
    ghl_contact_id: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class CampaignCreate(BaseModel):
    """Schema for creating a new campaign."""
    name: str
    description: Optional[str] = None
    commission_tiers: list[dict]  # [{"level": 1, "rate": 0.10}, ...]
    max_depth: int = 5
    hold_days: int = 14


class CampaignResponse(BaseModel):
    """Schema for campaign response."""
    id: str
    name: str
    description: Optional[str]
    commission_tiers: list[dict]
    max_depth: int
    hold_days: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class CommissionTierCreate(BaseModel):
    """Schema for creating a commission tier."""
    campaign_id: str
    level: int
    rate: float
    min_referrals_required: int = 0
    bonus_rate: float = 0.0


class CommissionTierResponse(BaseModel):
    """Schema for commission tier response."""
    id: str
    campaign_id: str
    level: int
    rate: float
    min_referrals_required: int
    bonus_rate: float
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class CommissionTierUpdate(BaseModel):
    """Schema for updating a commission tier."""
    rate: Optional[float] = None
    min_referrals_required: Optional[int] = None
    bonus_rate: Optional[float] = None
    is_active: Optional[bool] = None


# ======================
# DEPENDENCY INJECTION
# ======================

def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_api_key(x_api_key: str = Header(...)) -> str:
    """Verify API key from header."""
    if x_api_key != settings.app_secret_key:
        logger.warning(f"Invalid API key attempted: {x_api_key[:8]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return x_api_key


# ======================
# LIFESPAN CONTEXT MANAGER
# ======================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    logger.info("Application startup: Initializing commission engine and resources")
    # Startup logic
    yield
    # Shutdown logic
    logger.info("Application shutdown: Cleaning up resources")


# ======================
# FASTAPI APPLICATION
# ======================

app = FastAPI(
    title="Stripe Connect Referral System",
    description="Enhanced referral management with commission tiers and audit logging",
    version="2.0.0",
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ======================
# REQUEST TIMING MIDDLEWARE
# ======================

@app.middleware("http")
async def add_request_timing(request, call_next):
    """Middleware to time requests and log them."""
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time

    logger.info(
        f"{request.method} {request.url.path} - {response.status_code} - {duration:.3f}s",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_seconds": duration,
        },
    )

    response.headers["X-Process-Time"] = str(duration)
    return response


# ======================
# ROOT ENDPOINTS
# ======================

@app.get("/")
def root():
    """Root endpoint with API overview."""
    logger.debug("Root endpoint accessed")
    return {
        "app": "Stripe Connect Referral System",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/admin/dashboard",
        "endpoints": {
            "affiliates": "/affiliates",
            "campaigns": "/campaigns",
            "commission_tiers": "/commission-tiers",
            "webhooks_stripe": "/webhooks/stripe",
            "webhooks_ghl": "/webhooks/ghl",
            "admin_stats": "/admin/stats",
            "admin_payouts": "/admin/run-payouts",
            "admin_dashboard": "/admin/dashboard",
            "admin_dlq": "/admin/dead-letter-queue",
            "admin_audit": "/admin/audit-log",
        },
    }


@app.get("/health")
def health_check():
    """Health check endpoint."""
    logger.debug("Health check performed")
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0",
    }


# ======================
# AFFILIATE ENDPOINTS
# ======================

@app.post("/affiliates", response_model=AffiliateResponse, status_code=201)
def create_affiliate(
    affiliate_data: AffiliateCreate,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Create a new affiliate."""
    logger.info(f"Creating affiliate: {affiliate_data.email}")

    # Check if email already exists
    existing = db.query(Affiliate).filter(
        Affiliate.email == affiliate_data.email
    ).first()
    if existing:
        logger.warning(f"Affiliate creation failed: email {affiliate_data.email} already exists")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already exists",
        )

    # Find parent if referral code provided
    parent_id = None
    if affiliate_data.parent_referral_code:
        parent = db.query(Affiliate).filter(
            Affiliate.referral_code == affiliate_data.parent_referral_code
        ).first()
        if parent:
            parent_id = parent.id
            logger.info(f"Parent affiliate found: {parent_id}")
        else:
            logger.warning(f"Parent referral code not found: {affiliate_data.parent_referral_code}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent referral code not found",
            )

    # Create new affiliate
    affiliate = Affiliate(
        email=affiliate_data.email,
        name=affiliate_data.name,
        phone=affiliate_data.phone,
        parent_id=parent_id,
        ghl_contact_id=affiliate_data.ghl_contact_id,
    )

    db.add(affiliate)
    db.commit()
    db.refresh(affiliate)

    logger.info(f"Affiliate created successfully: {affiliate.id}")

    # Log audit event
    audit = AuditLog(
        entity_type="Affiliate",
        entity_id=affiliate.id,
        action="CREATE",
        changes={"email": affiliate_data.email, "name": affiliate_data.name},
    )
    db.add(audit)
    db.commit()

    return affiliate


@app.get("/affiliates", response_model=List[AffiliateResponse])
def list_affiliates(
    skip: int = 0,
    limit: int = 100,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """List all affiliates with pagination."""
    logger.info(f"Listing affiliates: skip={skip}, limit={limit}")

    affiliates = db.query(Affiliate).offset(skip).limit(limit).all()

    logger.info(f"Retrieved {len(affiliates)} affiliates")
    return affiliates


@app.get("/affiliates/{affiliate_id}", response_model=AffiliateResponse)
def get_affiliate(
    affiliate_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Get a single affiliate by ID."""
    logger.info(f"Fetching affiliate: {affiliate_id}")

    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        logger.warning(f"Affiliate not found: {affiliate_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Affiliate not found",
        )

    return affiliate


@app.get("/affiliates/{affiliate_id}/tree")
def get_referral_tree(
    affiliate_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Get referral tree for an affiliate."""
    logger.info(f"Fetching referral tree for affiliate: {affiliate_id}")

    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        logger.warning(f"Affiliate not found for tree: {affiliate_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Affiliate not found",
        )

    # Get all direct children
    children = db.query(Affiliate).filter(Affiliate.parent_id == affiliate_id).all()

    logger.info(f"Referral tree retrieved for {affiliate_id}: {len(children)} children")

    return {
        "affiliate_id": affiliate_id,
        "name": affiliate.name,
        "email": affiliate.email,
        "children_count": len(children),
        "children": [
            {
                "id": child.id,
                "name": child.name,
                "email": child.email,
                "depth": child.depth,
            }
            for child in children
        ],
    }


@app.get("/affiliates/{affiliate_id}/earnings")
def get_affiliate_earnings(
    affiliate_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Get earnings summary for an affiliate."""
    logger.info(f"Fetching earnings for affiliate: {affiliate_id}")

    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        logger.warning(f"Affiliate not found for earnings: {affiliate_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Affiliate not found",
        )

    # Placeholder for earnings calculation
    earnings = {
        "affiliate_id": affiliate_id,
        "total_earned": 0.0,
        "pending_payout": 0.0,
        "paid_out": 0.0,
        "referral_count": 0,
    }

    logger.info(f"Earnings retrieved for {affiliate_id}")
    return earnings


@app.post("/affiliates/{affiliate_id}/onboarding-link")
def create_onboarding_link(
    affiliate_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Create a Stripe onboarding link for an affiliate."""
    logger.info(f"Creating Stripe onboarding link for affiliate: {affiliate_id}")

    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        logger.warning(f"Affiliate not found for onboarding: {affiliate_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Affiliate not found",
        )

    # Placeholder for Stripe Connect account creation
    logger.info(f"Onboarding link generated for {affiliate_id}")

    return {
        "affiliate_id": affiliate_id,
        "onboarding_url": "https://connect.stripe.com/onboarding/...",
    }


# ======================
# CAMPAIGN ENDPOINTS
# ======================

@app.post("/campaigns", response_model=CampaignResponse, status_code=201)
def create_campaign(
    campaign_data: CampaignCreate,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Create a new campaign."""
    logger.info(f"Creating campaign: {campaign_data.name}")

    campaign = Campaign(
        name=campaign_data.name,
        description=campaign_data.description,
        max_depth=campaign_data.max_depth,
        hold_days=campaign_data.hold_days,
    )

    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    logger.info(f"Campaign created: {campaign.id}")

    # Create commission tiers for campaign
    for tier_data in campaign_data.commission_tiers:
        tier = CommissionTier(
            campaign_id=campaign.id,
            level=tier_data.get("level", 1),
            rate=tier_data.get("rate", 0.0),
            min_referrals_required=tier_data.get("min_referrals_required", 0),
            bonus_rate=tier_data.get("bonus_rate", 0.0),
        )
        db.add(tier)
        logger.info(f"Commission tier created for campaign {campaign.id}: level {tier.level}")

    db.commit()

    # Fetch tiers for response
    tiers = db.query(CommissionTier).filter(
        CommissionTier.campaign_id == campaign.id
    ).all()

    # Log audit event
    audit = AuditLog(
        entity_type="Campaign",
        entity_id=campaign.id,
        action="CREATE",
        changes={"name": campaign_data.name},
    )
    db.add(audit)
    db.commit()

    return {
        **campaign.__dict__,
        "commission_tiers": [tier.__dict__ for tier in tiers],
    }


@app.get("/campaigns", response_model=List[CampaignResponse])
def list_campaigns(
    skip: int = 0,
    limit: int = 100,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """List all campaigns with pagination."""
    logger.info(f"Listing campaigns: skip={skip}, limit={limit}")

    campaigns = db.query(Campaign).offset(skip).limit(limit).all()

    results = []
    for campaign in campaigns:
        tiers = db.query(CommissionTier).filter(
            CommissionTier.campaign_id == campaign.id
        ).all()
        results.append({
            **campaign.__dict__,
            "commission_tiers": [tier.__dict__ for tier in tiers],
        })

    logger.info(f"Retrieved {len(campaigns)} campaigns")
    return results


@app.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
def get_campaign(
    campaign_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Get a single campaign by ID."""
    logger.info(f"Fetching campaign: {campaign_id}")

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        logger.warning(f"Campaign not found: {campaign_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    tiers = db.query(CommissionTier).filter(
        CommissionTier.campaign_id == campaign_id
    ).all()

    return {
        **campaign.__dict__,
        "commission_tiers": [tier.__dict__ for tier in tiers],
    }


@app.put("/campaigns/{campaign_id}", response_model=CampaignResponse)
def update_campaign(
    campaign_id: str,
    campaign_data: CampaignCreate,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Update an existing campaign."""
    logger.info(f"Updating campaign: {campaign_id}")

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        logger.warning(f"Campaign not found for update: {campaign_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    # Update campaign fields
    campaign.name = campaign_data.name
    campaign.description = campaign_data.description
    campaign.max_depth = campaign_data.max_depth
    campaign.hold_days = campaign_data.hold_days

    db.commit()
    db.refresh(campaign)

    logger.info(f"Campaign updated: {campaign_id}")

    # Log audit event
    audit = AuditLog(
        entity_type="Campaign",
        entity_id=campaign_id,
        action="UPDATE",
        changes={"name": campaign_data.name},
    )
    db.add(audit)
    db.commit()

    tiers = db.query(CommissionTier).filter(
        CommissionTier.campaign_id == campaign_id
    ).all()

    return {
        **campaign.__dict__,
        "commission_tiers": [tier.__dict__ for tier in tiers],
    }


# ======================
# COMMISSION TIER ENDPOINTS
# ======================

@app.post("/commission-tiers", response_model=CommissionTierResponse, status_code=201)
def create_commission_tier(
    tier_data: CommissionTierCreate,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Create a new commission tier for a campaign."""
    logger.info(f"Creating commission tier for campaign: {tier_data.campaign_id}")

    # Verify campaign exists
    campaign = db.query(Campaign).filter(Campaign.id == tier_data.campaign_id).first()
    if not campaign:
        logger.warning(f"Campaign not found: {tier_data.campaign_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    # Check if tier level already exists
    existing = db.query(CommissionTier).filter(
        CommissionTier.campaign_id == tier_data.campaign_id,
        CommissionTier.level == tier_data.level,
        CommissionTier.is_active == True,
    ).first()
    if existing:
        logger.warning(f"Tier level {tier_data.level} already exists for campaign {tier_data.campaign_id}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tier level {tier_data.level} already exists",
        )

    tier = CommissionTier(
        campaign_id=tier_data.campaign_id,
        level=tier_data.level,
        rate=tier_data.rate,
        min_referrals_required=tier_data.min_referrals_required,
        bonus_rate=tier_data.bonus_rate,
    )

    db.add(tier)
    db.commit()
    db.refresh(tier)

    logger.info(f"Commission tier created: {tier.id}")

    # Log audit event
    audit = AuditLog(
        entity_type="CommissionTier",
        entity_id=tier.id,
        action="CREATE",
        changes={"level": tier_data.level, "rate": tier_data.rate},
    )
    db.add(audit)
    db.commit()

    return tier


@app.get("/commission-tiers/{campaign_id}", response_model=List[CommissionTierResponse])
def list_commission_tiers(
    campaign_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """List all commission tiers for a campaign."""
    logger.info(f"Listing commission tiers for campaign: {campaign_id}")

    # Verify campaign exists
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        logger.warning(f"Campaign not found: {campaign_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    tiers = db.query(CommissionTier).filter(
        CommissionTier.campaign_id == campaign_id
    ).order_by(CommissionTier.level).all()

    logger.info(f"Retrieved {len(tiers)} tiers for campaign {campaign_id}")
    return tiers


@app.put("/commission-tiers/{tier_id}", response_model=CommissionTierResponse)
def update_commission_tier(
    tier_id: str,
    tier_update: CommissionTierUpdate,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Update a commission tier."""
    logger.info(f"Updating commission tier: {tier_id}")

    tier = db.query(CommissionTier).filter(CommissionTier.id == tier_id).first()
    if not tier:
        logger.warning(f"Commission tier not found: {tier_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commission tier not found",
        )

    # Update fields if provided
    if tier_update.rate is not None:
        tier.rate = tier_update.rate
    if tier_update.min_referrals_required is not None:
        tier.min_referrals_required = tier_update.min_referrals_required
    if tier_update.bonus_rate is not None:
        tier.bonus_rate = tier_update.bonus_rate
    if tier_update.is_active is not None:
        tier.is_active = tier_update.is_active

    db.commit()
    db.refresh(tier)

    logger.info(f"Commission tier updated: {tier_id}")

    # Log audit event
    audit = AuditLog(
        entity_type="CommissionTier",
        entity_id=tier_id,
        action="UPDATE",
        changes=tier_update.dict(exclude_unset=True),
    )
    db.add(audit)
    db.commit()

    return tier


@app.delete("/commission-tiers/{tier_id}", status_code=204)
def delete_commission_tier(
    tier_id: str,
    db = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Deactivate a commission tier (soft delete)."""
    logger.info(f"Deactivating commission tier: {tier_id}")

    tier = db.query(CommissionTier).filter(CommissionTier.id == tier_id).first()
    if not tier:
        logger.warning(f"Commission tier not found for deletion: {tier_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Commission tier not found",
        )

    tier.is_active = False
    db.commit()

    logger.info(f"Commission tier deactivated: {tier_id}")

    # Log audit event
    audit = AuditLog(
        entity_type="CommissionTier",
        entity_id=tier_id,
        action="DELETE",
        changes={"is_active": False},
    )
    db.add(audit)
    db.commit()


# --- END PART 1 --- (continued in part 2)
# ===========================================================================
# WEBHOOK ENDPOINTS
# ===========================================================================

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle incoming Stripe webhook events.
    Verifies signature, records event, and processes payment/account updates.
    """
    try:
        body = await request.body()
        signature = request.headers.get("stripe-signature", "")

        # Verify webhook signature
        try:
            event = stripe.Webhook.construct_event(
                body, signature, settings.stripe_webhook_secret
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Invalid signature: {e}")
            raise HTTPException(status_code=400, detail="Invalid signature")

        # Record webhook event
        import time
        start_time = time.time()
        webhook_event = WebhookEvent(
            event_type=event.type,
            provider="stripe",
            event_id=event.id,
            raw_data=event,
            status="received"
        )
        db.add(webhook_event)
        db.commit()
        db.refresh(webhook_event)

        # Process based on event type
        if event.type == "payment_intent.succeeded":
            await _handle_payment_success(event.data.object, db)
            webhook_event.status = "processed"

        elif event.type == "charge.refunded":
            await _handle_refund(event.data.object, db)
            webhook_event.status = "processed"

        elif event.type == "account.updated":
            await _handle_account_updated(event.data.object, db)
            webhook_event.status = "processed"

        else:
            webhook_event.status = "ignored"

        # Record processing time
        processing_time_ms = int((time.time() - start_time) * 1000)
        webhook_event.processing_time_ms = processing_time_ms
        db.commit()

        logger.info(f"Stripe webhook {event.type} processed in {processing_time_ms}ms")
        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook processing error: {str(e)}", exc_info=True)

        # Create DLQ entry
        dlq_entry = DeadLetterQueue(
            event_type="stripe_webhook",
            provider="stripe",
            error_message=str(e),
            raw_data={"body": body.decode() if isinstance(body, bytes) else body}
        )
        db.add(dlq_entry)
        db.commit()

        raise HTTPException(status_code=500, detail="Webhook processing failed")


async def _handle_payment_success(payment_intent, db: Session):
    """Process a successful payment intent."""
    try:
        # Extract referral code from metadata
        referral_code = payment_intent.metadata.get("referral_code")
        if not referral_code:
            logger.warning("Payment intent missing referral_code in metadata")
            return

        # Find affiliate
        affiliate = db.query(Affiliate).filter(
            Affiliate.referral_code == referral_code
        ).first()
        if not affiliate:
            logger.warning(f"Affiliate not found for referral_code: {referral_code}")
            return

        # Create sale record
        sale = Sale(
            affiliate_id=affiliate.id,
            payment_intent_id=payment_intent.id,
            amount_cents=payment_intent.amount,
            currency=payment_intent.currency.upper(),
            customer_email=payment_intent.receipt_email or "",
            status="completed"
        )
        db.add(sale)
        db.flush()  # Get the sale ID

        # Use CommissionEngine to calculate and create commissions
        engine = CommissionEngine(db)
        commissions = engine.calculate_commissions(affiliate, sale.amount_cents)

        for commission in commissions:
            commission.sale_id = sale.id
            db.add(commission)

        # Update affiliate stats
        affiliate.total_sales += 1
        affiliate.total_revenue_cents += sale.amount_cents

        db.commit()
        logger.info(f"Created sale {sale.id} for affiliate {affiliate.id}: {sale.amount_cents} cents")

    except Exception as e:
        logger.error(f"Error handling payment success: {str(e)}", exc_info=True)
        db.rollback()
        raise


async def _handle_refund(charge, db: Session):
    """Process a refund."""
    try:
        payment_intent_id = charge.get("payment_intent")
        if not payment_intent_id:
            logger.warning("Refund charge missing payment_intent")
            return

        # Find sale
        sale = db.query(Sale).filter(
            Sale.payment_intent_id == payment_intent_id
        ).first()
        if not sale:
            logger.warning(f"Sale not found for payment_intent: {payment_intent_id}")
            return

        # Cancel all commissions for this sale
        commissions = db.query(Commission).filter(
            Commission.sale_id == sale.id,
            Commission.status != "cancelled"
        ).all()

        for commission in commissions:
            commission.status = "cancelled"
            commission.cancelled_at = datetime.utcnow()

        sale.status = "refunded"
        db.commit()

        logger.info(f"Refunded sale {sale.id} and {len(commissions)} commissions")

    except Exception as e:
        logger.error(f"Error handling refund: {str(e)}", exc_info=True)
        db.rollback()
        raise


async def _handle_account_updated(account, db: Session):
    """Process account update (Stripe Connect account)."""
    try:
        stripe_account_id = account.get("id")
        if not stripe_account_id:
            return

        # Find affiliate with this stripe account
        affiliate = db.query(Affiliate).filter(
            Affiliate.stripe_account_id == stripe_account_id
        ).first()
        if not affiliate:
            logger.warning(f"Affiliate not found for stripe account: {stripe_account_id}")
            return

        # Update onboarding status
        charges_enabled = account.get("charges_enabled", False)
        payouts_enabled = account.get("payouts_enabled", False)

        if charges_enabled and payouts_enabled:
            affiliate.stripe_onboarding_complete = True
        else:
            affiliate.stripe_onboarding_complete = False

        db.commit()
        logger.info(f"Updated affiliate {affiliate.id} onboarding status: {affiliate.stripe_onboarding_complete}")

    except Exception as e:
        logger.error(f"Error handling account update: {str(e)}", exc_info=True)
        db.rollback()
        raise


@app.post("/webhooks/ghl")
async def ghl_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle incoming GoHighLevel webhook events.
    """
    try:
        body = await request.body()
        data = await request.json()

        # Optional: Verify HMAC signature if configured
        if settings.ghl_webhook_secret:
            signature = request.headers.get("x-ghl-signature", "")
            # Implement HMAC verification here if needed

        # Record webhook event
        webhook_event = WebhookEvent(
            event_type=data.get("type", "unknown"),
            provider="ghl",
            event_id=data.get("id", ""),
            raw_data=data,
            status="received"
        )
        db.add(webhook_event)
        db.commit()

        # Extract GHL order data
        contact_id = data.get("contact_id")
        order_id = data.get("order_id")
        amount = data.get("amount")
        custom_fields = data.get("custom_fields", {})
        referral_code = custom_fields.get("referral_code")
        campaign_id = custom_fields.get("campaign_id")

        if not referral_code or not amount:
            logger.warning("GHL webhook missing required fields")
            webhook_event.status = "ignored"
            db.commit()
            return {"status": "ignored"}

        # Find affiliate
        affiliate = db.query(Affiliate).filter(
            Affiliate.referral_code == referral_code
        ).first()
        if not affiliate:
            logger.warning(f"Affiliate not found for referral_code: {referral_code}")
            webhook_event.status = "ignored"
            db.commit()
            return {"status": "ignored"}

        # Create sale from GHL order
        sale = Sale(
            affiliate_id=affiliate.id,
            payment_intent_id=order_id,
            amount_cents=int(float(amount) * 100),
            currency="USD",
            customer_email=data.get("customer_email", ""),
            status="completed"
        )
        db.add(sale)
        db.flush()

        # Calculate commissions
        engine = CommissionEngine(db)
        commissions = engine.calculate_commissions(affiliate, sale.amount_cents)

        for commission in commissions:
            commission.sale_id = sale.id
            db.add(commission)

        affiliate.total_sales += 1
        affiliate.total_revenue_cents += sale.amount_cents

        webhook_event.status = "processed"
        db.commit()

        logger.info(f"GHL webhook processed: sale {sale.id} for affiliate {affiliate.id}")
        return {"status": "processed"}

    except Exception as e:
        logger.error(f"GHL webhook error: {str(e)}", exc_info=True)

        dlq_entry = DeadLetterQueue(
            event_type="ghl_webhook",
            provider="ghl",
            error_message=str(e),
            raw_data={"body": body.decode() if isinstance(body, bytes) else body}
        )
        db.add(dlq_entry)
        db.commit()

        raise HTTPException(status_code=500, detail="Webhook processing failed")


# ===========================================================================
# ADMIN ENDPOINTS (all require API key auth)
# ===========================================================================

def verify_admin_api_key(api_key: str = Header(None)):
    """Verify admin API key."""
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    if api_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key


@app.get("/admin/stats")
async def admin_stats(
    api_key: str = Depends(verify_admin_api_key),
    db: Session = Depends(get_db)
):
    """Get enhanced stats for admin dashboard."""
    try:
        # Count affiliates by status
        total_affiliates = db.query(Affiliate).count()
        active_affiliates = db.query(Affiliate).filter(
            Affiliate.status == "active"
        ).count()
        pending_affiliates = db.query(Affiliate).filter(
            Affiliate.status == "pending"
        ).count()
        suspended_affiliates = db.query(Affiliate).filter(
            Affiliate.status == "suspended"
        ).count()

        # Sales stats
        total_sales = db.query(Sale).count()
        total_revenue_cents = db.query(func.sum(Sale.amount_cents)).scalar() or 0

        # Commission stats
        total_commissions = db.query(Commission).count()
        pending_commissions = db.query(Commission).filter(
            Commission.status == "pending"
        ).count()
        approved_commissions = db.query(Commission).filter(
            Commission.status == "approved"
        ).count()
        paid_commissions = db.query(Commission).filter(
            Commission.status == "paid"
        ).count()
        cancelled_commissions = db.query(Commission).filter(
            Commission.status == "cancelled"
        ).count()

        # Payout stats
        total_payouts = db.query(Payout).count()
        pending_payouts = db.query(Payout).filter(
            Payout.status == "pending"
        ).count()
        completed_payouts = db.query(Payout).filter(
            Payout.status == "completed"
        ).count()
        failed_payouts = db.query(Payout).filter(
            Payout.status == "failed"
        ).count()
        total_payout_amount = db.query(func.sum(Payout.amount_cents)).scalar() or 0

        # Recent webhook events
        recent_webhooks = db.query(WebhookEvent).order_by(
            WebhookEvent.created_at.desc()
        ).limit(20).all()

        # DLQ stats
        dlq_pending = db.query(DeadLetterQueue).filter(
            DeadLetterQueue.resolved_at == None
        ).count()

        # Recent errors
        recent_errors = db.query(DeadLetterQueue).order_by(
            DeadLetterQueue.created_at.desc()
        ).limit(10).all()

        return {
            "affiliates": {
                "total": total_affiliates,
                "active": active_affiliates,
                "pending": pending_affiliates,
                "suspended": suspended_affiliates
            },
            "sales": {
                "total": total_sales,
                "total_revenue_cents": total_revenue_cents
            },
            "commissions": {
                "total": total_commissions,
                "pending": pending_commissions,
                "approved": approved_commissions,
                "paid": paid_commissions,
                "cancelled": cancelled_commissions
            },
            "payouts": {
                "total": total_payouts,
                "pending": pending_payouts,
                "completed": completed_payouts,
                "failed": failed_payouts,
                "total_amount_cents": total_payout_amount
            },
            "webhook_events": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "provider": e.provider,
                    "status": e.status,
                    "created_at": e.created_at.isoformat()
                } for e in recent_webhooks
            ],
            "dlq_pending_count": dlq_pending,
            "recent_errors": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "error_message": e.error_message,
                    "created_at": e.created_at.isoformat()
                } for e in recent_errors
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching admin stats: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@app.post("/admin/run-payouts")
async def run_payouts(
    api_key: str = Depends(verify_admin_api_key),
    db: Session = Depends(get_db)
):
    """
    Run payouts for approved commissions past hold_until date.
    """
    try:
        summary = {
            "approved": 0,
            "paid": 0,
            "failed": 0,
            "skipped": 0,
            "total_amount_cents": 0
        }

        # Find APPROVED commissions past hold_until
        commissions = db.query(Commission).filter(
            Commission.status == "approved",
            Commission.hold_until <= datetime.utcnow()
        ).all()

        summary["approved"] = len(commissions)

        for commission in commissions:
            try:
                # Get affiliate
                affiliate = commission.affiliate
                if not affiliate.stripe_account_id:
                    logger.warning(f"Affiliate {affiliate.id} has no stripe account")
                    summary["skipped"] += 1
                    continue

                if not affiliate.stripe_onboarding_complete:
                    logger.warning(f"Affiliate {affiliate.id} onboarding incomplete")
                    summary["skipped"] += 1
                    continue

                # Create Stripe transfer
                transfer = stripe.Transfer.create(
                    amount=int(commission.amount_cents),
                    currency="usd",
                    destination=affiliate.stripe_account_id,
                    metadata={
                        "commission_id": commission.id,
                        "affiliate_id": affiliate.id
                    }
                )

                # Create payout record
                payout = Payout(
                    affiliate_id=affiliate.id,
                    commission_id=commission.id,
                    amount_cents=commission.amount_cents,
                    stripe_transfer_id=transfer.id,
                    status="completed"
                )
                db.add(payout)

                # Update commission
                commission.status = "paid"
                commission.paid_at = datetime.utcnow()

                summary["paid"] += 1
                summary["total_amount_cents"] += commission.amount_cents

                logger.info(f"Paid commission {commission.id} to affiliate {affiliate.id}")

            except Exception as e:
                logger.error(f"Failed to pay commission {commission.id}: {str(e)}")
                summary["failed"] += 1

        # Log to audit log
        audit = AuditLog(
            admin_id=1,  # Default admin
            action="run_payouts",
            details={
                "summary": summary
            }
        )
        db.add(audit)
        db.commit()

        logger.info(f"Payout run completed: {summary}")
        return summary

    except Exception as e:
        logger.error(f"Error running payouts: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Payout run failed")


@app.get("/admin/dead-letter-queue")
async def get_dlq(
    api_key: str = Depends(verify_admin_api_key),
    db: Session = Depends(get_db)
):
    """List dead letter queue entries (unresolved first)."""
    try:
        entries = db.query(DeadLetterQueue).order_by(
            DeadLetterQueue.resolved_at.is_(None).desc(),
            DeadLetterQueue.created_at.desc()
        ).all()

        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "provider": e.provider,
                "error_message": e.error_message,
                "created_at": e.created_at.isoformat(),
                "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
                "retry_count": e.retry_count
            } for e in entries
        ]
    except Exception as e:
        logger.error(f"Error fetching DLQ: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch DLQ")


@app.post("/admin/dead-letter-queue/{dlq_id}/retry")
async def retry_dlq_entry(
    dlq_id: int,
    api_key: str = Depends(verify_admin_api_key),
    db: Session = Depends(get_db)
):
    """Retry a dead letter queue entry."""
    try:
        entry = db.query(DeadLetterQueue).filter(
            DeadLetterQueue.id == dlq_id
        ).first()
        if not entry:
            raise HTTPException(status_code=404, detail="DLQ entry not found")

        entry.retry_count += 1
        entry.last_retry_at = datetime.utcnow()

        # Log retry
        audit = AuditLog(
            admin_id=1,
            action="retry_dlq_entry",
            details={"dlq_id": dlq_id, "retry_count": entry.retry_count}
        )
        db.add(audit)
        db.commit()

        logger.info(f"Retried DLQ entry {dlq_id} (attempt {entry.retry_count})")
        return {"status": "retried", "retry_count": entry.retry_count}

    except Exception as e:
        logger.error(f"Error retrying DLQ entry: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retry entry")


@app.post("/admin/dead-letter-queue/{dlq_id}/resolve")
async def resolve_dlq_entry(
    dlq_id: int,
    api_key: str = Depends(verify_admin_api_key),
    db: Session = Depends(get_db)
):
    """Mark a dead letter queue entry as resolved."""
    try:
        entry = db.query(DeadLetterQueue).filter(
            DeadLetterQueue.id == dlq_id
        ).first()
        if not entry:
            raise HTTPException(status_code=404, detail="DLQ entry not found")

        entry.resolved_at = datetime.utcnow()

        # Log resolution
        audit = AuditLog(
            admin_id=1,
            action="resolve_dlq_entry",
            details={"dlq_id": dlq_id}
        )
        db.add(audit)
        db.commit()

        logger.info(f"Resolved DLQ entry {dlq_id}")
        return {"status": "resolved"}

    except Exception as e:
        logger.error(f"Error resolving DLQ entry: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to resolve entry")


@app.get("/admin/audit-log")
async def get_audit_log(
    api_key: str = Depends(verify_admin_api_key),
    db: Session = Depends(get_db)
):
    """List recent audit log entries."""
    try:
        entries = db.query(AuditLog).order_by(
            AuditLog.created_at.desc()
        ).limit(100).all()

        return [
            {
                "id": e.id,
                "admin_id": e.admin_id,
                "action": e.action,
                "details": e.details,
                "created_at": e.created_at.isoformat()
            } for e in entries
        ]
    except Exception as e:
        logger.error(f"Error fetching audit log: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch audit log")


# ===========================================================================
# ADMIN DASHBOARD HTML
# ===========================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Dashboard - Stripe Connect Referral System</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #0f0f1e;
            color: #e0e0e0;
            line-height: 1.6;
        }

        .container {
            display: flex;
            height: 100vh;
            overflow: hidden;
        }

        /* Sidebar */
        .sidebar {
            width: 280px;
            background: #1a1a2e;
            border-right: 1px solid #2a2a3e;
            overflow-y: auto;
            padding: 20px;
            position: fixed;
            height: 100vh;
            left: 0;
            top: 0;
        }

        .sidebar-header {
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #2a2a3e;
        }

        .sidebar-title {
            font-size: 18px;
            font-weight: 700;
            color: #6c63ff;
            margin-bottom: 5px;
        }

        .sidebar-subtitle {
            font-size: 12px;
            color: #888;
        }

        .nav-section {
            margin-bottom: 25px;
        }

        .nav-section-title {
            font-size: 11px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
            padding-left: 10px;
        }

        .nav-item {
            padding: 12px 15px;
            margin-bottom: 5px;
            cursor: pointer;
            border-radius: 6px;
            color: #b0b0b0;
            transition: all 0.2s ease;
            font-size: 14px;
            border-left: 3px solid transparent;
        }

        .nav-item:hover {
            background: #252540;
            color: #e0e0e0;
            border-left-color: #6c63ff;
        }

        .nav-item.active {
            background: #6c63ff;
            color: white;
            border-left-color: #6c63ff;
        }

        /* Main content */
        .main {
            margin-left: 280px;
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        /* Header */
        .header {
            background: #1a1a2e;
            border-bottom: 1px solid #2a2a3e;
            padding: 20px 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header-title {
            font-size: 24px;
            font-weight: 700;
            color: #e0e0e0;
        }

        .api-key-input {
            padding: 8px 12px;
            background: #2a2a3e;
            border: 1px solid #3a3a4e;
            border-radius: 4px;
            color: #e0e0e0;
            font-size: 12px;
            width: 250px;
        }

        .api-key-input::placeholder {
            color: #666;
        }

        /* Content area */
        .content {
            flex: 1;
            overflow-y: auto;
            padding: 30px;
        }

        /* Tabs */
        .tab-content {
            display: none;
        }

        .tab-content.active {
            display: block;
        }

        /* KPI Cards */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .kpi-card {
            background: #1a1a2e;
            border: 1px solid #2a2a3e;
            border-radius: 8px;
            padding: 20px;
            transition: all 0.3s ease;
        }

        .kpi-card:hover {
            border-color: #6c63ff;
            transform: translateY(-4px);
        }

        .kpi-label {
            font-size: 12px;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }

        .kpi-value {
            font-size: 32px;
            font-weight: 700;
            color: #6c63ff;
            margin-bottom: 8px;
        }

        .kpi-subtext {
            font-size: 12px;
            color: #666;
        }

        /* Tables */
        .table-container {
            background: #1a1a2e;
            border: 1px solid #2a2a3e;
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 20px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th {
            background: #252540;
            padding: 15px;
            text-align: left;
            font-size: 12px;
            font-weight: 600;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid #2a2a3e;
        }

        td {
            padding: 15px;
            border-bottom: 1px solid #2a2a3e;
            font-size: 13px;
        }

        tr:hover {
            background: #252540;
        }

        /* Status badges */
        .badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .badge-success {
            background: #10b981;
            color: white;
        }

        .badge-warning {
            background: #f59e0b;
            color: white;
        }

        .badge-danger {
            background: #ef4444;
            color: white;
        }

        .badge-info {
            background: #6c63ff;
            color: white;
        }

        /* Buttons */
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .btn-primary {
            background: #6c63ff;
            color: white;
        }

        .btn-primary:hover {
            background: #5651d4;
            transform: translateY(-2px);
        }

        .btn-success {
            background: #10b981;
            color: white;
        }

        .btn-success:hover {
            background: #059669;
        }

        .btn-danger {
            background: #ef4444;
            color: white;
        }

        .btn-danger:hover {
            background: #dc2626;
        }

        .btn-sm {
            padding: 6px 12px;
            font-size: 12px;
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        /* Form */
        .form-group {
            margin-bottom: 20px;
        }

        .form-group label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: #b0b0b0;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 10px;
            background: #1a1a2e;
            border: 1px solid #2a2a3e;
            border-radius: 4px;
            color: #e0e0e0;
            font-size: 14px;
            font-family: inherit;
        }

        .form-group input:focus,
        .form-group select:focus,
        .form-group textarea:focus {
            outline: none;
            border-color: #6c63ff;
            box-shadow: 0 0 0 3px rgba(108, 99, 255, 0.1);
        }

        /* Loading spinner */
        .spinner {
            border: 3px solid #2a2a3e;
            border-top: 3px solid #6c63ff;
            border-radius: 50%;
            width: 24px;
            height: 24px;
            animation: spin 0.8s linear infinite;
            display: inline-block;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .loading {
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 40px;
            color: #888;
        }

        /* Mini bar chart */
        .chart-bar {
            display: flex;
            align-items: flex-end;
            gap: 8px;
            height: 120px;
            margin-top: 20px;
        }

        .bar {
            flex: 1;
            background: linear-gradient(180deg, #6c63ff, #5651d4);
            border-radius: 4px 4px 0 0;
            position: relative;
            min-height: 10px;
        }

        .bar-label {
            position: absolute;
            bottom: -20px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 11px;
            color: #666;
            white-space: nowrap;
        }

        /* Modal */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }

        .modal.active {
            display: flex;
        }

        .modal-content {
            background: #1a1a2e;
            border: 1px solid #2a2a3e;
            border-radius: 8px;
            padding: 30px;
            width: 90%;
            max-width: 500px;
            max-height: 90vh;
            overflow-y: auto;
        }

        .modal-header {
            font-size: 20px;
            font-weight: 700;
            margin-bottom: 20px;
            color: #e0e0e0;
        }

        .modal-close {
            position: absolute;
            top: 15px;
            right: 15px;
            font-size: 28px;
            cursor: pointer;
            color: #888;
        }

        .modal-close:hover {
            color: #e0e0e0;
        }

        /* Responsive */
        @media (max-width: 1024px) {
            .sidebar {
                width: 240px;
            }
            .main {
                margin-left: 240px;
            }
        }

        @media (max-width: 768px) {
            .container {
                flex-direction: column;
            }
            .sidebar {
                width: 100%;
                height: auto;
                position: relative;
                border-right: none;
                border-bottom: 1px solid #2a2a3e;
            }
            .main {
                margin-left: 0;
            }
            .kpi-grid {
                grid-template-columns: 1fr;
            }
            .api-key-input {
                width: 100%;
            }
        }

        /* Alerts */
        .alert {
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            font-size: 14px;
        }

        .alert-success {
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid #10b981;
            color: #10b981;
        }

        .alert-error {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid #ef4444;
            color: #ef4444;
        }

        /* Timeline */
        .timeline {
            position: relative;
            padding-left: 30px;
        }

        .timeline-item {
            margin-bottom: 20px;
            position: relative;
        }

        .timeline-item::before {
            content: '';
            position: absolute;
            left: -20px;
            top: 8px;
            width: 12px;
            height: 12px;
            background: #6c63ff;
            border-radius: 50%;
            border: 2px solid #1a1a2e;
        }

        .timeline-time {
            font-size: 11px;
            color: #666;
        }

        .timeline-action {
            font-size: 13px;
            color: #e0e0e0;
            margin-top: 4px;
        }

        .empty-state {
            text-align: center;
            padding: 40px;
            color: #666;
        }

        .empty-state-icon {
            font-size: 48px;
            margin-bottom: 15px;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Sidebar -->
        <div class="sidebar">
            <div class="sidebar-header">
                <div class="sidebar-title">Stripe Referral</div>
                <div class="sidebar-subtitle">Admin Panel v2.0.0</div>
            </div>

            <div class="nav-section">
                <div class="nav-section-title">Navigation</div>
                <div class="nav-item active" onclick="switchTab('overview')">📊 Overview</div>
                <div class="nav-item" onclick="switchTab('affiliates')">👥 Affiliates</div>
                <div class="nav-item" onclick="switchTab('campaigns')">🎯 Campaigns</div>
                <div class="nav-item" onclick="switchTab('tiers')">📈 Commission Tiers</div>
                <div class="nav-item" onclick="switchTab('payouts')">💰 Payouts</div>
            </div>

            <div class="nav-section">
                <div class="nav-section-title">System</div>
                <div class="nav-item" onclick="switchTab('webhooks')">🔔 Webhook Events</div>
                <div class="nav-item" onclick="switchTab('dlq')">⚠️ Dead Letter Queue</div>
                <div class="nav-item" onclick="switchTab('audit')">📋 Audit Log</div>
            </div>
        </div>

        <!-- Main content -->
        <div class="main">
            <!-- Header -->
            <div class="header">
                <div class="header-title" id="pageTitle">Overview</div>
                <input type="password" class="api-key-input" id="apiKeyInput" placeholder="Enter API Key..."
                       onchange="saveApiKey(this.value)">
            </div>

            <!-- Content -->
            <div class="content">
                <!-- Overview Tab -->
                <div class="tab-content active" id="overview">
                    <div id="overviewLoading" class="loading"><div class="spinner"></div></div>
                    <div id="overviewContent"></div>
                </div>

                <!-- Affiliates Tab -->
                <div class="tab-content" id="affiliates">
                    <div id="affiliatesLoading" class="loading"><div class="spinner"></div></div>
                    <div id="affiliatesContent"></div>
                </div>

                <!-- Campaigns Tab -->
                <div class="tab-content" id="campaigns">
                    <div id="campaignsLoading" class="loading"><div class="spinner"></div></div>
                    <div id="campaignsContent"></div>
                </div>

                <!-- Commission Tiers Tab -->
                <div class="tab-content" id="tiers">
                    <div id="tiersLoading" class="loading"><div class="spinner"></div></div>
                    <div id="tiersContent"></div>
                </div>

                <!-- Payouts Tab -->
                <div class="tab-content" id="payouts">
                    <div id="payoutsLoading" class="loading"><div class="spinner"></div></div>
                    <div id="payoutsContent"></div>
                </div>

                <!-- Webhooks Tab -->
                <div class="tab-content" id="webhooks">
                    <div id="webhooksLoading" class="loading"><div class="spinner"></div></div>
                    <div id="webhooksContent"></div>
                </div>

                <!-- DLQ Tab -->
                <div class="tab-content" id="dlq">
                    <div id="dlqLoading" class="loading"><div class="spinner"></div></div>
                    <div id="dlqContent"></div>
                </div>

                <!-- Audit Tab -->
                <div class="tab-content" id="audit">
                    <div id="auditLoading" class="loading"><div class="spinner"></div></div>
                    <div id="auditContent"></div>
                </div>
            </div>
        </div>
    </div>

    <script>
        // API Key management
        function getApiKey() {
            return localStorage.getItem('adminApiKey') || '';
        }

        function saveApiKey(key) {
            localStorage.setItem('adminApiKey', key);
        }

        function getHeaders() {
            return {
                'Content-Type': 'application/json',
                'X-API-Key': getApiKey()
            };
        }

        // Utility functions
        function formatCurrency(cents) {
            return '$' + (cents / 100).toFixed(2);
        }

        function formatDate(isoString) {
            return new Date(isoString).toLocaleDateString() + ' ' +
                   new Date(isoString).toLocaleTimeString();
        }

        function switchTab(tabName) {
            // Hide all tabs
            document.querySelectorAll('.tab-content').forEach(el => {
                el.classList.remove('active');
            });
            document.querySelectorAll('.nav-item').forEach(el => {
                el.classList.remove('active');
            });

            // Show selected tab
            document.getElementById(tabName).classList.add('active');
            event.target.classList.add('active');

            // Update page title
            const titles = {
                'overview': '📊 Overview',
                'affiliates': '👥 Affiliates',
                'campaigns': '🎯 Campaigns',
                'tiers': '📈 Commission Tiers',
                'payouts': '💰 Payouts',
                'webhooks': '🔔 Webhook Events',
                'dlq': '⚠️ Dead Letter Queue',
                'audit': '📋 Audit Log'
            };
            document.getElementById('pageTitle').textContent = titles[tabName] || tabName;

            // Load tab data
            loadTabData(tabName);
        }

        function showLoading(tabName) {
            const loading = document.getElementById(tabName + 'Loading');
            const content = document.getElementById(tabName + 'Content');
            if (loading) loading.style.display = 'flex';
            if (content) content.innerHTML = '';
        }

        function hideLoading(tabName) {
            const loading = document.getElementById(tabName + 'Loading');
            if (loading) loading.style.display = 'none';
        }

        // Load tab data
        async function loadTabData(tabName) {
            showLoading(tabName);
            try {
                switch(tabName) {
                    case 'overview':
                        await loadOverview();
                        break;
                    case 'affiliates':
                        await loadAffiliates();
                        break;
                    case 'campaigns':
                        await loadCampaigns();
                        break;
                    case 'tiers':
                        await loadTiers();
                        break;
                    case 'payouts':
                        await loadPayouts();
                        break;
                    case 'webhooks':
                        await loadWebhooks();
                        break;
                    case 'dlq':
                        await loadDLQ();
                        break;
                    case 'audit':
                        await loadAudit();
                        break;
                }
            } catch (error) {
                showError(tabName, error.message);
            } finally {
                hideLoading(tabName);
            }
        }

        // Overview tab
        async function loadOverview() {
            const resp = await fetch('/admin/stats', { headers: getHeaders() });
            const stats = await resp.json();

            let html = `
                <div class="kpi-grid">
                    <div class="kpi-card">
                        <div class="kpi-label">Total Affiliates</div>
                        <div class="kpi-value">${stats.affiliates.total}</div>
                        <div class="kpi-subtext">${stats.affiliates.active} active</div>
                    </div>
                    <div class="kpi-card">
                        <div class="kpi-label">Total Sales</div>
                        <div class="kpi-value">${stats.sales.total}</div>
                        <div class="kpi-subtext">${formatCurrency(stats.sales.total_revenue_cents)}</div>
                    </div>
                    <div class="kpi-card">
                        <div class="kpi-label">Commissions Pending</div>
                        <div class="kpi-value">${stats.commissions.pending}</div>
                        <div class="kpi-subtext">${stats.commissions.total} total</div>
                    </div>
                    <div class="kpi-card">
                        <div class="kpi-label">Commissions Paid</div>
                        <div class="kpi-value">${stats.commissions.paid}</div>
                        <div class="kpi-subtext">${formatCurrency(stats.payouts.total_amount_cents)}</div>
                    </div>
                    <div class="kpi-card">
                        <div class="kpi-label">DLQ Pending</div>
                        <div class="kpi-value">${stats.dlq_pending_count}</div>
                        <div class="kpi-subtext">Failed events</div>
                    </div>
                    <div class="kpi-card">
                        <div class="kpi-label">Payouts Completed</div>
                        <div class="kpi-value">${stats.payouts.completed}</div>
                        <div class="kpi-subtext">${stats.payouts.total} total</div>
                    </div>
                </div>

                <h3 style="margin-bottom: 20px; font-size: 18px;">Recent Webhook Events</h3>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Type</th>
                                <th>Provider</th>
                                <th>Status</th>
                                <th>Timestamp</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${stats.webhook_events.map(e => `
                                <tr>
                                    <td>${e.event_type}</td>
                                    <td>${e.provider}</td>
                                    <td><span class="badge badge-${e.status === 'processed' ? 'success' : e.status === 'received' ? 'info' : 'warning'}">${e.status}</span></td>
                                    <td>${formatDate(e.created_at)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>

                ${stats.recent_errors.length > 0 ? `
                    <h3 style="margin-bottom: 20px; font-size: 18px;">Recent Errors</h3>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>
                                    <th>Type</th>
                                    <th>Error</th>
                                    <th>Timestamp</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${stats.recent_errors.map(e => `
                                    <tr>
                                        <td>${e.event_type}</td>
                                        <td>${e.error_message}</td>
                                        <td>${formatDate(e.created_at)}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                ` : ''}
            `;

            document.getElementById('overviewContent').innerHTML = html;
        }

        // Affiliates tab
        async function loadAffiliates() {
            const resp = await fetch('/affiliates', { headers: getHeaders() });
            const affiliates = await resp.json();

            let html = `
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Email</th>
                                <th>Code</th>
                                <th>Status</th>
                                <th>Sales</th>
                                <th>Revenue</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${affiliates.map(a => `
                                <tr>
                                    <td>${a.name}</td>
                                    <td>${a.email}</td>
                                    <td><code>${a.referral_code}</code></td>
                                    <td><span class="badge badge-${a.status === 'active' ? 'success' : a.status === 'pending' ? 'warning' : 'danger'}">${a.status}</span></td>
                                    <td>${a.total_sales}</td>
                                    <td>${formatCurrency(a.total_revenue_cents)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;

            document.getElementById('affiliatesContent').innerHTML = html;
        }

        // Campaigns tab
        async function loadCampaigns() {
            const resp = await fetch('/campaigns', { headers: getHeaders() });
            const campaigns = await resp.json();

            let html = `
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Status</th>
                                <th>Commission Type</th>
                                <th>Created</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${campaigns.map(c => `
                                <tr>
                                    <td><strong>${c.name}</strong></td>
                                    <td><span class="badge badge-${c.status === 'active' ? 'success' : 'warning'}">${c.status}</span></td>
                                    <td>${c.commission_type}</td>
                                    <td>${formatDate(c.created_at)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;

            document.getElementById('campaignsContent').innerHTML = html;
        }

        // Commission Tiers tab
        async function loadTiers() {
            const resp = await fetch('/commission-tiers', { headers: getHeaders() });
            const tiers = await resp.json();

            let html = `
                <button class="btn btn-primary" onclick="showTierModal()">+ Add Tier</button>
                <br><br>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Campaign</th>
                                <th>Level</th>
                                <th>Rate</th>
                                <th>Min Referrals</th>
                                <th>Bonus Rate</th>
                                <th>Hold Days</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${tiers.map(t => `
                                <tr>
                                    <td>${t.campaign_id}</td>
                                    <td>${t.level}</td>
                                    <td>${(t.commission_rate_percent).toFixed(1)}%</td>
                                    <td>${t.min_referrals}</td>
                                    <td>${(t.bonus_rate_percent || 0).toFixed(1)}%</td>
                                    <td>${t.commission_hold_days}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;

            document.getElementById('tiersContent').innerHTML = html;
        }

        function showTierModal() {
            alert('Add tier modal - implement in tier modal');
        }

        // Payouts tab
        async function loadPayouts() {
            const resp = await fetch('/payouts', { headers: getHeaders() });
            const payouts = await resp.json();

            let html = `
                <button class="btn btn-success" onclick="runPayouts()">💸 Run Payouts</button>
                <div id="payoutResult"></div>
                <br>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Affiliate</th>
                                <th>Amount</th>
                                <th>Status</th>
                                <th>Date</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${payouts.map(p => `
                                <tr>
                                    <td>${p.affiliate_id}</td>
                                    <td>${formatCurrency(p.amount_cents)}</td>
                                    <td><span class="badge badge-${p.status === 'completed' ? 'success' : p.status === 'pending' ? 'warning' : 'danger'}">${p.status}</span></td>
                                    <td>${formatDate(p.created_at)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;

            document.getElementById('payoutsContent').innerHTML = html;
        }

        async function runPayouts() {
            if (!confirm('Run payouts now?')) return;

            try {
                const resp = await fetch('/admin/run-payouts', {
                    method: 'POST',
                    headers: getHeaders()
                });
                const result = await resp.json();

                let resultHtml = `
                    <div class="alert alert-success">
                        <strong>Payouts completed!</strong><br>
                        Paid: ${result.paid} | Failed: ${result.failed} | Skipped: ${result.skipped}<br>
                        Total: ${formatCurrency(result.total_amount_cents)}
                    </div>
                `;
                document.getElementById('payoutResult').innerHTML = resultHtml;

                setTimeout(() => loadPayouts(), 1000);
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        // Webhooks tab
        async function loadWebhooks() {
            const resp = await fetch('/admin/stats', { headers: getHeaders() });
            const stats = await resp.json();

            let html = `
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Type</th>
                                <th>Provider</th>
                                <th>Status</th>
                                <th>ID</th>
                                <th>Timestamp</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${stats.webhook_events.map(e => `
                                <tr>
                                    <td>${e.event_type}</td>
                                    <td>${e.provider}</td>
                                    <td><span class="badge badge-${e.status === 'processed' ? 'success' : e.status === 'received' ? 'info' : 'warning'}">${e.status}</span></td>
                                    <td><code>${e.id}</code></td>
                                    <td>${formatDate(e.created_at)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;

            document.getElementById('webhooksContent').innerHTML = html;
        }

        // DLQ tab
        async function loadDLQ() {
            const resp = await fetch('/admin/dead-letter-queue', { headers: getHeaders() });
            const entries = await resp.json();

            let html = entries.length === 0 ? '<div class="empty-state">No pending DLQ entries</div>' : `
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Type</th>
                                <th>Error</th>
                                <th>Retries</th>
                                <th>Timestamp</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${entries.map(e => `
                                <tr>
                                    <td>${e.event_type}</td>
                                    <td>${e.error_message}</td>
                                    <td>${e.retry_count}</td>
                                    <td>${formatDate(e.created_at)}</td>
                                    <td>
                                        <button class="btn btn-sm btn-primary" onclick="retryDLQ(${e.id})">Retry</button>
                                        <button class="btn btn-sm btn-danger" onclick="resolveDLQ(${e.id})">Resolve</button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;

            document.getElementById('dlqContent').innerHTML = html;
        }

        async function retryDLQ(id) {
            try {
                await fetch(\`/admin/dead-letter-queue/\${id}/retry\`, {
                    method: 'POST',
                    headers: getHeaders()
                });
                alert('Retry queued');
                loadDLQ();
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        async function resolveDLQ(id) {
            try {
                await fetch(\`/admin/dead-letter-queue/\${id}/resolve\`, {
                    method: 'POST',
                    headers: getHeaders()
                });
                alert('Marked as resolved');
                loadDLQ();
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        // Audit log tab
        async function loadAudit() {
            const resp = await fetch('/admin/audit-log', { headers: getHeaders() });
            const entries = await resp.json();

            let html = `
                <div class="timeline">
                    ${entries.map(e => `
                        <div class="timeline-item">
                            <div class="timeline-time">${formatDate(e.created_at)}</div>
                            <div class="timeline-action"><strong>${e.action}</strong></div>
                            <div style="font-size: 12px; color: #666; margin-top: 4px;">
                                Admin #${e.admin_id}
                            </div>
                        </div>
                    `).join('')}
                </div>
            `;

            document.getElementById('auditContent').innerHTML = html;
        }

        function showError(tabName, message) {
            const content = document.getElementById(tabName + 'Content');
            content.innerHTML = \`<div class="alert alert-error">Error: \${message}</div>\`;
        }

        // Initialize
        window.addEventListener('DOMContentLoaded', function() {
            const savedKey = getApiKey();
            if (savedKey) {
                document.getElementById('apiKeyInput').value = savedKey;
            }
            loadTabData('overview');
        });
    </script>
</body>
</html>
"""

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard():
    """Serve the admin dashboard HTML."""
    return HTMLResponse(content=DASHBOARD_HTML)


# ===========================================================================
# STARTUP EVENT
# ===========================================================================

@app.on_event("startup")
def on_startup():
    """Initialize database and log startup."""
    init_db()
    logger.info("Stripe Connect Referral System v2.0.0 started")
