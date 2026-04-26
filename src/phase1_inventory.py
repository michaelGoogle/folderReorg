"""
Phase 1a — walk the source tree and build inventory.csv (plan §5.2, §5.4).

One row per non-excluded file. Stable file_id = first 16 hex of SHA-256(abs_path).
`yymm` is derived from mtime here (may be overridden by content-date during
Phase 3 classification — see plan §1.3).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from src.config import DATA_DIR, DEFAULT_SOURCE
from src.exclusions import walk_files


def file_id(abs_path: Path) -> str:
    return hashlib.sha256(str(abs_path).encode("utf-8")).hexdigest()[:16]


def yymm_of(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d.year % 100:02d}{d.month:02d}"


def build_inventory(source: Path, out: Path) -> int:
    paths = list(walk_files(source))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as w:
        writer = csv.writer(w)
        writer.writerow([
            "file_id", "abs_path", "rel_path", "parent_dir",
            "filename", "ext", "size_bytes", "mtime", "yymm",
        ])
        for p in tqdm(paths, unit="file", desc="inventory"):
            st = p.stat()
            writer.writerow([
                file_id(p),
                str(p),
                str(p.relative_to(source)),
                str(p.parent),
                p.name,
                p.suffix.lower(),
                st.st_size,
                datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                yymm_of(st.st_mtime),
            ])
    return len(paths)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out", type=Path, default=DATA_DIR / "inventory.csv")
    args = ap.parse_args()
    if not args.source.is_dir():
        raise SystemExit(f"source not found: {args.source}")
    n = build_inventory(args.source, args.out)
    print(f"OK — {n:,} files inventoried → {args.out}")


if __name__ == "__main__":
    main()
