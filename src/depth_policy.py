"""
Mode C depth policy (plan §6.2): preserve top levels, cluster below.
"""

from __future__ import annotations

from pathlib import Path

from src.config import PRESERVE_DEPTH


def policy_for(rel_path: Path) -> str:
    """Return 'preserve' or 'cluster' for the folder containing this file."""
    depth = len(rel_path.parts) - 1  # parts include the filename
    return "preserve" if depth <= PRESERVE_DEPTH else "cluster"


def preserved_parent(rel_path: Path) -> Path | None:
    """
    Return the preserved ancestor folder (the one at PRESERVE_DEPTH) for a file.
    None if the file itself is inside a preserved folder.
    """
    parts = rel_path.parts[:-1]  # drop filename
    if len(parts) <= PRESERVE_DEPTH:
        return Path(*parts) if parts else None
    return Path(*parts[:PRESERVE_DEPTH])
