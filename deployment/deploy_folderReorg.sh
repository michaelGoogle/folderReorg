#!/usr/bin/env bash
set -euo pipefail

# folderReorg server deployment script.
# Expected to run on aizh in the deployment directory after sync.

DEPLOY_DIR="${FOLDERREORG_DEPLOY_DIR:-/home/michael.gerber/folderReorg}"
COMPOSE_FILE="${COMPOSE_FILE:-docker/compose.app.yml}"
TAR_FILE="${TAR_FILE:-folderReorg-project.tar}"

# Host-exposed ports (keep in 8051..8060 range)
CHAT_PERSONAL_HOST_PORT="${CHAT_PERSONAL_HOST_PORT:-8052}"
CHAT_360F_HOST_PORT="${CHAT_360F_HOST_PORT:-8053}"
RUNPY_REVIEW_URL="${RUNPY_REVIEW_URL:-http://127.0.0.1:8051}"

echo "========================================"
echo "folderReorg - Server Deployment"
echo "========================================"
echo "Deploy dir: ${DEPLOY_DIR}"
echo "Compose:    ${COMPOSE_FILE}"
echo "Ports:      review=8051 personal=${CHAT_PERSONAL_HOST_PORT} 360f=${CHAT_360F_HOST_PORT}"
echo

mkdir -p "${DEPLOY_DIR}"
cd "${DEPLOY_DIR}"

echo "[STEP 1] Extracting synced tar (if present)..."
if [ -f "${TAR_FILE}" ]; then
  tar -xf "${TAR_FILE}" --no-same-owner 2>/dev/null || tar -xf "${TAR_FILE}"
  echo "[OK] Extracted ${TAR_FILE}"
else
  echo "[INFO] No tar file found, using existing files"
fi

if [ ! -f "${COMPOSE_FILE}" ]; then
  echo "[ERROR] Missing compose file: ${COMPOSE_FILE}"
  exit 1
fi

echo "[STEP 2] Docker sanity check..."
docker info >/dev/null
echo "[OK] Docker is available"

echo "[STEP 3] Build shared app image..."
docker compose -f "${COMPOSE_FILE}" build folderreorg-pipeline

echo "[STEP 4] Start Qdrant (unchanged stack)..."
docker compose -f "${COMPOSE_FILE}" up -d qdrant-personal qdrant-360f

echo "[STEP 5] Start chat services on 805x host ports..."
CHAT_PERSONAL_HOST_PORT="${CHAT_PERSONAL_HOST_PORT}" \
CHAT_360F_HOST_PORT="${CHAT_360F_HOST_PORT}" \
RUNPY_REVIEW_URL="${RUNPY_REVIEW_URL}" \
docker compose -f "${COMPOSE_FILE}" up -d \
  folderreorg-chat-personal \
  folderreorg-chat-360f

echo "[STEP 6] Start container-native KB schedulers..."
docker compose -f "${COMPOSE_FILE}" up -d \
  folderreorg-kb-scheduler-personal \
  folderreorg-kb-scheduler-360f

echo "[STEP 7] Cleanup dangling images (best effort)..."
docker image prune -f >/dev/null 2>&1 || true

echo
echo "========================================"
echo "DEPLOYMENT COMPLETED"
echo "========================================"
echo "Personal chat: http://127.0.0.1:${CHAT_PERSONAL_HOST_PORT}"
echo "360F chat:     http://127.0.0.1:${CHAT_360F_HOST_PORT}"
echo
echo "Useful commands:"
echo "  docker compose -f ${COMPOSE_FILE} ps"
echo "  docker compose -f ${COMPOSE_FILE} logs --tail=100 folderreorg-chat-personal"
echo "  docker compose -f ${COMPOSE_FILE} logs --tail=100 folderreorg-chat-360f"
