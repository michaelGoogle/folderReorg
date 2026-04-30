#!/usr/bin/env bash
set -euo pipefail

REINDEX_AT="${REINDEX_AT:-02:00}"
KB_VARIANT="${KB_VARIANT:-personal}"

echo "[scheduler] variant=${KB_VARIANT} daily_at=${REINDEX_AT}"

while true; do
  now_epoch="$(date +%s)"
  target_epoch="$(date -d "today ${REINDEX_AT}" +%s)"

  if [ "${target_epoch}" -le "${now_epoch}" ]; then
    target_epoch="$(date -d "tomorrow ${REINDEX_AT}" +%s)"
  fi

  sleep_seconds="$((target_epoch - now_epoch))"
  echo "[scheduler] sleeping ${sleep_seconds}s until $(date -d "@${target_epoch}" '+%Y-%m-%d %H:%M:%S')"
  sleep "${sleep_seconds}"

  echo "[scheduler] running kb.py --variant ${KB_VARIANT} reindex at $(date '+%Y-%m-%d %H:%M:%S')"
  python kb.py --variant "${KB_VARIANT}" reindex || true
done
