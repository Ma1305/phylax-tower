#!/usr/bin/env bash
# Build, push and deploy the two Hold Short APIs to Cloud Run.
#   deploy/cloudrun.sh [--dry-run]
# Defaults: project ambient-handler-460806-t7, region us-east4 (override with PROJECT_ID, REGION, ...).
# See deploy/README.md for what must exist first. Prints the two service URLs at the end.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

PROJECT_ID="${PROJECT_ID:-ambient-handler-460806-t7}"
REGION="${REGION:-us-east4}"
FIREBASE_PROJECT="${FIREBASE_PROJECT:-hackathon-d0bf5}"   # Firebase Hosting project of the web app

REPO="${AR_REPO:-holdshort}"                       # Artifact Registry docker repository
TAG="${TAG:-$(git -C "$(dirname "$0")/.." rev-parse --short HEAD 2>/dev/null || echo latest)}"
DOCKER="${DOCKER:-docker}"                         # e.g. DOCKER="sudo docker"
REGISTRY="${REGISTRY:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}}"
TOWER_IMAGE="${REGISTRY}/tower:${TAG}"
VIZ_IMAGE="${REGISTRY}/viz-api:${TAG}"

# Secret Manager secret names (values never pass through this script).
SECRET_MONGODB_URI="${SECRET_MONGODB_URI:-holdshort-mongodb-uri}"
SECRET_LLM_API_KEY="${SECRET_LLM_API_KEY:-holdshort-llm-api-key}"
SECRET_ELEVENLABS_API_KEY="${SECRET_ELEVENLABS_API_KEY:-holdshort-elevenlabs-api-key}"

# Plain variables.
MONGODB_DB="${MONGODB_DB:-holdshort_v2}"
VOICE_ENABLED="${VOICE_ENABLED:-false}"
CORS_ORIGINS="${CORS_ORIGINS:-https://${FIREBASE_PROJECT}.web.app,https://${FIREBASE_PROJECT}.firebaseapp.com}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT-holdshort-run@${PROJECT_ID}.iam.gserviceaccount.com}"  # runtime service account; SERVICE_ACCOUNT= to use the default
VPC_FLAGS="${VPC_FLAGS:-}"                         # optional, e.g. "--network=default --subnet=default --vpc-egress=all-traffic"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

SA_FLAG=()
[ -n "$SERVICE_ACCOUNT" ] && SA_FLAG=(--service-account "$SERVICE_ACCOUNT")
# shellcheck disable=SC2206
EXTRA=($VPC_FLAGS)

# gcloud's --set-env-vars splits on commas; use a custom delimiter because CORS_ORIGINS has commas.
ENV_VARS="^@^MONGODB_DB=${MONGODB_DB}@VOICE_ENABLED=${VOICE_ENABLED}@CORS_ORIGINS=${CORS_ORIGINS}"

# --- images (SKIP_BUILD=1 when they were built with deploy/cloudbuild.yaml) ---------------------
if [ "${SKIP_BUILD:-0}" != 1 ]; then
  run $DOCKER build -f "$ROOT/deploy/backend.Dockerfile" -t "$TOWER_IMAGE" "$ROOT"
  run $DOCKER push "$TOWER_IMAGE"
  run $DOCKER build -f "$ROOT/deploy/viz-api.Dockerfile" -t "$VIZ_IMAGE" "$ROOT"
  run $DOCKER push "$VIZ_IMAGE"
fi

# --- tower: always-on workers + WebSockets -> one warm instance, CPU always allocated --------
run gcloud run deploy holdshort-tower \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$TOWER_IMAGE" --port 8000 \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --timeout 3600 --session-affinity \
  --cpu 2 --memory 2Gi \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "MONGODB_URI=${SECRET_MONGODB_URI}:latest,LLM_API_KEY=${SECRET_LLM_API_KEY}:latest,ELEVENLABS_API_KEY=${SECRET_ELEVENLABS_API_KEY}:latest" \
  "${SA_FLAG[@]}" "${EXTRA[@]}"

# --- viz-api: read-only, request scoped apart from its WebSocket ------------------------------
run gcloud run deploy holdshort-viz-api \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$VIZ_IMAGE" --port 8100 \
  --allow-unauthenticated \
  --timeout 3600 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "MONGODB_URI=${SECRET_MONGODB_URI}:latest" \
  "${SA_FLAG[@]}" "${EXTRA[@]}"

# --- URLs -------------------------------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  run gcloud run services describe holdshort-tower --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
  run gcloud run services describe holdshort-viz-api --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
  echo "TOWER_URL=<printed by the command above>"
  echo "VIZ_API_URL=<printed by the command above>"
else
  TOWER_URL="$(gcloud run services describe holdshort-tower --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')"
  VIZ_API_URL="$(gcloud run services describe holdshort-viz-api --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')"
  echo "TOWER_URL=${TOWER_URL}"
  echo "VIZ_API_URL=${VIZ_API_URL}"
fi
