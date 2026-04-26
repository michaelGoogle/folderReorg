"""
Central configuration. All tunables live here; per-run overrides via CLI flags
on each phase script.

Path convention:
    PROJECT_ROOT = /home/michael.gerber/folderReorg
    SOURCE_LOCAL = PROJECT_ROOT/source_local/<subset-name>   (rsync'd from NAS)
    TARGET_LOCAL = PROJECT_ROOT/target_local/<subset-name>   (grown by phase 5)
    DATA_DIR     = PROJECT_ROOT/data                         (csvs + extracted text)
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths ----------------------------------------------------------------
PROJECT_ROOT = Path(os.environ.get("FOLDERREORG_ROOT", Path(__file__).resolve().parent.parent))

DATA_DIR = PROJECT_ROOT / "data"
EXTRACTED_TEXT_DIR = DATA_DIR / "extracted_text"
LOGS_DIR = PROJECT_ROOT / "logs"

# Default source / target (override via --source / --target on each phase CLI)
DEFAULT_SOURCE = PROJECT_ROOT / "source_local" / "F-Finance"
DEFAULT_TARGET = PROJECT_ROOT / "target_local" / "F-Finance"

# --- LLM ------------------------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:14b-instruct-q4_K_M")

# Used for both cluster naming and per-file naming (single-model run, §14 note:
# if Phase 3 throughput is unacceptable on the full corpus, switch per-file
# naming to a smaller model like qwen2.5:7b-instruct).
PER_FILE_LLM_MODEL = os.environ.get("PER_FILE_LLM_MODEL", LLM_MODEL)

# --- Embeddings -----------------------------------------------------------
# Multilingual per plan §12.4. Use the sentence-transformers name (we embed via
# the HuggingFace checkpoint, not Ollama, for batched GPU throughput).
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "32"))

# --- Text extraction ------------------------------------------------------
MAX_TEXT_CHARS = 4000           # first couple of pages — §5.5
EXTRACT_WORKERS = 8

# --- Clustering -----------------------------------------------------------
HDBSCAN_MIN_CLUSTER_SIZE = 10   # tune per subset; F-Finance (2.5k files) → smaller
HDBSCAN_MIN_SAMPLES = 3

# --- Mode C depth policy (§6.2) ------------------------------------------
PRESERVE_DEPTH = 3              # depths 1..3 preserved; depth 4+ may be clustered

# --- Date reconciliation (§1.3) ------------------------------------------
CONTENT_DATE_DEVIATION_MONTHS = 6

# --- Versioning -----------------------------------------------------------
DEFAULT_VERSION_UNVERSIONED = "V0-01"

# --- Exclusions (see src/exclusions.py) ---------------------------------
# Core list locked in §1.3; note the user's archive convention uses `_Archive`
# with an underscore (see src/exclusions.py).

# --- Sanity --------------------------------------------------------------
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
