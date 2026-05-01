#!/usr/bin/env bash
# Build, push, and deploy machwave-api + worker for the given environment.
# Usage: scripts/deploy.sh <env> <tag>
#   env: prod
#   tag: docker image tag (e.g. release tag)
#
# Assumes the caller is already authenticated to gcloud and Docker is configured
# for the GCR registry (gcloud auth configure-docker).

set -euo pipefail

ENV="${1:?usage: deploy.sh <env> <tag>}"
TAG="${2:?usage: deploy.sh <env> <tag>}"

CONFIG="deploy/${ENV}/config.yaml"
SERVICE="deploy/${ENV}/service.yaml"
WORKER_SERVICE="deploy/${ENV}/worker-service.yaml"

for f in "$CONFIG" "$SERVICE" "$WORKER_SERVICE"; do
  [[ -f "$f" ]] || { echo "missing: $f" >&2; exit 1; }
done

GCP_PROJECT_ID=$(awk -F': *' '/^GCP_PROJECT_ID:/ {print $2}' "$CONFIG")
GCP_REGION=$(awk -F': *' '/^GCP_REGION:/ {print $2}' "$CONFIG")

[[ -n "$GCP_PROJECT_ID" && -n "$GCP_REGION" ]] || {
  echo "GCP_PROJECT_ID/GCP_REGION not found in $CONFIG" >&2; exit 1;
}

IMAGE_API="gcr.io/${GCP_PROJECT_ID}/machwave-api-${ENV}:${TAG}"
IMAGE_WORKER="gcr.io/${GCP_PROJECT_ID}/machwave-worker-${ENV}:${TAG}"

# Cloud Run runs linux/amd64. On Apple Silicon, plain `docker build` produces
# arm64 images, which Cloud Run rejects with a misleading "Image not found".
# `--provenance=false` strips attestation manifests, which some pull paths
# choke on; harmless for our use.
BUILD_FLAGS="--platform linux/amd64 --provenance=false"

echo "==> Building and pushing API image: $IMAGE_API"
docker buildx build $BUILD_FLAGS -t "$IMAGE_API" --push .

echo "==> Building and pushing worker image: $IMAGE_WORKER"
docker buildx build $BUILD_FLAGS -f Dockerfile.worker -t "$IMAGE_WORKER" --push .

echo "==> Deploying worker service to Cloud Run ($GCP_REGION)"
sed "s|IMAGE_PLACEHOLDER|${IMAGE_WORKER}|g" "$WORKER_SERVICE" \
  | gcloud run services replace - --region "$GCP_REGION"

echo "==> Deploying API service to Cloud Run ($GCP_REGION)"
sed "s|IMAGE_PLACEHOLDER|${IMAGE_API}|g" "$SERVICE" \
  | gcloud run services replace - --region "$GCP_REGION"
