"""
Demo script â Shows the referral system working end-to-end
with a simulated 5-level MLM tree and commission calculations.

No Stripe keys needed â this runs entirely locally.
"""

import sys
import os

# Patch settings before any imports that use config
os.environ.update({
    "STRIPE_SECRET_KEY": "sk_test_demo",
    "STRIPE_PUBLISHABLE_KEY": "pk_test_demo",
    "STRIPE_WEBHOOK_SECRET": "whsec_demo",
    "STRIPE_CONNECT_WEBHOOK_SECRET": "whsec_demo",
    "GHL_API_KEY": "demo",
    "DATABASE_URL": "sqlite:///./demo.db",
})

from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from models import (
    Base, Affiliate, AffiliateStatus, Campaign, Sale, Commission, CommissionStatus
)
from commission_engine import commission_engine
import secrets

# ââ Setup ââââââââââââââââââââââââââââââââââââââââââââââââââ
engine = create_engine("sqlite:////tmp/demo_referral.db")
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)
db = SessionLocal()

print("=" * 65)
print("  STRIPE CONNECT REFERRAL SYSTEM â LIVE DEMO")
print("=" * 65)

# ââ Step 1: Create a 5-level referral tree âââââââââââââââââ
print("\nð STEP 1: Building a 5-level referral tree\n")

affiliates = []
names = [
    ("Alice Johnson", "alice@example.com"),     # Level 0 (root)
    ("Bob Smith", "bob@example.com"),            # Level 1 (referred by Alice)
    ("Carol Williams", "carol@example.com"),     # Level 2 (referred by Bob)
    ("David Brown", "david@example.com"),        # Level 3 (referred by Carol)
    ("Eva Martinez", "eva@example.com"),         # Level 4 (referred by David)
    ("Frank Lee", "frank@example.com"),          # Level 5 (referred by Eva)
]

for i, (name, email) in enumerate(names):
    aff = Affiliate(
        email=email,
        name=name,
        referral_code=secrets.token_urlsafe(8),
        parent_id=affiliates[i - 1].id if i > 0 else None,
        depth=i,
        status=AffiliateStatus.ACTIVE,
        stripe_account_id=f"acct_demo_{name.split()[0].lower()}",
        stripe_onboarding_complete=True,
    )
    db.add(aff)
    db.flush()
    affiliates.append(aff)

    indent = "  " * i
    arrow = " <ââ referred by " + affiliates[i - 1].name if i > 0 else " (ROOT)"
    print(f"  {indent}Level {i}: {name}{arrow}")

db.commit()

# ââ Step 2: Create a campaign with 5-level commissions âââââ
print("\nð STEP 2: Creating campaign with tiered commissions\n")

campaign = Campaign(
    name="Premium SaaS Plan",
    description="5-level commission structure for $297/mo plan",
    commission_tiers=[
        {"level": 1, "percentage": 20.0},
        {"level": 2, "percentage": 10.0},
        {"level": 3, "percentage": 5.0},
        {"level": 4, "percentage": 3.0},
        {"level": 5, "percentage": 2.0},
    ],
    max_depth=5,
    hold_days=30,
)
db.add(campaign)
db.commit()

print("  Campaign: Premium SaaS Plan")
print("  âââââââââââ¬âââââââââââââ¬ââââââââââââââââââ")
print("  â  Level  â Commission â  On $297 sale   â")
print("  âââââââââââ¼âââââââââââââ¼ââââââââââââââââââ¤")
total_pct = 0
for tier in campaign.commission_tiers:
    pct = tier["percentage"]
    amt = 297 * pct / 100
    total_pct += pct
    print(f"  â    {tier['level']}    â   {pct:5.1f}%    â    ${amt:>7.2f}      â")
print("  âââââââââââ¼âââââââââââââ¼ââââââââââââââââââ¤")
print(f"  â  Total  â   {total_pct:5.1f}%    â    ${297 * total_pct / 100:>7.2f}      â")
print(f"  â  You    â   {100-total_pct:5.1f}%    â    ${297 * (100-total_pct) / 100:>7.2f}      â")
print("  âââââââââââ´âââââââââââââ´ââââââââââââââââââ")

# ââ Step 3: Simulate a sale from the deepest affiliate âââââ
print("\nð STEP 3: Simulating a $297 sale by Frank (Level 5)\n")

frank = affiliates[5]  # Deepest in the tree
sale = Sale(
    affiliate_id=frank.id,
    campaign_id=campaign.id,
    stripe_payment_intent_id="pi_demo_12345",
    amount_cents=29700,  # $297.00
    currency="usd",
)
db.add(sale)
db.flush()

