"""
Knowledge-base configuration.

Two physically-separate KB stacks share this module. The active variant is
selected by the `KB_VARIANT` environment variable:

    KB_VARIANT=personal   →  Qdrant on :6333, indexes Personal/* subsets
    KB_VARIANT=360f       →  Qdrant on :6433, indexes 360F/* subsets

Each variant has its own Qdrant container, its own data volume, its own
collection name, its own systemd timer, and its own Streamlit chat URL.
The two stacks never share state.

All other tunables (chunking, OCR languages, embedding model, retrieval k)
are common to both variants.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Repo layout -----------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent

# --- HuggingFace offline (same autotoggle as phase2_embed.py) -------------
# Enables if bge-m3 is already cached; skips the HF Hub metadata ping.
_HF_CACHE = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
if (_HF_CACHE / "models--BAAI--bge-m3").exists() and "HF_HUB_OFFLINE" not in os.environ:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# --- NAS mount (shared across variants) ----------------------------------
# We use SSHFS to mount the NAS read-only on aizh, so the indexer always
# sees the canonical NAS state (changes you drop in via DSM/SMB are picked
# up on the next scan with no rsync needed).
#
# NAS_MOUNT points to the SFTP chroot root (= NAS /volume1/). All shared
# folders are visible directly under this path. Mount details: see
# kb/mount_nas.sh (invoked by systemd ExecStartPre and `./kb.py mount`).
NAS_MOUNT = Path(os.environ.get("KB_NAS_MOUNT", "/home/michael.gerber/nas"))


# --- Variant selection ----------------------------------------------------
KB_VARIANT = os.environ.get("KB_VARIANT", "personal").lower()
if KB_VARIANT not in ("personal", "360f"):
    raise SystemExit(
        f"KB_VARIANT must be 'personal' or '360f' (got {KB_VARIANT!r}). "
        f"Set via env var or `./kb.py --variant ...`."
    )

# Per-variant base on the NAS mount (auto-discovery walks this).
PERSONAL_BASE = NAS_MOUNT / "Data_Michael_restructured" / "Personal"
SIXTYF_BASE   = NAS_MOUNT / "Data_Michael_restructured" / "360F"

_PERSONAL = {
    "label":      "Personal",
    "qdrant_port": 6333,
    "ui_port":     8502,
    "collection":  "folderreorg_personal",
    "data_subdir": "personal",
    "color":       "#2E7D32",   # green
    "base":        PERSONAL_BASE,
}

_360F = {
    "label":      "360F",
    "qdrant_port": 6433,
    "ui_port":     8503,
    "collection":  "folderreorg_360f",
    "data_subdir": "360f",
    "color":       "#1565C0",   # blue
    "base":        SIXTYF_BASE,
}

_VARIANT = _PERSONAL if KB_VARIANT == "personal" else _360F


# Folder names we never index (Synology metadata, hidden dirs, etc.)
_SKIP_NAMES = {"@eaDir", "#recycle", ".DS_Store", "lost+found"}


def discover_roots() -> list[tuple[str, Path]]:
    """
    Scan the active variant's base folder and return [(slug, abs_path), ...]
    for every directory under it. Auto-discovers any subset you've restructured
    via the pipeline — no need to edit this file when you add a new subset.

    Personal scans:  /home/michael.gerber/nas/Data_Michael_restructured/Personal/*
    360F     scans:  /home/michael.gerber/nas/Data_Michael_restructured/360F/*

    Returns [] if the mount isn't up or the base doesn't exist; callers should
    treat that as "nothing to index right now" rather than an error.
    """
    base: Path = _VARIANT["base"]
    if not base.exists() or not base.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    try:
        for p in sorted(base.iterdir()):
            if not p.is_dir():
                continue
            if p.name in _SKIP_NAMES or p.name.startswith((".", "@", "#")):
                continue
            out.append((p.name, p))
    except OSError:
        # NAS unreachable, mount stale, etc. Caller treats as empty.
        return []
    return out


# Initial best-effort population at import time. Long-lived processes (like
# Streamlit) should call discover_roots() at use time to pick up new subsets
# added after import.
DEFAULT_ROOTS: list[tuple[str, Path]] = discover_roots()

# --- Qdrant ----------------------------------------------------------------
QDRANT_URL = os.environ.get("QDRANT_URL",
                            f"http://localhost:{_VARIANT['qdrant_port']}")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", _VARIANT["collection"])

# --- Streamlit chat UI -----------------------------------------------------
UI_PORT = int(os.environ.get("KB_UI_PORT", _VARIANT["ui_port"]))
UI_LABEL = _VARIANT["label"]
UI_COLOR = _VARIANT["color"]

# --- Per-variant data dir (last_scan summaries, etc.) ---------------------
DATA_DIR = ROOT / "kb" / "data" / _VARIANT["data_subdir"]
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- Embedding model (shared) ---------------------------------------------
EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "BAAI/bge-m3")
EMBED_DIM = 1024
EMBED_BATCH_SIZE = int(os.environ.get("KB_EMBED_BATCH", "32"))

# --- Chunking (shared) ----------------------------------------------------
CHUNK_CHARS = int(os.environ.get("KB_CHUNK_CHARS", "2000"))
CHUNK_OVERLAP_CHARS = int(os.environ.get("KB_CHUNK_OVERLAP", "200"))

# --- Extraction / OCR (shared) -------------------------------------------
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024   # skip files > 500 MB
OCR_ENABLED = os.environ.get("KB_OCR_ENABLED", "1") != "0"
OCR_LANGS = os.environ.get("KB_OCR_LANGS", "deu+eng")
OCR_DPI = int(os.environ.get("KB_OCR_DPI", "200"))
OCR_WORKERS = int(os.environ.get("KB_OCR_WORKERS", "4"))

TEXT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".xls",
             ".txt", ".md", ".csv", ".rtf"}
OCR_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp", ".heic"}

# --- LLM / Ollama (shared) ------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.environ.get("KB_LLM_MODEL", "qwen2.5:14b-instruct-q4_K_M")

# --- Retrieval (shared) ---------------------------------------------------
TOP_K = int(os.environ.get("KB_TOP_K", "10"))
