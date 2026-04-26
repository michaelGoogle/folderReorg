#!/usr/bin/env bash
# kb/mount_nas.sh — idempotent SSHFS mount of the NAS restructured tree.
#
# Mounts:  mgzh11:/volume1/Data_Michael_restructured  →  /home/michael.gerber/nas
# Read-only, with auto-reconnect. No-op if already mounted.
#
# Used by:
#   · ./kb.py mount         (manual)
#   · systemd service ExecStartPre  (so the nightly timer always sees the NAS)

set -euo pipefail

MOUNT_POINT="${KB_NAS_MOUNT:-/home/michael.gerber/nas}"
# Synology's SFTP server is chrooted to /volume1/; absolute paths starting
# with /volume1/ are rejected. Mount the SFTP default ('.') instead — this
# exposes all shared folders at the top of the mount point, so we can later
# point roots at Data_Michael_restructured/, Data_Michael/, 360F-*, etc.
REMOTE="${KB_NAS_REMOTE:-mgzh11:.}"

mkdir -p "$MOUNT_POINT"

if mountpoint -q "$MOUNT_POINT"; then
    echo "already mounted: $MOUNT_POINT"
    exit 0
fi

echo "mounting $REMOTE → $MOUNT_POINT (sshfs, read-only, tuned)"
sshfs "$REMOTE" "$MOUNT_POINT" \
    -o reconnect \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -o ro \
    -o cache_timeout=60 \
    -o IdentityFile=/home/michael.gerber/.ssh/id_ed25519 \
    -o StrictHostKeyChecking=accept-new \
    -o Compression=no \
    -o Ciphers=aes128-gcm@openssh.com \
    -o max_read=65536

# Verify
if mountpoint -q "$MOUNT_POINT"; then
    echo "OK: mounted"
else
    echo "FAIL: sshfs returned 0 but mountpoint check failed" >&2
    exit 1
fi
