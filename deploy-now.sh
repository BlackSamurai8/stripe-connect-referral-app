#!/bin/bash
# ============================================================
# DEPLOY SCRIPT â Run this on your computer
# ============================================================
# Prerequisites:
#   1. Install Git: https://git-scm.com/downloads
#   2. Install GitHub CLI: https://cli.github.com
#   3. Install Railway CLI: npm install -g @railway/cli
#      (or: brew install railway on Mac)
#
# Then run: bash deploy-now.sh
# ============================================================

set -e

echo ""
echo "=========================================="
echo "  Stripe Connect Referral App â Deployer"
echo "=========================================="
echo ""

# --- Step 1: Authenticate ---
echo "STEP 1: Logging in to GitHub..."
gh auth status 2>/dev/null || gh auth login

echo ""
echo "STEP 2: Logging in to Railway..."
railway login 2>/dev/null || railway login

# --- Step 2: Create GitHub repo ---
echo ""
echo "STEP 3: Creating GitHub repository..."
cd "$(dirname "$0")"

git init 2>/dev/null || true
git add -A
git commit -m "Initial commit - Stripe Connect referral system" 2>/dev/null || echo "Already committed"

gh repo create stripe-connect-referral-app --private --source=. --push

echo ""
echo "â Code pushed to GitHub"

# --- Step 3: Create Railway project + database ---
echo ""
echo "STEP 4: Creating Railway project..."
railway init --name stripe-connect-referral

echo ""
echo "STEP 5: Adding PostgreSQL database..."
railway add --plugin postgresql

echo ""
echo "STEP 6: Linking to Railway service..."
railway link

# --- Step 4: Set environment variables ---
echo ""
echo "STEP 7: Setting environment variables..."
echo ""
echo "I need your API keys. You can find them at:"
echo "  Stripe: https://dashboard.stripe.com/apikeys"
echo "  GHL:    Settings â Business Profile â API Key"
echo ""

read -p "Stripe Secret Key (sk_...): " STRIPE_SK
read -p "Stripe Publishable Key (pk_...): " STRIPE_PK

# Generate a random app secret
APP_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32)

railway variables set \
  STRIPE_SECRET_KEY="$STRIPE_SK" \
  STRIPE_PUBLISHABLE_KEY="$STRIPE_PK" \
  STRIPE_WEBHOOK_SECRET="placeholder-update-after-deploy" \
  STRIPE_CONNECT_WEBHOOK_SECRET="placeholder-update-after-deploy" \
  GHL_API_KEY="placeholder-update-later" \
  APP_SECRET_KEY="$APP_SECRET" \
  FRONTEND_URL="https://your-frontend.com" \
  DEFAULT_MAX_DEPTH="5"

echo ""
echo "â Variables set. Your admin API key is:"
echo "  $APP_SECRET"
echo "  (Save this! You'll need it for admin endpoints)"

# --- Step 5: Deploy ---
echo ""
echo "STEP 8: Deploying..."
railway up --detach

echo ""
echo "STEP 9: Generating public URL..."
railway domain

echo ""
echo "=========================================="
echo "  DEPLOYMENT COMPLETE!"
echo "=========================================="
echo ""
echo "NEXT STEPS:"
echo "  1. Copy your Railway URL from above"
echo "  2. Go to https://dashboard.stripe.com/webhooks"
echo "  3. Create webhook â URL: https://YOUR-URL/webhooks/stripe"
echo "     Events: payment_intent.succeeded, charge.refunded, account.updated"
echo "  4. Copy the webhook signing secret and update it:"
echo "     railway variables set STRIPE_WEBHOOK_SECRET=whsec_xxx"
echo "  5. Create second webhook â URL: https://YOUR-URL/webhooks/stripe-connect"
echo "     Check 'Listen to events on Connected accounts'"
echo "     Events: account.updated"
echo "     railway variables set STRIPE_CONNECT_WEBHOOK_SECRET=whsec_xxx"
echo "  6. Update APP_BASE_URL:"
echo "     railway variables set APP_BASE_URL=https://YOUR-URL"
echo "  7. Visit https://YOUR-URL/docs to see your API!"
echo ""
