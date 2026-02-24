"""
GoHighLevel (GHL) notification service.

Pushes payout/commission data back to GHL contacts so affiliates
can see their earnings in the GHL dashboard/portal.

Custom fields required in GHL:
  - referral_total_earned    (currency / text)
  - referral_last_payout     (currency / text)
  - referral_last_payout_date (text / date)
  - referral_pending_balance  (currency / text)
  - referral_status           (text) -> "Active", "Pending", etc.
"""

import httpx
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

GHL_API_BASE = "https://services.leadconnectorhq.com"


class GHLService:
    """Handles all communication with the GoHighLevel API."""

    def __init__(self, api_key: str, location_id: str = ""):
        self.api_key = api_key
        self.location_id = location_id
        self.enabled = api_key and api_key not in ("not_needed_yet", "skip", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Version": "2021-07-28",
        }

    async def update_contact_earnings(
        self,
        ghl_contact_id: str,
        total_earned_cents: int,
        last_payout_cents: int,
        last_payout_date: Optional[datetime] = None,
        pending_balance_cents: int = 0,
        affiliate_status: str = "Active",
    ) -> bool:
        """
        Update a GHL contact's custom fields with their earnings data.

        Args:
            ghl_contact_id: The GHL contact ID for this affiliate
            total_earned_cents: Lifetime total paid out in cents
            last_payout_cents: Most recent payout amount in cents
            last_payout_date: When the last payout was sent
            pending_balance_cents: Amount pending/approved but not yet paid
            affiliate_status: Current affiliate status string

        Returns:
            True if update succeeded, False otherwise
        """
        if not self.enabled:
            logger.info("GHL service disabled - skipping contact update")
            return False

        if not ghl_contact_id:
            logger.warning("No GHL contact ID provided - skipping update")
            return False

        # Format values for GHL custom fields
        total_earned = f"${total_earned_cents / 100:.2f}"
        last_payout = f"${last_payout_cents / 100:.2f}"
        pending_balance = f"${pending_balance_cents / 100:.2f}"
        payout_date = ""
        if last_payout_date:
            payout_date = last_payout_date.strftime("%Y-%m-%d %H:%M UTC")

        payload = {
            "customFields": [
                {"key": "referral_total_earned", "field_value": total_earned},
                {"key": "referral_last_payout", "field_value": last_payout},
                {"key": "referral_last_payout_date", "field_value": payout_date},
                {"key": "referral_pending_balance", "field_value": pending_balance},
                {"key": "referral_status", "field_value": affiliate_status},
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(
                    f"{GHL_API_BASE}/contacts/{ghl_contact_id}",
                    headers=self._headers(),
                    json=payload,
                )

                if resp.status_code == 200:
                    logger.info(
                        f"GHL contact {ghl_contact_id} updated: "
                        f"total={total_earned}, last={last_payout}"
                    )
                    return True
                else:
                    logger.error(
                        f"GHL update failed for {ghl_contact_id}: "
                        f"{resp.status_code} - {resp.text}"
                    )
                    return False

        except Exception as e:
            logger.error(f"GHL API error for contact {ghl_contact_id}: {e}")
            return False

    async def create_contact(
        self,
        email: str,
        name: str,
        phone: str = "",
        referral_code: str = "",
        tags: list = None,
    ) -> Optional[str]:
        """
        Create a contact in GHL and return the contact ID.
        Useful for auto-syncing affiliates to GHL.

        Returns:
            GHL contact ID if created, None on failure
        """
        if not self.enabled:
            logger.info("GHL service disabled - skipping contact creation")
            return None

        # Split name into first/last
        parts = name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        payload = {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "source": "Referral System",
            "tags": tags or ["affiliate"],
        }

        if phone:
            payload["phone"] = phone

        if self.location_id:
            payload["locationId"] = self.location_id

        if referral_code:
            payload["customFields"] = [
                {"key": "referral_code", "field_value": referral_code},
                {"key": "referral_status", "field_value": "Pending"},
                {"key": "referral_total_earned", "field_value": "$0.00"},
                {"key": "referral_pending_balance", "field_value": "$0.00"},
            ]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{GHL_API_BASE}/contacts/",
                    headers=self._headers(),
                    json=payload,
                )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    contact_id = data.get("contact", {}).get("id")
                    logger.info(f"GHL contact created: {contact_id} for {email}")
                    return contact_id
                else:
                    logger.error(
                        f"GHL contact creation failed for {email}: "
                        f"{resp.status_code} - {resp.text}"
                    )
                    return None

        except Exception as e:
            logger.error(f"GHL API error creating contact {email}: {e}")
            return None

    async def add_note(
        self,
        ghl_contact_id: str,
        body: str,
    ) -> bool:
        """
        Add a note to a GHL contact (useful for payout receipts).
        """
        if not self.enabled or not ghl_contact_id:
            return False

        payload = {"body": body}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{GHL_API_BASE}/contacts/{ghl_contact_id}/notes",
                    headers=self._headers(),
                    json=payload,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"GHL note added to contact {ghl_contact_id}")
                    return True
                else:
                    logger.error(
                        f"GHL note failed for {ghl_contact_id}: "
                        f"{resp.status_code} - {resp.text}"
                    )
                    return False

        except Exception as e:
            logger.error(f"GHL note error for {ghl_contact_id}: {e}")
            return False
