"""
Phase 2a — embed extracted texts (plan §6.2) using bge-m3 (multilingual).

Writes:
    data/embeddings.npy        float32 [N, D]
    data/embeddings_index.csv  one column: file_id, in the same order as embeddings.npy
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# --- Auto-enable HuggingFace offline mode when the model is already cached ----
# This MUST run before importing sentence-transformers / huggingface_hub so they
# pick up the env var at module load.
#
# Effect: silences the
#   "You are sending unauthenticated requests to the HF Hub …"
# warning, and skips the tiny version-check HTTP call that sentence-transformers
# makes on every load. If the cache is missing (first run on a fresh machine),
# we keep online mode so the first download still works.
_HF_CACHE = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
if (_HF_CACHE / "models--BAAI--bge-m3").exists() and "HF_HUB_OFFLINE" not in os.environ:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np
import pandas as pd

from src.config import DATA_DIR, EMBED_BATCH_SIZE, EMBED_MODEL


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extraction", type=Path, default=DATA_DIR / "extraction_results.csv")
    ap.add_argument("--out-emb",    type=Path, default=DATA_DIR / "embeddings.npy")
    ap.add_argument("--out-idx",    type=Path, default=DATA_DIR / "embeddings_index.csv")
    ap.add_argument("--model",      type=str,  default=EMBED_MODEL)
    ap.add_argument("--batch-size", type=int,  default=EMBED_BATCH_SIZE)
    ap.add_argument("--device",     type=str,  default="cuda")
    args = ap.parse_args()

    # Lazy imports — sentence-transformers pulls torch and is heavy
    from sentence_transformers import SentenceTransformer

    ext = pd.read_csv(args.extraction)
    ok = ext[ext["status"] == "ok"].reset_index(drop=True)
    if ok.empty:
        raise SystemExit("No ok-status files to embed. Run phase1_extract first.")

    texts = [Path(p).read_text(encoding="utf-8") for p in ok["text_path"]]

    print(f"Loading embedding model {args.model} on {args.device} …")
    model = SentenceTransformer(args.model, device=args.device)

    print(f"Embedding {len(texts):,} docs (batch={args.batch_size}) …")
    embs = model.encode(
        texts,
        batch_size=args.batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32)

    np.save(args.out_emb, embs)
    ok[["file_id"]].to_csv(args.out_idx, index=False)
    print(f"OK — {embs.shape} → {args.out_emb}")


if __name__ == "__main__":
    main()
