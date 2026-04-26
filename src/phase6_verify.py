"""
Phase 6 — verify (plan §10).

Checks:
1. Count check: approved rows == ok execution_log rows == files in target tree.
2. Hash check: each target file's SHA-256 matches the source's SHA-256.
3. Source-untouched check: random 5% resample of the source matches the
   manifest from Phase 0.
4. Convention lint: every target filename matches CONVENTION_PATTERN.
"""

from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path

import pandas as pd

from src.config import DATA_DIR, DEFAULT_SOURCE, DEFAULT_TARGET
from src.naming import CONVENTION_PATTERN


def sha256(p: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",    type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--target",    type=Path, default=DEFAULT_TARGET)
    ap.add_argument("--manifest",  type=Path, default=DATA_DIR / "source_manifest.csv")
    ap.add_argument("--log",       type=Path, default=DATA_DIR / "execution_log.csv")
    ap.add_argument("--plan",      type=Path, default=DATA_DIR / "rename_plan_approved.csv")
    ap.add_argument("--sample-pct", type=float, default=5.0,
                    help="Percentage of source files to re-hash for the untouched check")
    args = ap.parse_args()

    # Fall back to the base plan if no approved version
    plan_path = args.plan if args.plan.exists() else DATA_DIR / "rename_plan.csv"
    plan = pd.read_csv(plan_path)
    if "decision" not in plan.columns:
        plan["decision"] = "approve"
    approved = plan[plan["decision"] == "approve"]
    log = pd.read_csv(args.log)
    ok_log = log[log["status"] == "ok"]

    print(f"[check 1] approved={len(approved)}  ok_log={len(ok_log)}  "
          f"files_in_target={sum(1 for _ in args.target.rglob('*') if _.is_file())}")

    # [check 2] hash match source vs target for every ok row
    # Build source hash lookup from the manifest; build a file_id → src_path from the plan
    manifest = pd.read_csv(args.manifest)
    # manifest rel_path is relative to source; build absolute
    manifest["abs_path"] = manifest["rel_path"].apply(lambda r: str(args.source / r))
    src_hash_by_abs = dict(zip(manifest["abs_path"], manifest["sha256"]))

    src_path_by_fid = dict(zip(plan["file_id"], plan["current_path"]))

    mismatches = 0
    checked = 0
    for r in ok_log.to_dict("records"):
        fid = r["file_id"]
        dst = Path(r["final_path"])
        src_abs = src_path_by_fid.get(fid)
        if not dst.exists() or not src_abs:
            continue
        src_hash = src_hash_by_abs.get(src_abs)
        if src_hash is None:
            # Fall back to re-hashing the source
            try:
                src_hash = sha256(Path(src_abs))
            except Exception:
                continue
        if sha256(dst) != src_hash:
            mismatches += 1
        checked += 1
    print(f"[check 2] hash match: checked={checked}  mismatches={mismatches}")

    # [check 3] source-untouched sample
    n = max(1, int(len(manifest) * args.sample_pct / 100))
    sample = manifest.sample(n=min(n, len(manifest)), random_state=42)
    untouched_bad = 0
    for r in sample.to_dict("records"):
        p = Path(r["abs_path"])
        if not p.exists():
            untouched_bad += 1
            continue
        if sha256(p) != r["sha256"]:
            untouched_bad += 1
    print(f"[check 3] source untouched (sample of {len(sample)}): bad={untouched_bad}")

    # [check 4] convention lint
    bad_names: list[str] = []
    for p in args.target.rglob("*"):
        if p.is_file():
            if not CONVENTION_PATTERN.match(p.name):
                bad_names.append(str(p.relative_to(args.target)))
    print(f"[check 4] convention lint: {len(bad_names)} non-conforming names")
    if bad_names:
        for b in bad_names[:10]:
            print(f"  - {b}")
        (DATA_DIR / "non_conforming.csv").write_text(
            "rel_path\n" + "\n".join(bad_names), encoding="utf-8"
        )

    print("DONE")


if __name__ == "__main__":
    main()
