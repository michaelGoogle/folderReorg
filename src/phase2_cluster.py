"""
Phase 2b — HDBSCAN clustering on the embeddings (plan §6.3).

Writes cluster_assignments.csv with (file_id, cluster_id).
cluster_id = -1 means "noise" (HDBSCAN couldn't assign it to any cluster);
those files are handled individually by Phase 3.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import DATA_DIR, HDBSCAN_MIN_CLUSTER_SIZE, HDBSCAN_MIN_SAMPLES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb",  type=Path, default=DATA_DIR / "embeddings.npy")
    ap.add_argument("--idx",  type=Path, default=DATA_DIR / "embeddings_index.csv")
    ap.add_argument("--out",  type=Path, default=DATA_DIR / "cluster_assignments.csv")
    ap.add_argument("--min-cluster-size", type=int, default=HDBSCAN_MIN_CLUSTER_SIZE)
    ap.add_argument("--min-samples",      type=int, default=HDBSCAN_MIN_SAMPLES)
    args = ap.parse_args()

    # Lazy import — hdbscan pulls a C extension
    import hdbscan

    embs = np.load(args.emb)
    idx = pd.read_csv(args.idx)
    if len(idx) != len(embs):
        raise SystemExit(f"Index/emb length mismatch: {len(idx)} vs {len(embs)}")

    print(f"HDBSCAN on {embs.shape} "
          f"(min_cluster_size={args.min_cluster_size}, min_samples={args.min_samples}) …")
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples,
        metric="euclidean",       # normalized embeddings → euclidean ≈ cosine
        cluster_selection_method="eom",
        core_dist_n_jobs=-1,
    )
    labels = clusterer.fit_predict(embs)

    idx["cluster_id"] = labels.astype(int)
    idx.to_csv(args.out, index=False)

    n_clusters = int((labels >= 0).any()) and int(labels.max()) + 1
    n_noise = int((labels == -1).sum())
    print(f"{n_clusters} clusters, {n_noise:,} noise points ({n_noise / len(labels):.1%})")
    print("Top cluster sizes:")
    print(idx[idx["cluster_id"] >= 0]["cluster_id"].value_counts().head(20).to_string())
    print(f"OK — wrote {args.out}")


if __name__ == "__main__":
    main()
