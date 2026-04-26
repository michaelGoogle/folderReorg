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
        # One-line summary on stdout; the per-file errors / skipped lists
        # would otherwise blow up to thousands of lines and bury the rest
        # of the scan output. Full detail is in the JSON file (and via
        # `./status.py --errors --skipped --root <NAME>` for nicer output).
        if "fatal" in summary:
            print(f"  ✗ FATAL — see {out}")
        else:
            n_errors = len(summary.get("errors", []) or [])
            n_skipped_listed = len(summary.get("skipped", []) or [])
            n_skipped_overflow = int(summary.get("skipped_overflow", 0) or 0)
            print(
                f"  → new={summary.get('new', 0)} "
                f"updated={summary.get('updated', 0)} "
                f"unchanged={summary.get('unchanged', 0)} "
                f"deleted={summary.get('deleted', 0)} "
                f"skip={summary.get('skip', 0)} "
                f"chunks_added={summary.get('chunks_added', 0)} "
                f"errors={n_errors}"
            )
            print(f"  → details in: {out}")
            if n_errors or n_skipped_listed:
                hint = []
                if n_errors:
                    hint.append(f"{n_errors} error(s)")
                if n_skipped_listed:
                    overflow = (f" + {n_skipped_overflow} overflow"
                                if n_skipped_overflow else "")
                    hint.append(f"{n_skipped_listed} skipped record(s){overflow}")
                print(f"     ({', '.join(hint)} — see "
                      f"./status.py --detail --root {name})")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] done")
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
