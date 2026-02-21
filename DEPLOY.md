# Deploy to Railway â Step by Step

This guide walks you through deploying the Stripe Connect Referral System to Railway with a PostgreSQL database. Total time: about 10 minutes.

## Prerequisites

You'll need:

- A Stripe account with Connect enabled (https://dashboard.stripe.com/settings/connect)
- A GoHighLevel account with API access
- A GitHub account (free)
- A Railway account (https://railway.app â sign up with GitHub, free tier available)

## Step 1: Push to GitHub

Open a terminal on your computer and run:

```bash
cd stripe-connect-referral-app
git init
git add .
git commit -m "Initial commit - Stripe Connect referral system"
```

Then create a new repo on GitHub (https://github.com/new), name it `stripe-connect-referral-app`, and push:

```bash
git remote add origin https://github.com/YOUR_USERNAME/stripe-connect-referral-app.git
git branch -M main
git push -u origin main
```

## Step 2: Create Railway Project

1. Go to https://railway.app/new
2. Click **"Deploy from GitHub Repo"**
3. Select your `stripe-connect-referral-app` repo
4. Railway will auto-detect it as a Python app and start building

## Step 3: Add PostgreSQL

1. In your Railway project dashboard, click **"+ New"** â **"Database"** â **"PostgreSQL"**
2. Railway creates the database instantly
3. Click on the PostgreSQL service â **"Variables"** tab
4. Copy the `DATABASE_URL` value (starts with `postgresql://...`)

## Step 4: Set Environment Variables

1. Click on your app service (not the database)
2. Go to the **"Variables"** tab
3. Click **"RAW Editor"** and paste all of these (filling in your real values):

```
STRIPE_SECRET_KEY=sk_live_your_real_key
STRIPE_PUBLISHABLE_KEY=pk_live_your_real_key
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret
STRIPE_CONNECT_WEBHOOK_SECRET=whsec_your_connect_webhook_secret
GHL_API_KEY=your_gohighlevel_api_key
GHL_WEBHOOK_SECRET=your_ghl_webhook_verify_token
DATABASE_URL=${{Postgres.DATABASE_URL}}
APP_SECRET_KEY=generate-a-random-string-here
APP_BASE_URL=https://your-app.up.railway.app
FRONTEND_URL=https://your-frontend.com
DEFAULT_MAX_DEPTH=5
```

**Important:** The `${{Postgres.DATABASE_URL}}` syntax tells Railway to automatically inject the database URL. Don't replace it with the actual URL.

**Generate a random APP_SECRET_KEY** â this is your admin API key. Run this in your terminal:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

4. Click **"Save"**. Railway will automatically redeploy.

## Step 5: Get Your App URL

1. Click on your app service
2. Go to **"Settings"** â **"Networking"**
3. Click **"Generate Domain"** to get a public URL like `https://stripe-connect-referral-abc123.up.railway.app`
4. Go back to Variables and update `APP_BASE_URL` to this URL

## Step 6: Set Up Stripe Webhooks

Go to https://dashboard.stripe.com/webhooks and create two webhooks:

**Webhook 1 â Platform events:**

- URL: `https://YOUR-RAILWAY-URL/webhooks/stripe`
- Events to listen for:
  - `payment_intent.succeeded`
  - `charge.refunded`
  - `account.updated`
- Copy the signing secret â put it in `STRIPE_WEBHOOK_SECRET`

**Webhook 2 â Connected account events:**

- URL: `https://YOUR-RAILWAY-URL/webhooks/stripe-connect`
- Check **"Listen to events on Connected accounts"**
- Events: `account.updated`
- Copy the signing secret â put it in `STRIPE_CONNECT_WEBHOOK_SECRET`

## Step 7: Set Up GoHighLevel Webhook

1. In GHL, go to **Automation** â **Workflows**
2. Create a new workflow
3. Trigger: **"Order Submitted"** or **"Invoice Payment Received"**
4. Add action: **"Webhook"**
5. URL: `https://YOUR-RAILWAY-URL/webhooks/ghl`
6. Method: POST
7. Make sure the referral code is captured in a custom field on the contact

## Step 8: Verify It's Working

Open your browser and visit:

```
https://YOUR-RAILWAY-URL/docs
```

You should see the interactive API documentation (Swagger UI). Try these:

1. **Health check:** `GET /health` â should return `{"status": "healthy"}`
2. **Create a campaign:** `POST /campaigns` with your commission tiers
3. **Create an affiliate:** `POST /affiliates` with an email and name
4. **Get onboarding link:** `POST /affiliates/{id}/onboarding-link` â returns a Stripe URL

For admin endpoints, add the header: `X-API-Key: your-app-secret-key`

## Step 9: Set Up Automatic Payouts (Optional)

Railway supports cron jobs. Add a second service to your project:

1. Click **"+ New"** â **"Cron Job"**
2. Set schedule: `0 0 * * *` (daily at midnight UTC)
3. Command: `curl -X POST https://YOUR-RAILWAY-URL/admin/run-payouts -H "X-API-Key: YOUR_APP_SECRET_KEY"`

This will automatically approve held commissions and send Stripe transfers every day.

## Costs

Railway's free tier includes $5/month of usage, which covers a small app + database. For a production referral program, expect roughly $5-10/month on the Hobby plan.

## Troubleshooting

**App won't start:** Check the deploy logs in Railway. Usually it's a missing environment variable.

**Webhooks not arriving:** Use Stripe's webhook logs (Dashboard â Webhooks â select webhook â Recent deliveries) to see if events are being sent and what response your app returns.

**"Invalid signature" errors:** Make sure you copied the correct webhook signing secret. Each webhook endpoint has its own secret.

**Database errors:** Click on the PostgreSQL service in Railway and check that it's running. The `${{Postgres.DATABASE_URL}}` variable should auto-populate.
