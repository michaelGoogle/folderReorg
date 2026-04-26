"""
Folder/file exclusion rules. Per plan §1.3 + §5.3, with one real-world
adjustment: the user's archive convention is `_Archive` (underscore prefix),
not `Archive`. We match both.
"""

from __future__ import annotations

from pathlib import Path

# Case-insensitive folder names to skip at any depth.
EXCLUDED_FOLDER_NAMES_CI = {
    "archive",                       # plan convention
    "_archive",                      # user's actual convention
    "node_modules", "__pycache__", "venv", ".venv",
    "tmp", "temp", "cache", "caches",
    # Synology-specific metadata dirs (safety — we read from a local copy,
    # but just in case rsync brought any through):
    "@eadir", "#recycle",
}

EXCLUDED_FILE_NAMES_CI = {
    "thumbs.db", ".ds_store", "desktop.ini",
}


def is_excluded(p: Path) -> bool:
    """True if this file should be skipped entirely (never inventoried/copied)."""
    # Per-component checks
    for part in p.parts:
        name_ci = part.lower()
        # Hidden / dotfolders at any depth (but NOT the leaf file itself —
        # that is handled below).
        if part.startswith(".") and part != p.name:
            return True
        if name_ci in EXCLUDED_FOLDER_NAMES_CI:
            return True
    # Marker files and dotfiles at the leaf
    if p.name.lower() in EXCLUDED_FILE_NAMES_CI:
        return True
    if p.name.startswith("."):
        return True
    return False


def walk_files(root: Path):
    """Yield every non-excluded regular file under `root`."""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if is_excluded(p):
            continue
        yield p
