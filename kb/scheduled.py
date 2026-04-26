"""
Entrypoint for the systemd timer (daily delta scan).

Walks every configured root, runs delta_scan, writes a summary JSON to
kb/data/last_scan_<root>.json. Exits 0 on success even if some files were
skipped (skip != fatal); exits 1 only on hard errors.

Invoked by systemd:
    systemd/folderreorg-kb.service  →  python -m kb.scheduled
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from kb.config import DATA_DIR, KB_VARIANT, discover_roots
from kb.indexer import delta_scan


def main() -> int:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] "
          f"KB scheduled scan starting (variant: {KB_VARIANT})")
    # Re-discover at run time so any newly-restructured subsets get picked up
    # without a config edit / process restart.
    roots = discover_roots()
    if not roots:
        print("  (nothing to index — base folder empty or mount not up)")
        return 0
    any_error = False
    for name, path in roots:
        print(f"--- {name}  ({path}) ---")
        try:
            summary = delta_scan(name, path)
        except Exception as e:
            print(f"  FATAL: {e}")
            any_error = True
            summary = {"root": name, "fatal": str(e)}
        out = DATA_DIR / f"last_scan_{name}.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"  wrote {out}")
        print(f"  {summary}")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] done")
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
