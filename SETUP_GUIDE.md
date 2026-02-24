# Stripe Connect Referral System - Complete Setup Guide

**For: Strati**
**Version: 2.0.0**
**Last Updated: February 24, 2026**

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [How It Works (Flow Diagrams)](#3-how-it-works)
4. [Stripe Setup](#4-stripe-setup)
5. [GoHighLevel (GHL) Setup](#5-gohighlevel-ghl-setup)
6. [Railway Deployment & Environment Variables](#6-railway-deployment--environment-variables)
7. [Admin Dashboard Guide](#7-admin-dashboard-guide)
8. [API Reference](#8-api-reference)
9. [Testing the Full Flow](#9-testing-the-full-flow)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. System Overview

This is a **multi-level referral commission system** built on Stripe Connect. It allows you to:

- **Create affiliates** with unique referral codes
- **Track sales** via Stripe payments or GHL orders
- **Calculate multi-level commissions** automatically (up to 5 levels deep)
- **Pay affiliates** directly to their Stripe Express accounts
- **Monitor everything** via an admin dashboard

### Key Components

| Component | Purpose |
|-----------|---------|
| **FastAPI Backend** | Handles all API requests, webhooks, and dashboard |
| **Stripe Connect** | Creates Express accounts for affiliates and sends payouts |
| **Stripe Webhooks** | Receives payment events to trigger commission calculation |
| **GHL Webhooks** | Receives order events from GoHighLevel |
| **Commission Engine** | Calculates multi-level commissions walking up the referral tree |
| **Admin Dashboard** | Web UI to manage affiliates, campaigns, payouts, and monitor system |
| **PostgreSQL** | Stores all affiliates, sales, commissions, and payouts |

---

## 2. Architecture Diagram

```
                          STRIPE CONNECT REFERRAL SYSTEM
   ============================================================================

   CUSTOMER PAYMENT SOURCES
   ========================

   +-----------------+         +------------------+
   |  Stripe Payment |         |  GHL Order/Form  |
   |  (with referral |         |  (with referral  |
   |   code in       |         |   code in custom |
   |   metadata)     |         |   fields)        |
   +--------+--------+         +--------+---------+
            |                           |
            v                           v
   +------------------+       +------------------+
   | POST /webhooks/  |       | POST /webhooks/  |
   |     stripe       |       |      ghl         |
   | (signature       |       | (HMAC signature  |
   |  verified)       |       |  verified)       |
   +--------+---------+       +--------+---------+
            |                           |
            +-------------+-------------+
                          |
                          v
   COMMISSION CALCULATION ENGINE
   =============================

   +----------------------------------------------------------+
   |                                                          |
   |  1. Find affiliate by referral_code                      |
   |  2. Create Sale record                                   |
   |  3. Walk UP the referral tree:                           |
   |                                                          |
   |     Sale Affiliate (Level 0) - no commission             |
   |          |                                               |
   |          v                                               |
   |     Parent (Level 1) --> 10% commission                  |
   |          |                                               |
   |          v                                               |
   |     Grandparent (Level 2) --> 5% commission              |
   |          |                                               |
   |          v                                               |
   |     Great-Grandparent (Level 3) --> 2% commission        |
   |          |                                               |
   |          v                                               |
   |     ... up to max_depth (default 5)                      |
   |                                                          |
   |  4. Create Commission records (status: PENDING)          |
   |  5. Set hold_until = now + hold_days (default 14 days)   |
   |                                                          |
   +----------------------------------------------------------+


   PAYOUT FLOW
   ===========

   +------------------+     +------------------+     +------------------+
   |  PENDING         | --> |  APPROVED        | --> |  PAID            |
   |  Commissions     |     |  Commissions     |     |  Commissions     |
   |                  |     |                  |     |                  |
   |  (hold period    |     |  (admin clicks   |     |  (Stripe Transfer|
   |   not expired)   |     |   "Approve" or   |     |   sent to        |
   |                  |     |   hold expires)   |     |   affiliate's    |
   |                  |     |                  |     |   Express acct)  |
   +------------------+     +------------------+     +------------------+

         If sale refunded:
         PENDING --> REFUNDED (no payout)
         PENDING --> CANCELLED (charge.refunded webhook)


   STRIPE CONNECT ACCOUNTS
   =======================

   +-------------------+         +--------------------+
   |  YOUR PLATFORM    |         |  AFFILIATE EXPRESS  |
   |  ACCOUNT          | ------> |  ACCOUNT            |
   |                   | Transfer|                     |
   |  (receives all    | ------> |  (receives          |
   |   payments)       |         |   commission via    |
   |                   |         |   stripe.Transfer)  |
   +-------------------+         +--------------------+

   Affiliate Onboarding:
   1. Admin generates onboarding link
   2. Affiliate clicks link -> Stripe hosted onboarding
   3. Affiliate provides identity, bank account
   4. On completion -> redirect to /onboarding-complete
   5. App verifies charges_enabled + payouts_enabled
   6. Affiliate status: ONBOARDING -> ACTIVE


   ADMIN DASHBOARD
   ===============

   +------------------------------------------------------------------+
   |  https://your-app.up.railway.app/admin/dashboard                 |
   |                                                                  |
   |  [Overview] [Affiliates] [Campaigns] [Tiers] [Payouts]          |
   |  [Webhook Events] [Dead Letter Queue] [Audit Log]               |
   |                                                                  |
   |  Overview:     KPI cards, recent events, error summary           |
   |  Affiliates:   List, status, onboarding buttons                  |
   |  Campaigns:    Commission structures, hold periods               |
   |  Tiers:        Per-level rates, bonus rates, thresholds          |
   |  Payouts:      Transfer history, Run Payouts button              |
   |  Webhooks:     Event log from Stripe and GHL                     |
   |  DLQ:          Failed events, retry/resolve buttons              |
   |  Audit Log:    System activity history                           |
   +------------------------------------------------------------------+
```

---

## 3. How It Works

### 3A. Payment to Commission Flow

```
Step 1: Customer pays via Stripe (with referral code in metadata)
        OR GHL sends order webhook (with referral code)

Step 2: Webhook received -> Sale created in database

Step 3: Commission Engine runs:
        - Finds affiliate by referral code
        - Walks up referral tree
        - Creates commission for each parent at each level
        - Commission amount = sale_amount x tier_rate
        - All commissions start as PENDING with hold_until date

Step 4: After hold period (14 days default):
        - Admin approves commissions OR auto-approved when hold expires
        - Status changes: PENDING -> APPROVED

Step 5: Admin runs payouts:
        - Stripe Transfer created for each approved commission
        - Money sent to affiliate's Express account
        - Status changes: APPROVED -> PAID
```

### 3B. Multi-Level Commission Example

```
Referral Tree:
  Alice (root, no parent)
    -> Bob (referred by Alice)
      -> Carol (referred by Bob)
        -> Dave (referred by Carol) <-- MAKES A SALE

Campaign: "Default" with tiers:
  Level 1: 10%
  Level 2: 5%
  Level 3: 2%

Dave makes a $100 sale:
  - Carol (Level 1 parent): $10.00 (10%)
  - Bob (Level 2 grandparent): $5.00 (5%)
  - Alice (Level 3 great-grandparent): $2.00 (2%)
  Total commissions: $17.00
```

### 3C. Affiliate Onboarding Flow

```
Step 1: Admin creates affiliate via API or dashboard
        -> Affiliate gets referral code (e.g., "KG23MNAO")

Step 2: Admin clicks "Onboard to Stripe" in dashboard
        -> Stripe Express account created
        -> Onboarding URL generated

Step 3: Share onboarding URL with affiliate
        -> Affiliate clicks link
        -> Stripe-hosted form: identity, address, bank account

Step 4: Affiliate completes form
        -> Redirected to /onboarding-complete
        -> App verifies with Stripe: charges_enabled + payouts_enabled
        -> Affiliate status: ACTIVE
        -> Ready to receive payouts!
```

---

## 4. Stripe Setup

### Step 1: Enable Stripe Connect

1. Go to **[Stripe Dashboard](https://dashboard.stripe.com)** -> **Settings** -> **Connect**
2. Click **"Get started with Connect"**
3. Choose **"Platform"** as your business model
4. Complete the Connect application

### Step 2: Get API Keys

1. Go to **Developers** -> **API Keys**
2. Copy your **Secret key** (starts with `sk_test_` for sandbox, `sk_live_` for production)
3. Copy your **Publishable key** (starts with `pk_test_` or `pk_live_`)

### Step 3: Create Main Stripe Webhook

This webhook receives payment events from your platform account.

1. Go to **Developers** -> **Webhooks** -> **Add endpoint**
2. Set **Endpoint URL**: `https://your-app.up.railway.app/webhooks/stripe`
3. Select **"Your account"** under "Events from"
4. Select these events:
   - `payment_intent.succeeded`
   - `charge.refunded`
   - `account.updated`
5. Click **Add endpoint**
6. Copy the **Signing secret** (starts with `whsec_`)
7. Save as `STRIPE_WEBHOOK_SECRET` in Railway

### Step 4: Create Stripe Connect Webhook

This webhook receives events from your affiliates' connected accounts.

1. Go to **Developers** -> **Webhooks** -> **Add endpoint**
2. Set **Endpoint URL**: `https://your-app.up.railway.app/webhooks/stripe-connect`
3. Select **"Connected and v2 accounts"** under "Events from"
4. Select these events:
   - `v2.core.account.updated` (or `account.updated`)
5. Click **Add endpoint**
6. Copy the **Signing secret** (starts with `whsec_`)
7. Save as `STRIPE_CONNECT_WEBHOOK_SECRET` in Railway

### Step 5: Configure Payments with Referral Codes

When creating payments, include the affiliate's referral code in the metadata:

```javascript
// Stripe.js / Node.js example
const paymentIntent = await stripe.paymentIntents.create({
  amount: 10000,  // $100.00 in cents
  currency: 'usd',
  metadata: {
    referral_code: 'KG23MNAO',  // Affiliate's referral code
    campaign_id: 'your-campaign-id'  // Optional, defaults to first active
  }
});
```

**Important:** The `referral_code` in metadata is what links a payment to an affiliate. Without it, no commission is calculated.

---

## 5. GoHighLevel (GHL) Setup

### Step 1: Create a Workflow Webhook

1. In GHL, go to **Automation** -> **Workflows**
2. Create a new workflow or edit an existing one
3. Add a **Webhook** action step
4. Set the webhook URL to: `https://your-app.up.railway.app/webhooks/ghl`
5. Set method: **POST**
6. Set content type: **application/json**

### Step 2: Configure the Webhook Payload

The webhook must send a JSON body with these fields:

```json
{
  "type": "order.created",
  "contact_id": "{{contact.id}}",
  "order_id": "{{order.id}}",
  "amount": {{order.total}},
  "customer_email": "{{contact.email}}",
  "referral_code": "{{contact.referral_code}}",
  "campaign_id": "your-campaign-id"
}
```

**Field Mapping:**

| Field | Required | Description | GHL Variable |
|-------|----------|-------------|--------------|
| `referral_code` | Yes | Affiliate's referral code | `{{contact.referral_code}}` or custom field |
| `amount` | Yes | Order amount as number (dollars, not cents) | `{{order.total}}` |
| `order_id` | No | GHL order identifier | `{{order.id}}` |
| `contact_id` | No | GHL contact ID | `{{contact.id}}` |
| `customer_email` | No | Customer email | `{{contact.email}}` |
| `campaign_id` | No | Campaign to use (defaults to first active) | Custom or hardcoded |

### Step 3: Store Referral Codes in GHL Contacts

You need a way to track which referral code brought in each contact:

1. Create a **Custom Field** in GHL called `referral_code`
2. When a contact comes in via a referral link, store the code:
   - Via landing page URL parameter: `?ref=KG23MNAO`
   - Via hidden form field
   - Via GHL workflow that extracts from source URL

### Step 4: Trigger Webhook on Order

Set your workflow trigger to fire when:
- **New order is created**
- **Payment is received**
- **Invoice is paid**

Then add the webhook step to send the data to the referral system.

### Step 5: GHL Webhook Signature (Optional)

For added security, you can configure HMAC signature verification:

1. Set `GHL_WEBHOOK_SECRET` in Railway to a secret string
2. In GHL webhook settings, add a header:
   - Header: `x-ghl-signature`
   - Value: HMAC-SHA256 hash of the request body using the secret

> Note: If you set `GHL_WEBHOOK_SECRET` to `skip` or `not_needed_yet`, signature verification is disabled.

### GHL Integration Diagram

```
  GHL Contact fills form with ?ref=KG23MNAO
       |
       v
  Contact created in GHL
  (referral_code stored in custom field)
       |
       v
  Contact makes purchase / order created
       |
       v
  GHL Workflow triggers
       |
       v
  Webhook POST to /webhooks/ghl
  {
    referral_code: "KG23MNAO",
    amount: 99.99,
    order_id: "order_123",
    customer_email: "customer@email.com"
  }
       |
       v
  Sale created -> Commissions calculated
  (Affiliate KG23MNAO's parent gets Level 1 commission)
```

---

## 6. Railway Deployment & Environment Variables

### Required Environment Variables

Set these in Railway -> Your Service -> Variables:

| Variable | Example | Description |
|----------|---------|-------------|
| `STRIPE_SECRET_KEY` | `sk_test_51SGT...` | Stripe API secret key |
| `STRIPE_PUBLISHABLE_KEY` | `pk_test_51SGT...` | Stripe publishable key |
| `STRIPE_WEBHOOK_SECRET` | `whsec_ZqZNs86...` | Signing secret for main webhook |
| `STRIPE_CONNECT_WEBHOOK_SECRET` | `whsec_ro921...` | Signing secret for Connect webhook |
| `GHL_API_KEY` | `your-ghl-key` | GHL API key (or `not_needed_yet`) |
| `GHL_WEBHOOK_SECRET` | `your-secret` | GHL webhook HMAC secret (or `skip`) |
| `DATABASE_URL` | `postgresql://...` | PostgreSQL connection string (auto from Railway) |
| `APP_BASE_URL` | `https://web-production-0db7d4.up.railway.app` | Your app's public URL |
| `FRONTEND_URL` | `https://web-production-0db7d4.up.railway.app` | Frontend URL (same if no separate frontend) |
| `APP_SECRET_KEY` | `random-secure-string` | API key for endpoints |
| `ADMIN_API_KEY` | `random-secure-string` | Admin API key (used for dashboard login) |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_MAX_DEPTH` | `5` | Default max referral tree depth |
| `LOG_LEVEL` | `INFO` | Logging level |
| `SENTRY_DSN` | (empty) | Sentry error tracking DSN |
| `ENVIRONMENT` | `production` | Environment name |
| `DLQ_MAX_RETRIES` | `3` | Max retries for failed events |
| `DLQ_RETRY_DELAY_MINUTES` | `15` | Delay between retries |

### Deployment

The app auto-deploys when you push to the `main` branch on GitHub:
- Repository: `https://github.com/BlackSamurai8/stripe-connect-referral-app`
- Railway auto-detects pushes and rebuilds

---

## 7. Admin Dashboard Guide

### Accessing the Dashboard

1. Open: `https://your-app.up.railway.app/admin/dashboard`
2. Enter your **Admin API Key** when prompted
3. The key is stored in your browser session

### Dashboard Tabs

#### Overview Tab
- **What it shows:** KPI summary cards (affiliates, sales, commissions, payouts)
- **Use for:** Quick health check of the system
- **Key metrics:** Active affiliates, total revenue, pending vs paid commissions

#### Affiliates Tab
- **What it shows:** All affiliates with their status, referral codes, and Stripe connection
- **Actions available:**
  - View affiliate details
  - **"Onboard to Stripe"** button: Generates Stripe Express onboarding link
  - Copy onboarding link to send to affiliate
  - Shows "Connected" badge for fully onboarded affiliates

#### Campaigns Tab
- **What it shows:** All campaigns with their commission structures
- **Actions available:**
  - View campaign details
  - See commission tiers per campaign
  - Campaign settings: max_depth, hold_days

#### Commission Tiers Tab
- **What it shows:** All commission tiers across campaigns
- **Actions available:**
  - **"+ Add Tier"** button: Create new tier with campaign, level, rate, bonus
  - Edit existing tiers
  - Deactivate old tiers

#### Payouts Tab
- **What it shows:** All payout transfers with status
- **Actions available:**
  - **"Run Payouts"** button: Processes all approved commissions
  - View transfer details, amounts, dates
  - See failed payouts with error messages

#### Webhook Events Tab
- **What it shows:** Recent webhook events from Stripe and GHL
- **Use for:** Debugging, verifying events are being received
- **Shows:** Event ID, type, source, processed status, timestamp

#### Dead Letter Queue Tab
- **What it shows:** Failed webhook events that couldn't be processed
- **Actions available:**
  - **Retry** a failed event
  - **Resolve** (mark as handled)
  - View error messages

#### Audit Log Tab
- **What it shows:** All system actions with timestamps
- **Use for:** Compliance, debugging, tracking who did what
- **Tracks:** Payout runs, status changes, tier updates

### Common Dashboard Workflows

#### Onboard a New Affiliate
1. Go to **Affiliates** tab
2. Find the affiliate (or create via API)
3. Click **"Onboard to Stripe"**
4. Copy the generated URL
5. Send URL to the affiliate
6. After they complete onboarding, their status changes to "Connected"

#### Process Payouts
1. Go to **Payouts** tab
2. Click **"Run Payouts"** button
3. Review results: Paid, Failed, Skipped counts
4. Check failed payouts for error details

#### Monitor System Health
1. Go to **Overview** tab
2. Check if DLQ count > 0 (indicates failed webhooks)
3. Review recent errors
4. Go to **Dead Letter Queue** tab to retry/resolve failures

---

## 8. API Reference

### Authentication

All API endpoints (except webhooks and onboarding pages) require:

```
Header: X-API-Key: your-admin-api-key
```

### Key Endpoints

#### Create Affiliate
```
POST /affiliates
Content-Type: application/json
X-API-Key: your-key

{
  "name": "John Smith",
  "email": "john@example.com",
  "phone": "+1234567890",
  "parent_referral_code": "PARENT_CODE"  // optional, links to parent
}
```

#### Generate Onboarding Link
```
POST /affiliates/{affiliate_id}/onboarding-link
X-API-Key: your-key

Response:
{
  "affiliate_id": "...",
  "stripe_account_id": "acct_...",
  "onboarding_url": "https://connect.stripe.com/...",
  "expires_at": 1234567890
}
```

#### Create Campaign
```
POST /campaigns
Content-Type: application/json
X-API-Key: your-key

{
  "name": "Main Referral Program",
  "description": "10% L1, 5% L2, 2% L3",
  "commission_tiers": [
    {"level": 1, "rate": 0.10},
    {"level": 2, "rate": 0.05},
    {"level": 3, "rate": 0.02}
  ],
  "max_depth": 5,
  "hold_days": 14
}
```

#### Approve Commissions
```
POST /admin/approve-commissions
X-API-Key: your-key

Response: {"approved": 5}
```

#### Run Payouts
```
POST /admin/run-payouts
X-API-Key: your-key

Response: {
  "approved": 5,
  "paid": 4,
  "failed": 0,
  "skipped": 1,
  "total_amount_cents": 1500
}
```

#### Get System Stats
```
GET /admin/stats
X-API-Key: your-key
```

---

## 9. Testing the Full Flow

### Step-by-Step Test in Sandbox

#### 1. Create an Affiliate
```bash
curl -X POST https://your-app.up.railway.app/affiliates \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"name": "Test Parent", "email": "parent@test.com"}'
```
Note the `referral_code` (e.g., `KG23MNAO`)

#### 2. Onboard the Affiliate to Stripe
```bash
curl -X POST https://your-app.up.railway.app/affiliates/AFFILIATE_ID/onboarding-link \
  -H "X-API-Key: YOUR_API_KEY"
```
Open the `onboarding_url` in a browser and complete the Stripe onboarding.

#### 3. Create a Child Affiliate (seller)
```bash
curl -X POST https://your-app.up.railway.app/affiliates \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"name": "Test Seller", "email": "seller@test.com", "parent_referral_code": "KG23MNAO"}'
```
Note the child's `referral_code` (e.g., `WRUUX7VC`)

#### 4. Create a Test Payment
```bash
curl -X POST https://api.stripe.com/v1/payment_intents \
  -u "sk_test_YOUR_KEY:" \
  -d "amount=5000" \
  -d "currency=usd" \
  -d "metadata[referral_code]=WRUUX7VC" \
  -d "payment_method=pm_card_visa" \
  -d "confirm=true" \
  -d "automatic_payment_methods[enabled]=true" \
  -d "automatic_payment_methods[allow_redirects]=never"
```

#### 5. Verify Commission Was Created
Check dashboard Overview tab - you should see 1 new commission (PENDING)

#### 6. Approve and Pay
```bash
# Approve commissions (bypass hold period)
curl -X POST https://your-app.up.railway.app/admin/approve-commissions \
  -H "X-API-Key: YOUR_API_KEY"

# Run payouts
curl -X POST https://your-app.up.railway.app/admin/run-payouts \
  -H "X-API-Key: YOUR_API_KEY"
```

#### 7. Verify Payout
Check dashboard Payouts tab - should show COMPLETED transfer

---

## 10. Troubleshooting

### Common Issues

#### "No commissions created" after payment
- **Cause:** The affiliate who made the sale has no parent
- **Fix:** Commissions go to PARENT affiliates, not the selling affiliate. The seller needs a `parent_referral_code` set

#### "Skipped" in payout results
- **Cause:** Affiliate doesn't have a Stripe account or onboarding isn't complete
- **Fix:** Generate onboarding link and have affiliate complete it. Check that `stripe_onboarding_complete` is `true`

#### Webhook not received
- **Check:** Stripe Dashboard -> Webhooks -> Event deliveries tab
- **Check:** Railway logs (`railway logs -n 50`)
- **Fix:** Verify endpoint URL is correct, signing secret matches

#### "Invalid signature" webhook error
- **Cause:** Webhook signing secret mismatch
- **Fix:** Copy the exact signing secret from Stripe webhook settings and update Railway env var

#### GHL webhook returning "ignored"
- **Cause:** Missing `referral_code` or `amount` in webhook payload
- **Fix:** Ensure GHL workflow sends these fields in the JSON body

#### Affiliate status stuck on "onboarding"
- **Cause:** Affiliate didn't complete Stripe onboarding, or webhook didn't fire
- **Fix:** Have affiliate visit the onboarding URL again, or manually set status to "active" via API

### Useful Commands

```bash
# Check Railway logs
railway logs -n 50

# Check app health
curl https://your-app.up.railway.app/health

# Get system stats
curl -H "X-API-Key: KEY" https://your-app.up.railway.app/admin/stats

# List affiliates
curl -H "X-API-Key: KEY" https://your-app.up.railway.app/affiliates

# Check DLQ for errors
curl -H "X-API-Key: KEY" https://your-app.up.railway.app/admin/dead-letter-queue
```

---

## Quick Reference Card

| What | URL/Value |
|------|-----------|
| **Dashboard** | `https://web-production-0db7d4.up.railway.app/admin/dashboard` |
| **API Base** | `https://web-production-0db7d4.up.railway.app` |
| **Stripe Webhook** | `/webhooks/stripe` |
| **Connect Webhook** | `/webhooks/stripe-connect` |
| **GHL Webhook** | `/webhooks/ghl` |
| **GitHub Repo** | `https://github.com/BlackSamurai8/stripe-connect-referral-app` |
| **Railway Project** | `discerning-celebration` |
| **Stripe Mode** | Sandbox (switch to live when ready) |
