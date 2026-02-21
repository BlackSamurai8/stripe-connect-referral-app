"""
Multilevel Commission Calculation Engine.

This is the heart of the referral system. When a sale occurs:
1. Identify the affiliate who made the sale
2. Walk up their referral tree
3. For each ancestor, calculate their commission based on the campaign tiers
4. Create Commission records with appropriate hold periods
"""

import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from models import (
    Affiliate, AffiliateStatus, Campaign, Sale, Commission, CommissionStatus
)

logger = logging.getLogger(__name__)


class CommissionEngine:
    """Calculates and distributes commissions across the referral tree."""

    def calculate_commissions(self, db: Session, sale: Sale) -> list[Commission]:
        """
        Calculate commissions for a sale across the entire upline.

        Returns a list of Commission objects (already added to the session).
        """
        campaign = db.get(Campaign, sale.campaign_id)
        if not campaign or not campaign.is_active:
            logger.warning(f"Campaign {sale.campaign_id} not found or inactive for sale {sale.id}")
            return []

        # Get the affiliate who made the sale
        affiliate = db.get(Affiliate, sale.affiliate_id)
        if not affiliate:
            logger.warning(f"Affiliate {sale.affiliate_id} not found for sale {sale.id}")
            return []

        # Build a lookup of commission percentages by level
        tier_map = {}
        for tier in campaign.commission_tiers:
            tier_map[tier["level"]] = tier["percentage"]

        max_depth = campaign.max_depth
        hold_until = datetime.now(timezone.utc) + timedelta(days=campaign.hold_days)

        # Walk up the referral tree starting from the direct referrer
        # Level 1 = the affiliate who made the sale (direct referrer)
        # Level 2 = the person who referred that affiliate, etc.
        commissions = []
        current = affiliate
        level = 1

        while current and level <= max_depth:
            percentage = tier_map.get(level)

            if percentage is None or percentage <= 0:
                # No commission at this level, but keep walking
                # (in case there's a commission at a higher level)
                current_parent_id = current.parent_id
                if current_parent_id:
                    current = db.get(Affiliate, current_parent_id)
                else:
                    current = None
                level += 1
                continue

            # Only pay active affiliates with completed Stripe onboarding
            if (current.status == AffiliateStatus.ACTIVE
                    and current.stripe_onboarding_complete
                    and current.stripe_account_id):

                amount_cents = int(sale.amount_cents * (percentage / 100.0))

                if amount_cents > 0:
                    commission = Commission(
                        sale_id=sale.id,
                        affiliate_id=current.id,
                        level=level,
                        percentage=percentage,
                        amount_cents=amount_cents,
                        currency=sale.currency,
                        status=CommissionStatus.PENDING,
                        payable_after=hold_until,
                    )
                    db.add(commission)
                    commissions.append(commission)

                    logger.info(
                        f"Commission created: affiliate={current.id}, "
                        f"level={level}, amount=${amount_cents/100:.2f}, "
                        f"sale={sale.id}"
                    )
            else:
                logger.info(
                    f"Skipping affiliate {current.id} at level {level} "
                    f"(status={current.status}, onboarding={current.stripe_onboarding_complete})"
                )

            # Move up the tree
            if current.parent_id:
                current = db.get(Affiliate, current.parent_id)
            else:
                current = None
            level += 1

        db.flush()
        return commissions

    def handle_refund(self, db: Session, sale: Sale) -> list[Commission]:
        """
        Mark all commissions for a sale as refunded.
        If already paid, we log it — you may want to handle clawbacks separately.
        """
        commissions = db.query(Commission).filter(Commission.sale_id == sale.id).all()
        refunded = []

        for commission in commissions:
            if commission.status == CommissionStatus.PAID:
                logger.warning(
                    f"Commission {commission.id} already paid — manual clawback may be needed"
                )
                # You could create a negative commission or flag for manual review
            commission.status = CommissionStatus.REFUNDED
            refunded.append(commission)

        sale.is_refunded = True
        db.flush()
        return refunded


# Singleton
commission_engine = CommissionEngine()
