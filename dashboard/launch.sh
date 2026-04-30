#!/usr/bin/env bash
# Launch the unified folder-reorg dashboard on LAN port 8500.
#
# Usage:
#   ./dashboard/launch.sh                 # foreground
#   nohup ./dashboard/launch.sh > /tmp/dashboard.log 2>&1 &
#   disown                                # detach from SSH
#
# Or in tmux:
#   tmux new -d -s dashboard ./dashboard/launch.sh
#
# Reach from your laptop / phone on the LAN:
#   http://192.168.1.10:8500
#
# (Bound to 0.0.0.0 — make sure ufw allows port 8500:
#   sudo ufw allow 8500/tcp )

set -e

# Resolve repo root from this script's location, regardless of where it's called from
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$ROOT"

VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "venv python not found at $VENV_PY" >&2
    echo "create the venv first (see docs/setup.md §4.4)" >&2
    exit 1
fi

PORT="${DASHBOARD_PORT:-8500}"
ADDR="${DASHBOARD_ADDRESS:-0.0.0.0}"

echo "Starting dashboard on http://${ADDR}:${PORT} ..."
echo "  repo root: $ROOT"
echo "  python:    $VENV_PY"
echo

exec "$VENV_PY" -m streamlit run dashboard/home.py \
    --server.address "$ADDR" \
    --server.port    "$PORT" \
    --server.headless         true \
    --browser.gatherUsageStats false