# ââ Step 4: Calculate commissions up the tree ââââââââââââââ
print("  Running commission engine...\n")
commissions = commission_engine.calculate_commissions(db, sale)
db.commit()

print(f"  {len(commissions)} commissions created:\n")
print("  âââââââââââ¬âââââââââââââââââââ¬ââââââââââ¬âââââââââââ¬ââââââââââââââ")
print("  â  Level  â    Affiliate      â  Rate   â  Amount  â   Status    â")
print("  âââââââââââ¼âââââââââââââââââââ¼ââââââââââ¼âââââââââââ¼ââââââââââââââ¤")
for c in commissions:
    aff = db.get(Affiliate, c.affiliate_id)
    print(f"  â    {c.level}    â {aff.name:<16} â  {c.percentage:4.1f}%  â  ${c.amount_cents/100:>6.2f}  â  {c.status.value:<9}  â")
print("  âââââââââââ´âââââââââââââââââââ´ââââââââââ´âââââââââââ´ââââââââââââââ")

total_commissions = sum(c.amount_cents for c in commissions)
print(f"\n  Total commissions: ${total_commissions/100:.2f}")
print(f"  Platform keeps:   ${(29700 - total_commissions)/100:.2f}")

# ââ Step 5: Show the upline from Frank's perspective âââââââ
print("\nð STEP 4: Frank's upline (who earns from his sales)\n")

upline = frank.get_upline(db, max_depth=10)
for i, ancestor in enumerate(upline):
    level = i + 1
    comm = next((c for c in commissions if c.affiliate_id == ancestor.id), None)
    earning = f"earns ${comm.amount_cents/100:.2f}" if comm else "no commission at this level"
    print(f"  Level {level}: {ancestor.name} â {earning}")

# ââ Step 6: Simulate a second sale + referral tree view ââââ
print("\nð STEP 5: Simulating a second sale by Carol (Level 2)\n")

carol = affiliates[2]
sale2 = Sale(
    affiliate_id=carol.id,
    campaign_id=campaign.id,
    stripe_payment_intent_id="pi_demo_67890",
    amount_cents=29700,
    currency="usd",
)
db.add(sale2)
db.flush()

commissions2 = commission_engine.calculate_commissions(db, sale2)
db.commit()

print(f"  {len(commissions2)} commissions created (Carol only has 2 ancestors):\n")
for c in commissions2:
    aff = db.get(Affiliate, c.affiliate_id)
    print(f"  Level {c.level}: {aff.name} earns ${c.amount_cents/100:.2f} ({c.percentage}%)")

# ââ Step 7: Show earnings summary âââââââââââââââââââââââââ
print("\nð STEP 6: Earnings summary across all sales\n")

print("  ââââââââââââââââââââ¬âââââââââââââââ¬ââââââââââââââââ")
print("  â    Affiliate     â Total Earned â  # of Payouts â")
print("  ââââââââââââââââââââ¼âââââââââââââââ¼ââââââââââââââââ¤")

for aff in affiliates:
    aff_commissions = db.query(Commission).filter(Commission.affiliate_id == aff.id).all()
    total = sum(c.amount_cents for c in aff_commissions)
    count = len(aff_commissions)
    if count > 0:
        print(f"  â {aff.name:16} â   ${total/100:>7.2f}   â      {count:<2}       â")

print("  ââââââââââââââââââââ´âââââââââââââââ´ââââââââââââââââ")

# ââ Step 8: Simulate refund ââââââââââââââââââââââââââââââââ
print("\nð STEP 7: Simulating a refund on Frank's sale\n")

refunded = commission_engine.handle_refund(db, sale)
db.commit()
print(f"  {len(refunded)} commissions marked as REFUNDED:")
for c in refunded:
    aff = db.get(Affiliate, c.affiliate_id)
    print(f"  â {aff.name}: ${c.amount_cents/100:.2f} â {c.status.value}")

# ââ Done âââââââââââââââââââââââââââââââââââââââââââââââââââ
print("\n" + "=" * 65)
print("  DEMO COMPLETE")
print("=" * 65)
print("\n  To run the full API server:")
print("  1. Copy .env.example to .env and add your Stripe keys")
print("  2. Run: python main.py")
print("  3. Open: http://localhost:8000/docs (interactive API docs)\n")

# Cleanup
db.close()
os.remove("/tmp/demo_referral.db")
