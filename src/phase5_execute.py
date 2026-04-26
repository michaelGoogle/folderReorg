"""
Phase 5 — execute the approved plan by COPYING (not renaming) from source to
a new target tree (plan §9).

Runs on aizh (no Docker, no NAS executor). The source and target are both local
paths on aizh; the restructured target is rsync'd back to the NAS by the caller
after this phase completes cleanly.

Safety guarantees:
- Never modifies the source tree.
- SHA-256 verifies every copy before moving to the next row.
- Applies RULE 4 minor-version bump if a target name collides in the new tree.
- Idempotent — if execution_log already shows a row as `ok`, it's skipped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from src.config import DATA_DIR, DEFAULT_TARGET


def sha256(p: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def copy_and_hash(src: Path, dst: Path, buf: int = 1 << 20) -> str:
    """
    Copy `src` → `dst` while hashing the source bytes in flight.
    Returns the sha256 hex of the source content.

    Replaces the previous `shutil.copy2(src, dst)` + `sha256(src)` pattern,
    which re-read the entire source file a second time just to hash it.
    That second read is fine on local SSD (doubles → nothing), but on an
    SSHFS-mounted NAS it literally doubles the network load for Phase 5.
    Hashing during the copy keeps source reads to one pass.

    Metadata (mtime + perms) is preserved after write, matching shutil.copy2.
    """
    h = hashlib.sha256()
    with src.open("rb") as fi, dst.open("wb") as fo:
        while chunk := fi.read(buf):
            h.update(chunk)
            fo.write(chunk)
    try:
        shutil.copystat(src, dst)
    except Exception:
        pass  # best-effort metadata copy; content is what matters
    return h.hexdigest()


def unique_target(dst: Path) -> Path:
    """Apply RULE 4 minor-version bump if `dst` already exists."""
    if not dst.exists():
        return dst
    m = re.search(r"V(\d+)-(\d+)", dst.name)
    if not m:
        # Fall back to suffix counter
        i = 1
        while True:
            cand = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
            if not cand.exists():
                return cand
            i += 1
    major = int(m.group(1))
    minor = int(m.group(2))
    cand = dst
    while cand.exists():
        minor += 1
        cand = dst.with_name(re.sub(r"V\d+-\d+", f"V{major}-{minor:02d}", dst.name, count=1))
    return cand


def _load_existing_log(log_path: Path) -> dict[str, str]:
    """Return {file_id: status} for resume support."""
    if not log_path.exists():
        return {}
    prev = pd.read_csv(log_path)
    return dict(zip(prev["file_id"], prev["status"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan",   type=Path, default=DATA_DIR / "rename_plan_approved.csv",
                    help="Approved plan CSV (fall back to rename_plan.csv if absent)")
    ap.add_argument("--target", type=Path, default=DEFAULT_TARGET,
                    help="Target tree root (will be created; files go under proposed_parent)")
    ap.add_argument("--log",    type=Path, default=DATA_DIR / "execution_log.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan_path = args.plan if args.plan.exists() else DATA_DIR / "rename_plan.csv"
    if not plan_path.exists():
        raise SystemExit(f"no plan: tried {args.plan} and {DATA_DIR / 'rename_plan.csv'}")
    plan = pd.read_csv(plan_path)
    if "decision" not in plan.columns:
        plan["decision"] = "approve"
    plan = plan[plan["decision"] == "approve"].copy()
    print(f"Plan: {len(plan):,} rows approved from {plan_path}")

    args.target.mkdir(parents=True, exist_ok=True)
    already = _load_existing_log(args.log)

    # Pre-create all target directories once
    tgt_dirs = {args.target / str(r["proposed_parent"]) for _, r in plan.iterrows()}
    if not args.dry_run:
        for d in sorted(tgt_dirs, key=lambda p: len(p.parts)):
            d.mkdir(parents=True, exist_ok=True)
    else:
        print(f"[dry-run] would mkdir {len(tgt_dirs)} target dirs")

    log_rows: list[dict] = []
    for r in tqdm(plan.to_dict("records"), unit="file", desc="copy"):
        fid = r["file_id"]
        if already.get(fid) == "ok":
            log_rows.append({"file_id": fid, "status": "ok", "final_path": "", "error": "skip-resume"})
            continue

        src = Path(r["current_path"])
        tgt_dir = args.target / str(r["proposed_parent"])
        dst = unique_target(tgt_dir / str(r["proposed_name"]))

        if args.dry_run:
            log_rows.append({"file_id": fid, "status": "dry", "final_path": str(dst), "error": ""})
            continue

        try:
            if not src.exists():
                raise FileNotFoundError(str(src))
            src_hash = copy_and_hash(src, dst)   # single source read
            if src_hash != sha256(dst):
                dst.unlink(missing_ok=True)
                raise IOError("hash mismatch after copy")
            log_rows.append({"file_id": fid, "status": "ok", "final_path": str(dst), "error": ""})
        except Exception as e:
            log_rows.append({"file_id": fid, "status": "error", "final_path": str(dst), "error": str(e)})

    # Append to the log (preserve prior rows; this script is idempotent)
    log_exists = args.log.exists()
    with args.log.open("a" if log_exists else "w", newline="", encoding="utf-8") as w:
        writer = csv.DictWriter(w, fieldnames=["file_id", "status", "final_path", "error"])
        if not log_exists:
            writer.writeheader()
        writer.writerows(log_rows)

    df = pd.DataFrame(log_rows)
    print(df["status"].value_counts().to_string())
    print(f"OK — log at {args.log}")


if __name__ == "__main__":
    main()
