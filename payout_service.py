"""
Payout Service â Handles sending money to affiliates via Stripe Connect Transfers.

This runs on a schedule (e.g., daily cron) to process approved commissions.
"""

import logging
from datetime import datetime, timezone
import stripe
from sqlalchemy.orm import Session

from config import get_settings
from models import Affiliate, Commission, CommissionStatus

logger = logging.getLogger(__name__)
settings = get_settings()
stripe.api_key = settings.stripe_secret_key


class PayoutService:
    """Processes approved commissions and sends Stripe transfers."""

    def approve_pending_commissions(self, db: Session) -> int:
        """
        Move commissions past their hold period from PENDING to APPROVED.
        Returns the number of approved commissions.
        """
        now = datetime.now(timezone.utc)
        pending = (
            db.query(Commission)
            .filter(
                Commission.status == CommissionStatus.PENDING,
                Commission.payable_after <= now,
            )
            .all()
        )

        count = 0
        for commission in pending:
            # Verify the sale wasn't refunded
            if commission.sale and commission.sale.is_refunded:
                commission.status = CommissionStatus.REFUNDED
            else:
                commission.status = CommissionStatus.APPROVED
                count += 1

        db.commit()
        logger.info(f"Approved {count} commissions for payout")
        return count

    def process_payouts(self, db: Session) -> dict:
        """
        Send Stripe transfers for all APPROVED commissions.
        Returns summary of results.
        """
        approved = (
            db.query(Commission)
            .filter(Commission.status == CommissionStatus.APPROVED)
            .all()
        )

        results = {"paid": 0, "failed": 0, "skipped": 0, "total_amount_cents": 0}

        for commission in approved:
            affiliate = db.get(Affiliate, commission.affiliate_id)

            if not affiliate or not affiliate.stripe_account_id:
                commission.status = CommissionStatus.FAILED
                commission.error_message = "Affiliate has no Stripe account"
                results["skipped"] += 1
                continue

            try:
                transfer = stripe.Transfer.create(
                    amount=commission.amount_cents,
                    currency=commission.currency,
                    destination=affiliate.stripe_account_id,
                    metadata={
                        "commission_id": commission.id,
                        "sale_id": commission.sale_id,
                        "affiliate_id": commission.affiliate_id,
                        "level": str(commission.level),
                    },
                )

                commission.stripe_transfer_id = transfer.id
                commission.status = CommissionStatus.PAID
                commission.paid_at = datetime.now(timezone.utc)
                results["paid"] += 1
                results["total_amount_cents"] += commission.amount_cents

                logger.info(
                    f"Paid commission {commission.id}: "
                    f"${commission.amount_cents/100:.2f} -> {affiliate.stripe_account_id}"
                )

            except stripe.StripeError as e:
                commission.status = CommissionStatus.FAILED
                commission.error_message = str(e)
                results["failed"] += 1
                logger.error(f"Failed to pay commission {commission.id}: {e}")

        db.commit()
        logger.info(f"Payout run complete: {results}")
        return results


# Singleton
payout_service = PayoutService()
