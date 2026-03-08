#!/bin/bash
# ╔══════════════════════════════════════════════════════════╗
# ║   WHALE RADAR — One-Command Google Cloud Run Deploy     ║
# ║   Run this in Google Cloud Shell:  bash deploy.sh       ║
# ╚══════════════════════════════════════════════════════════╝

set -e  # Exit on any error

echo ""
echo "🐋 WHALE RADAR — Deploying to Google Cloud Run"
echo "================================================"

# ── Get or create a project ──────────────────────────────
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo ""
  echo "No project set. Creating one now..."
  PROJECT_ID="whale-radar-$(date +%s | tail -c 6)"
  gcloud projects create $PROJECT_ID --name="Whale Radar"
  gcloud config set project $PROJECT_ID
  echo "✅ Project created: $PROJECT_ID"
else
  echo "✅ Using project: $PROJECT_ID"
fi

# ── Enable required APIs ─────────────────────────────────
echo ""
echo "Enabling Cloud Run & Container Registry APIs..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com --quiet
echo "✅ APIs enabled"

# ── Deploy directly from source (no Docker needed) ───────
echo ""
echo "Deploying to Cloud Run (this takes ~2 minutes)..."
echo ""

gcloud run deploy whale-radar \
  --source . \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 3600 \
  --concurrency 1 \
  --quiet

# ── Get the URL ───────────────────────────────────────────
echo ""
SERVICE_URL=$(gcloud run services describe whale-radar \
  --region us-central1 \
  --format 'value(status.url)')

echo "================================================"
echo "✅ DEPLOYMENT COMPLETE!"
echo ""
echo "🌐 Your live URL:"
echo "   $SERVICE_URL"
echo ""
echo "Open that URL in any browser to use Whale Radar."
echo "================================================"
