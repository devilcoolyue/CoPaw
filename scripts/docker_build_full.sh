#!/usr/bin/env bash
# Build standalone Docker image with embedded browser (public base images).
# Run from repo root: bash scripts/docker_build_full.sh [IMAGE_TAG] [EXTRA_ARGS...]
# Example: bash scripts/docker_build_full.sh copaw:latest
#          bash scripts/docker_build_full.sh copaw:latest --no-cache
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DOCKERFILE="$REPO_ROOT/deploy/Dockerfile.full"
TAG="${1:-copaw:latest}"
shift || true

DISABLED_CHANNELS="${COPAW_DISABLED_CHANNELS:-imessage}"

echo "[docker_build_full] Building image: $TAG"
echo "[docker_build_full] Dockerfile: $DOCKERFILE"
echo "[docker_build_full] Python: 3.11 | Node: 20 | Browser: Chromium (system)"

docker build -f "$DOCKERFILE" \
    --build-arg COPAW_DISABLED_CHANNELS="$DISABLED_CHANNELS" \
    ${COPAW_ENABLED_CHANNELS:+--build-arg COPAW_ENABLED_CHANNELS="$COPAW_ENABLED_CHANNELS"} \
    -t "$TAG" "$@" .

echo ""
echo "[docker_build_full] Done: $TAG"
echo "[docker_build_full] Run: docker run -d -p 8088:8088 --name copaw $TAG"
echo "[docker_build_full] Or:  docker run -d -e COPAW_PORT=3000 -p 3000:3000 --name copaw $TAG"
