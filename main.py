"""
Stripe Connect Referral App - FastAPI Backend (Part 1)
Enhanced with commission tiers, audit logging, and advanced error handling.
"""

from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Header, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from loguru import logger
import time

# Import database models
from database import (
    SessionLocal,
    Affiliate,
    Campaign,
    CommissionTier,
    AuditLog,
    DeadLetterQueue,
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
  (’‰âe³sion_tiers: list[dict]
    max_depth: int
    hold_days: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True