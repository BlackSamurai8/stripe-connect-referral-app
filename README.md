# Stripe Connect Referral System

A multilevel referral/affiliate program built with FastAPI, Stripe Connect, and GoHighLevel integration. Supports unlimited-depth referral trees with custom commission tiers per campaign.

## Architecture

```
ââââââââââââââââ     âââââââââââââââââââââ     ââââââââââââââââââ
â  GoHighLevel ââââââ¶â  This App (API)   ââââââ¶â Stripe Connect â
â  (CRM/Funnels)â     â  FastAPI + SQLite  â     â  (Payouts)     â
ââââââââââââââââ     âââââââââââââââââââââ     ââââââââââââââââââ
   webhooks âââ¶         â²       â                  â²       â
                        â       â¼                  â       â¼
                   Stripe Webhooks            Express Accounts
                   (payments, refunds)        (affiliate payouts)
```

## How It Works

1. **Affiliates sign up** via your app or API, optionally providing a referral code
2. **They onboard to Stripe Connect** (Express accounts) to receive payouts
3. **Sales come in** via Stripe payments or GHL orders, tagged with a referral code
4. **Commissions cascade** up the referral tree based on campaign tier rules
5. **After a hold period**, commissions are approved and paid via Stripe Transfers

## Quick Start

```bash
# 1. Clone and install
cd stripe-connect-referral-app
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your Stripe and GHL keys

# 3. Run
python main.py
# App runs at http://localhost:8000
# API docs at http://localhost:8000/docs
```

## Setup Guide

### Step 1: Stripe Setup

1. Go to [Stripe Dashboard](https://dashboard.stripe.com) â Settings â Connect
2. Enable Connect and configure your platform profile
3. Get your API keys from Developers â API Keys
4. Set up webhooks:
   - URL: `https://your-domain.com/webhooks/stripe`
   - Events: `payment_intent.succeeded`, `charge.refunded`, `account.updated`
   - Copy the webhook signing secret to your `.env`
5. Set up a separate Connect webhook:
   - URL: `https://your-domain.com/webhooks/stripe-connect`
   - Listen to events on Connected accounts
   - Events: `account.updated`

### Step 2: GoHighLevel Setup

1. Get your GHL API key from Settings â Business Profile â API Key
2. Set up a webhook in GHL:
   - Go to Automation â Workflows
   - Create a workflow triggered by "Order Submitted" or "Invoice Payment Received"
   - Add a webhook action pointing to `https://your-domain.com/webhooks/ghl`
3. Make sure your GHL forms/funnels capture the referral code in a custom field

### Step 3: Connecting Payments to Referrals

When creating Stripe PaymentIntents (in your checkout flow), include referral metadata:

```python
stripe.PaymentIntent.create(
    amount=9900,
    currency="usd",
    metadata={
        "referral_code": "abc123",      # The affiliate's referral code
        "campaign_id": "campaign-uuid",  # Which campaign this sale belongs to
    }
)
```

For GHL, store the referral code in a custom field on the contact.

## API Endpoints

### Affiliates
- `POST /affiliates` â Register a new affiliate
- `GET /affiliates` â List all affiliates
- `GET /affiliates/{id}` â Get affiliate details
- `POST /affiliates/{id}/onboarding-link` â Get Stripe Connect onboarding URL
- `GET /affiliates/{id}/dashboard-link` â Get Stripe Express dashboard URL
- `GET /affiliates/{id}/referral-tree` â View an affiliate's downline tree
- `GET /affiliates/{id}/earnings` â View earnings breakdown by level

### Campaigns
- `POST /campaigns` â Create a campaign with commission tiers
- `GET /campaigns` â List campaigns
- `PUT /campaigns/{id}` â Update a campaign

### Admin
- `POST /admin/run-payouts` â Trigger a payout run
- `GET /admin/commissions` â List all commissions
- `GET /admin/sales` â List all sales
- `GET /admin/stats` â Program overview statistics

### Webhooks
- `POST /webhooks/stripe` â Stripe payment/refund events
- `POST /webhooks/stripe-connect` â Stripe Connect account events
- `POST /webhooks/ghl` â GoHighLevel order events

## Campaign Configuration Example

Create a 5-level campaign:

```json
{
  "name": "Premium Plan Referrals",
  "commission_tiers": [
    {"level": 1, "percentage": 20.0},
    {"level": 2, "percentage": 10.0},
    {"level": 3, "percentage": 5.0},
    {"level": 4, "percentage": 3.0},
    {"level": 5, "percentage": 2.0}
  ],
  "max_depth": 5,
  "hold_days": 30,
  "stripe_product_id": "prod_xxx",
  "ghl_product_id": "ghl_product_123"
}
```

This means on a $100 sale:
- Direct referrer earns $20
- Their referrer earns $10
- Level 3 earns $5
- Level 4 earns $3
- Level 5 earns $2
- You keep $60

## Production Deployment

1. **Switch to PostgreSQL** â Update `DATABASE_URL` in `.env`
2. **Set up a cron job** for automatic payouts:
   ```bash
   # Run payouts daily at midnight
   0 0 * * * curl -X POST https://your-domain.com/admin/run-payouts
   ```
3. **Use a process manager** like systemd or Docker
4. **Add authentication** to admin endpoints (API key or JWT)
5. **Set up monitoring** for webhook failures

## File Structure

```
âââ main.py                 # FastAPI app with all routes
âââ models.py               # SQLAlchemy database models
âââ database.py             # Database session management
âââ config.py               # Environment configuration
âââ commission_engine.py    # Multilevel commission calculator
âââ payout_service.py       # Stripe Connect transfer processor
âââ requirements.txt        # Python dependencies
âââ .env.example            # Environment variable template
âââ README.md               # This file
```
