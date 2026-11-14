#!/usr/bin/env bash
# Build visualizer/web against the Cloud Run API URLs and deploy it to Firebase Hosting.
#   PROJECT_ID=my-proj REGION=us-east1 deploy/firebase.sh [--dry-run]
# TOWER_URL / VIZ_API_URL may be set; otherwise they are read from Cloud Run.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

: "${PROJECT_ID:?set PROJECT_ID (the Google Cloud / Firebase project id)}"
: "${REGION:?set REGION (the Cloud Run region of the APIs)}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/visualizer/web"

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

service_url() {
  gcloud run services describe "$1" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
}

if [ -z "${TOWER_URL:-}" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    run gcloud run services describe phylax-tower --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
    TOWER_URL="https://phylax-tower-XXXX.${REGION}.run.app"
  else
    TOWER_URL="$(service_url phylax-tower)"
  fi
fi
if [ -z "${VIZ_API_URL:-}" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    run gcloud run services describe phylax-viz-api --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
    VIZ_API_URL="https://phylax-viz-api-XXXX.${REGION}.run.app"
  else
    VIZ_API_URL="$(service_url phylax-viz-api)"
  fi
fi
[ -n "$TOWER_URL" ] && [ -n "$VIZ_API_URL" ] || { echo "could not resolve the API URLs" >&2; exit 1; }
echo "VITE_TOWER_API=${TOWER_URL}"
echo "VITE_VIZ_API=${VIZ_API_URL}"

run npm --prefix "$WEB" ci --no-audit --no-fund
run env VITE_TOWER_API="$TOWER_URL" VITE_VIZ_API="$VIZ_API_URL" npm --prefix "$WEB" run build
run firebase deploy --only hosting --project "$PROJECT_ID" --config "$WEB/firebase.json"
