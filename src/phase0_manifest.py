"""
Phase 0 — freeze the source and record a baseline SHA-256 manifest (plan §4).

The source stays read-only. This manifest is what Phase 6 uses to prove the
copies in the new tree are byte-identical to the originals.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tqdm import tqdm

from src.config import DATA_DIR, DEFAULT_SOURCE
from src.exclusions import walk_files


def sha256(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def _row(args: tuple[Path, Path]) -> tuple[str, int, str]:
    root, p = args
    st = p.stat()
    return (str(p.relative_to(root)), st.st_size, sha256(p))


def build_manifest(source: Path, out: Path, workers: int = 8) -> int:
    """Write a source_manifest.csv (rel_path, size_bytes, sha256). Returns file count."""
    paths = list(walk_files(source))
    print(f"Manifesting {len(paths):,} files under {source}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as w:
        writer = csv.writer(w)
        writer.writerow(["rel_path", "size_bytes", "sha256"])
        with ProcessPoolExecutor(max_workers=workers) as pool:
            args = [(source, p) for p in paths]
            for r in tqdm(pool.map(_row, args, chunksize=50), total=len(paths), unit="file"):
                writer.writerow(r)
    return len(paths)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help="Local source root (usually rsync'd mirror of NAS folder)")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "source_manifest.csv")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if not args.source.is_dir():
        raise SystemExit(f"source not found: {args.source}")
    n = build_manifest(args.source, args.out, workers=args.workers)
    print(f"OK — wrote {n:,} rows to {args.out}")


if __name__ == "__main__":
    main()
