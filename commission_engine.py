"""
Enhanced Commission Engine for Stripe Connect Referral App

This module calculates commissions for affiliates in a multi-level referral program.
It supports:
- Database-backed commission tiers (CommissionTier table)
- Fallback to campaign JSON configuration
- Tiered bonuses based on referral count thresholds
- Structured logging with contextual information
- Commission summaries for observability
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from sqlalchemy.orm import Session
from models import (
    Affiliate, AffiliateStatus, Campaign, Sale, Commission, CommissionStatus,
    CommissionTier
)

# Configure structured logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@dataclass
class CommissionRecord:
    """Represents a single commission record to be created."""
    affiliate_id: int
    level: int
    rate: float
    amount_cents: int
    hold_until: datetime


@dataclass
class CommissionSummary:
    """Summary of commission calculation results."""
    sale_id: int
    campaign_id: int
    total_commissions_count: int
    total_amount_cents: int
    max_depth_reached: int
    commissions: List[Dict]

    def to_dict(self) -> Dict:
        """Convert summary to dictionary for JSON serialization."""
        return {
            'sale_id': self.sale_id,
            'campaign_id': self.campaign_id,
            'total_commissions_count': self.total_commissions_count,
            'total_amount_cents': self.total_amount_cents,
            'max_depth_reached': self.max_depth_reached,
            'commissions': self.commissions,
        }


class CommissionCalculationError(Exception):
    """Raised when commission calculation fails."""
    pass


class CommissionEngine:
    """
    Calculates and creates commissions for sales in a multi-level referral program.

    The engine:
    1. Walks up the referral tree from the sale's affiliate
    2. Checks for active CommissionTier database records
    3. Falls back to campaign.commission_tiers JSON if no tiers exist
    4. Applies tiered bonuses based on referral count thresholds
    5. Respects campaign max_depth limit
    6. Returns structured commission data and summaries
    """

    def __init__(self, session: Session):
        """
        Initialize the commission engine.

        Args:
            session: SQLAlchemy database session
        """
        self.session = session

    def calculate_commissions(self, sale: Sale) -> Tuple[List[Commission], CommissionSummary]:
        """
        Calculate and create commissions for a sale.

        This is the main entry point. It:
        1. Validates the sale and campaign
        2. Determines commission rates (database or JSON)
        3. Walks the referral tree
        4. Creates commission records
        5. Returns commission list and summary

        Args:
            sale: The Sale object to calculate commissions for

        Returns:
            Tuple of (created_commissions, commission_summary)

        Raises:
            CommissionCalculationError: If sale validation fails
        """
        logger.info(
            "Starting commission calculation",
            extra={
                'sale_id': sale.id,
                'affiliate_id': sale.affiliate_id,
                'campaign_id': sale.campaign_id,
                'amount_cents': sale.amount_cents,
            }
        )

        # Validate sale and campaign
        self._validate_sale(sale)
        campaign = sale.campaign
        if not campaign or not campaign.is_active:
            raise CommissionCalculationError(
                f"Campaign {sale.campaign_id} is not active"
            )

        # Get commission rates from database or JSON
        rates_by_level = self._get_commission_rates(campaign)
        logger.info(
            "Commission rates determined",
            extra={
                'campaign_id': campaign.id,
                'source': 'database' if self._has_active_tiers(campaign) else 'json',
                'rates_count': len(rates_by_level),
            }
        )

        # Walk the referral tree and collect commission records
        commission_records = []
        current_affiliate = sale.affiliate
        current_level = 1

        while current_affiliate and current_level <= campaign.max_depth:
            if current_affiliate.parent_id is None:
                # Reached the top of the tree
                break

            parent_affiliate = current_affiliate.parent
            if not parent_affiliate:
                logger.warning(
                    "Parent affiliate not found but parent_id exists",
                    extra={
                        'sale_id': sale.id,
                        'current_affiliate_id': current_affiliate.id,
                        'parent_id': current_affiliate.parent_id,
                    }
                )
                break

            # Check if parent is active
            if parent_affiliate.status != AffiliateStatus.ACTIVE:
                logger.info(
                    "Skipping inactive parent affiliate",
                    extra={
                        'sale_id': sale.id,
                        'affiliate_id': parent_affiliate.id,
                        'status': parent_affiliate.status,
                        'level': current_level,
                    }
                )
                current_affiliate = parent_affiliate
                current_level += 1
                continue

            # Get rate for this level
            if current_level not in rates_by_level:
                logger.info(
                    "No commission rate for level",
                    extra={
                        'sale_id': sale.id,
                        'level': current_level,
                        'affiliate_id': parent_affiliate.id,
                    }
                )
                break

            rate_info = rates_by_level[current_level]
            base_rate = rate_info['rate']
            bonus_rate = rate_info.get('bonus_rate', 0.0)
            min_referrals = rate_info.get('min_referrals_required', 0)

            # Apply tiered bonus if applicable
            final_rate = base_rate
            bonus_applied = False
            if min_referrals > 0:
                referral_count = self._count_direct_referrals(parent_affiliate)
                if referral_count >= min_referrals:
                    final_rate = base_rate + bonus_rate
                    bonus_applied = True
                    logger.info(
                        "Bonus rate applied",
                        extra={
                            'sale_id': sale.id,
                            'affiliate_id': parent_affiliate.id,
                            'level': current_level,
                            'referral_count': referral_count,
                            'min_required': min_referrals,
                            'base_rate': base_rate,
                            'bonus_rate': bonus_rate,
                            'final_rate': final_rate,
                        }
                    )

            # Calculate commission amount
            commission_amount = int(sale.amount_cents * final_rate)

            # Calculate hold_until date
            hold_until = datetime.now(timezone.utc) + timedelta(days=campaign.hold_days)

            # Record this commission
            commission_record = CommissionRecord(
                affiliate_id=parent_affiliate.id,
                level=current_level,
                rate=final_rate,
                amount_cents=commission_amount,
                hold_until=hold_until,
            )
            commission_records.append(commission_record)

            logger.info(
                "Commission record created",
                extra={
                    'sale_id': sale.id,
                    'affiliate_id': parent_affiliate.id,
                    'level': current_level,
                    'rate': final_rate,
                    'amount_cents': commission_amount,
                    'bonus_applied': bonus_applied,
                }
            )

            # Move up the tree
            current_affiliate = parent_affiliate
            current_level += 1

        # Create Commission records in database
        created_commissions = self._create_commission_records(sale, commission_records)

        # Build summary
        total_amount = sum(c.amount_cents for c in created_commissions)
        summary = CommissionSummary(
            sale_id=sale.id,
            campaign_id=campaign.id,
            total_commissions_count=len(created_commissions),
            total_amount_cents=total_amount,
            max_depth_reached=current_level - 1,
            commissions=[
                {
                    'affiliate_id': c.affiliate_id,
                    'level': c.level,
                    'rate': c.rate,
                    'amount_cents': c.amount_cents,
                    'status': c.status,
                    'hold_until': c.hold_until.isoformat(),
                }
                for c in created_commissions
            ],
        )

        logger.info(
            "Commission calculation completed",
            extra={
                'sale_id': sale.id,
                'total_commissions': len(created_commissions),
                'total_amount_cents': total_amount,
                'max_depth_reached': current_level - 1,
            }
        )

        return created_commissions, summary

t_affiliate.id,
                    }
                )
                break

            # Check if parent is active
            if parent_affiliate.status != AffiliateStatus.ACTIVE:
                logger.info(
                    "Skipping inactive parent affiliate",
                    extra={
                        'sale_id': sale.id,
                        'affiliate_id': parent_affiliate.id,
                        'status': parent_affiliate.status,
                        'level': current_level,
                    }
                )
                current_affiliate = parent_affiliate
                current_level += 1
                continue

            # Get rate for this level
            if current_level not in rates_by_level:
                logger.info(
                    "No commission rate for level",
                    extra={
                        'sale_id': sale.id,
                        'level': current_level,
                        'affiliate_id': parent_affiliate.id,
                    }
                )
                break

            rate_info = rates_by_level[current_level]
            base_rate = rate_info['rate']
            bonus_rate = rate_info.get('bonus_rate', 0.0)
            min_referrals = rate_info.get('min_referrals_required', 0)

            # Apply tiered bonus if applicable
            final_rate = base_rate
            bonus_applied = False
            if min_referrals > 0:
                referral_count = self._count_direct_referrals(parent_affiliate)
                if referral_count >= min_referrals:
                    final_rate = base_rate + bonus_rate
                    bonus_applied = True
                    logger.info(
                        "Bonus rate applied",
                        extra={
                            'sale_id': sale.id,
                            'affiliate_id': parent_affiliate.id,
                            'level': current_level,
                            'referral_count': referral_count,
                            'min_required': min_referrals,
                            'base_rate': base_rate,
                            'bonus_rate': bonus_rate,
                            'final_rate': final_rate,
                        }
                    )

            # Calculate commission amount
            commission_amount = int(sale.amount_cents * final_rate)

            # Calculate hold_until date
            hold_until = datetime.now(timezone.utc) + timedelta(days=campaign.hold_days)

            # Record this commission
            commission_record = CommissionRecord(
                affiliate_id=parent_affiliate.id,
                level=current_level,
                rate=final_rate,
                amount_cents=commission_amount,
                hold_until=hold_until,
            )
            commission_records.append(commission_record)

            logger.info(
                "Commission record created",
                extra={
                    'sale_id': sale.id,
                    'affiliate_id': parent_affiliate.id,
                    'level': current_level,
                    'rate': final_rate,
                    'amount_cents': commission_amount,
                    'bonus_applied': bonus_applied,
                }
            )

            # Move up the tree
            current_affiliate = parent_affiliate
            current_level += 1

        # Create Commission records in database
        created_commissions = self._create_commission_records(sale, commission_records)

        # Build summary
        total_amount = sum(c.amount_cents for c in created_commissions)
        summary = CommissionSummary(
            sale_id=sale.id,
            campaign_id=campaign.id,
            total_commissions_count=len(created_commissions),
            total_amount_cents=total_amount,
            max_depth_reached=current_level - 1,
            commissions=[
                {
                    'affiliate_id': c.affiliate_id,
                    'level': c.level,
                    'rate': c.rate,
                    'amount_cents': c.amount_cents,
                    'status': c.status,
                    'hold_until': c.hold_until.isoformat(),
                }
                for c in created_commissions
            ],
        )

        logger.info(
            "Commission calculation completed",
            extra={
                'sale_id': sale.id,
                'total_commissions': len(created_commissions),
                'total_amount_cents': total_amount,
                'max_depth_reached': current_level - 1,
            }
        )

        return created_commissions, summary

t_sale(self, sale: Sale) -> None:
        """
        Validate that a sale is eligible for commission calculation.

        Args:
            sale: The Sale to validate

        Raises:
            CommissionCalculationError: If sale is invalid
        """
        if not sale:
            raise CommissionCalculationError("sale is None")

        if not sale.affiliate_id:
            raise CommissionCalculationError(
                f"Sale {sale.id} has no affiliate_id"
            )

        if sale.amount_cents <= 0:
            raise CommissionCalculationError(
                f"Sale {sale.id} has invalid amount: {sale.amount_cents}"
            )

        if not sale.campaign_id:
            raise CommissionCalculationError(
                f"Sale {sale.id} has no campaign_id"
            )

        affiliate = sale.affiliate
        if not affiliate:
            raise CommissionCalculationError(
                f"Affiliate {sale.affiliate_id} not found for sale {sale.id}"
            )

    def _has_active_tiers(self, campaign: Campaign) -> bool:
       """
        Check if campaign has active CommissionTier records.

        Args:
            campaign: The Campaign to check

        Returns:
            True if active tiers exist, False otherwise
        """
        if not campaign.tiers:
            return False

        active_tiers = [tier for tier in campaign.tiers if tier.is_active]
        return len(active_tiers) > 0

    def _get_commission_rates(self, campaign: Campaign) -> Dict[int, Dict]:
        """
        Get commission rates for a campaign.

        Prioritizes database CommissionTier records over campaign.commission_tiers JSON.
        Returns a dict mapping level -> {rate, bonus_rate, min_referrals_required}.

        Args:            campaign: The Campaign to get rates for

        Returns:
            Dict mapping level (int) to rate info dict
        """
        rates = {}

        # Try to get rates from CommissionTier table
        if self._has_active_tiers(campaign):
            logger.debug(
                "Loading commission rates from database",
                extra={'campaign_id': campaign.id}
            )
            for tier in campaign.tiers:
                if tier.is_active:
                    rates[tier.level] = {
                        'rate': tier.rate,
                        'bonus_rate': tier.bonus_rate or 0.0,
                        'min_referrals_required': tier.min_referrals_required or 0,
                    }
            return rates

        # Fall back to campaign.commission_tiers JSON
        logger.debug(
            "No active CommissionTier records, using JSON fallback",
            extra={'campaign_id': campaign.id}
        )
        if campaign.commission_tiers and isinstance(campaign.commission_tiers, list):
            for tier_data in campaign.commission_tiers:
                level = tier_data.get('level')
                rate = tier_data.get('rate')
                if level is not None and rate is not None:
                    rates[level] = {
                        'rate': float(rate),
                        'bonus_rate': 0.0,
                        'min_referrals_required': 0,
                    }

        if not rates:
            logger.warning(
                "No commission rates found",
                extra={'campaign_id': campaign.id}
            )

        return rates

    def _count_direct_referrals(self, affiliate: Affiliate) -> int:
        """
        Count the number of direct referrals (children) for an affiliate.

        This is used to determine if tiered bonuses should be applied.

        Args:
            affiliate: The Affiliate to count referrals for

        Returns:
            Number of direct referrals (children)
        """
        if not affiliate or not affiliate.children:
            return 0

        # Count active children only
        active_children = [
            child for child in affiliate.children
            if child.status == AffiliateStatus.ACTIVE
        ]
        return len(active_children)

    def _create_commission_records(
        self, sale: Sale, commission_records: List[CommissionRecord]
    ) -> List[Commission]:
        """
        Create Commission records in the database.

        Args:
            sale: The Sale these commissions are for
            commission_records: List of CommissionRecord objects

        Returns:
            List of created Commission objects

        Raises:
            CommissionCalculationError: If database operation fails
        """
        created = []

        try:
            for record in commission_records:
                commission = Commission(
                    sale_id=sale.id,
                    affiliate_id=record.affiliate_id,
                    level=record.level,
                    rate=record.rate,
                    amount_cents=record.amount_cents,
                    status=CommissionStatus.PENDING,
                    hold_until=record.hold_until,
                )
                self.session.add(commission)
                created.append(commission)

            self.session.commit()
            logger.info(
                "Commission records committed to database",
                extra={
                    'sale_id': sale.id,
                    'count': len(created),
                }
            )
        except Exception as e:
            self.session.rollback()
            logger.error(
                "Failed to create commission records",
                extra={
                    'sale_id': sale.id,
                    'error': str(e),
                },
                exc_info=True,
            )
            raise CommissionCalculationError(
                f"Failed to create commission records: {str(e)}"
            ) from e

        return created


def process_sale(session: Session, sale: Sale) -> Tuple[List[Commission], CommissionSummary]:
    """
    Process a sale and calculate commissions.

    This is a convenience function that creates an engine and processes a sale.

    Args:
        session: SQLAlchemy database session
        sale: The Sale to process

    Returns:
        Tuple of (created_commissions, commission_summary)

    Raises:
        CommissionCalculationError: If processing fails
    """
    engine = CommissionEngine(session)
    return engine.calculate_commissions(sale)
