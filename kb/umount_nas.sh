#!/usr/bin/env bash
# kb/umount_nas.sh — unmount the SSHFS NAS mount. No-op if not mounted.
set -euo pipefail

MOUNT_POINT="${KB_NAS_MOUNT:-/home/michael.gerber/nas}"

if ! mountpoint -q "$MOUNT_POINT"; then
    echo "not mounted: $MOUNT_POINT"
    exit 0
fi

# Prefer fusermount3, fall back to fusermount
if command -v fusermount3 >/dev/null 2>&1; then
    fusermount3 -u "$MOUNT_POINT"
else
    fusermount -u "$MOUNT_POINT"
fi
echo "unmounted: $MOUNT_POINT"
