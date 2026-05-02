#!/usr/bin/env python3
"""
Knowledge-base CLI.

USAGE
  ./kb.py setup                        start Qdrant + create collection
  ./kb.py index [--root NAME] [--path P]   initial full index
  ./kb.py reindex                      delta scan (what the systemd timer runs)
  ./kb.py query "what is ..."          ad-hoc RAG query from the terminal
  ./kb.py chat                         launch Streamlit chat UI on :8502
  ./kb.py status                       collection stats + last-scan summary
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


HELP_EPILOG = """\
Environment overrides (set before invocation):
  QDRANT_URL          default: http://localhost:6333
  QDRANT_COLLECTION   default: folderreorg
  KB_LLM_MODEL        default: qwen2.5:14b-instruct-q4_K_M
  KB_EMBED_MODEL      default: BAAI/bge-m3
  KB_CHUNK_CHARS      default: 2000
  KB_OCR_LANGS        default: deu+eng
  KB_OCR_ENABLED      0 to disable OCR (PDFs + images)

Systemd timer (nightly 02:00):
  systemctl --user status  folderreorg-kb.timer
  systemctl --user list-timers  folderreorg-kb.timer
  journalctl --user -u folderreorg-kb.service --since today
"""


def cmd_setup(args) -> int:
    """
    Start BOTH Qdrant containers (docker-compose) and create BOTH collections.
    The --variant flag is ignored for setup; we always bring up the full stack.
    """
    compose = Path(__file__).resolve().parent / "docker" / "qdrant" / "docker-compose.yml"
    if not compose.exists():
        print(f"missing {compose}", file=sys.stderr)
        return 1
    print("Starting Qdrant containers via docker-compose …")
    rc = subprocess.call(["docker", "compose", "-f", str(compose), "up", "-d"])
    if rc != 0:
        return rc

    # Ensure each variant's collection exists. We re-import kb.indexer per
    # variant by mutating KB_VARIANT and forcing a fresh import each time.
    import importlib
    for variant in ("personal", "360f"):
        os.environ["KB_VARIANT"] = variant
        # Drop any cached kb.* modules so they re-read the new variant
        for mod in list(sys.modules):
            if mod.startswith("kb."):
                del sys.modules[mod]
        from kb.config import QDRANT_COLLECTION, QDRANT_URL
        from kb.indexer import ensure_collection
        print(f"  → ensuring collection {QDRANT_COLLECTION!r} on {QDRANT_URL}")
        ensure_collection()
    print("OK — both stacks ready")
    return 0


def cmd_mount(args) -> int:
    """Mount the NAS read-only via SSHFS at /home/michael.gerber/nas."""
    script = Path(__file__).resolve().parent / "kb" / "mount_nas.sh"
    return subprocess.call(["bash", str(script)])


def cmd_umount(args) -> int:
    """Unmount the SSHFS NAS mount."""
    script = Path(__file__).resolve().parent / "kb" / "umount_nas.sh"
    return subprocess.call(["bash", str(script)])


def cmd_index(args) -> int:
    from kb.config import DATA_DIR, DEFAULT_ROOTS
    from kb.indexer import delta_scan
    if args.path:
        roots = [(args.root or Path(args.path).name, Path(args.path))]
    else:
        roots = DEFAULT_ROOTS
    for name, path in roots:
        print(f"=== index {name}  ({path}) ===")
        summary = delta_scan(name, path)
        print(json.dumps(summary, indent=2))
        # Persist a per-root summary so the skipped[]/errors[] lists are
        # inspectable after the fact (status.py --detail --root <NAME>,
        # the dashboard's KB page, or just `cat kb/data/<variant>/last_scan_<NAME>.json`).
        # Same path/format as kb.scheduled writes — they're interchangeable.
        out = DATA_DIR / f"last_scan_{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"  → written: {out}")
    return 0


def cmd_reindex(args) -> int:
    from kb.scheduled import main as scheduled_main
    return scheduled_main()


def cmd_query(args) -> int:
    from kb.query import answer
    result = answer(
        args.question,
        root=args.root,
        language=args.language,
        yymm_prefix=args.yymm,
        compound_prefix=args.compound,
        top_k=args.top_k,
    )
    print("=== ANSWER ===")
    print(result.text)
    print()
    print(f"=== SOURCES ({len(result.sources)}) ===")
    for s in result.sources:
        tag = []
        if s.compound: tag.append(s.compound)
        if s.yymm: tag.append(s.yymm)
        tag_s = f"  [{' · '.join(tag)}]" if tag else ""
        print(f"  · {s.score:.3f}  {s.rel_path}{tag_s}")
    return 0


def cmd_chat(args) -> int:
    root = Path(__file__).resolve().parent
    # Pull the variant's default port from kb.config (already initialised).
    from kb.config import UI_PORT, UI_LABEL
    port = str(args.port if args.port else UI_PORT)
    env = {**os.environ, "KB_VARIANT": args.variant}
    argv = [
        ".venv/bin/streamlit", "run", "chat_ui/chat_ui.py",
        "--server.address", "0.0.0.0",
        "--server.port", port,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    print(f"Launching {UI_LABEL} chat UI on http://0.0.0.0:{port} …")
    return subprocess.call(argv, cwd=str(root), env=env)


def cmd_remove(args) -> int:
    """
    Delete every chunk for one root from the active variant's collection,
    and (by default) also delete the per-root last_scan summary file.

    Does NOT delete:
      · The source files on the NAS (Data_Michael_restructured/<col>/<root>/)
      · The pipeline state file (data/runs/<col>/<root>.state.json)
      · The local target_local/<col>/<root>/ scratch dir

    To remove those too, do it manually after this command — they're
    independent of the KB and the wizard rebuilds them on the next run.
    """
    from kb.config import DATA_DIR, QDRANT_COLLECTION, KB_VARIANT
    from kb.indexer import count_root_chunks, delete_root

    root = args.root
    n_before = count_root_chunks(root)
    summary_path = DATA_DIR / f"last_scan_{root}.json"

    print(f"Will remove from {QDRANT_COLLECTION} (variant: {KB_VARIANT}):")
    print(f"  · {n_before:,} chunks where root='{root}'")
    if summary_path.exists() and not args.keep_summary:
        print(f"  · {summary_path.relative_to(Path(__file__).resolve().parent)} "
              f"(last-scan summary file)")
    if n_before == 0 and not summary_path.exists():
        print("Nothing to do.")
        return 0

    if not args.yes:
        print()
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    n_deleted = delete_root(root)
    print(f"✓ deleted {n_deleted:,} chunks from {QDRANT_COLLECTION}")

    if summary_path.exists() and not args.keep_summary:
        summary_path.unlink()
        print(f"✓ removed {summary_path}")

    # Sanity: re-count
    remaining = count_root_chunks(root)
    if remaining:
        print(f"⚠ {remaining:,} chunks still present (Qdrant async cleanup?). "
              f"Re-run the command if persistent.")
    return 0


def cmd_status(args) -> int:
    from kb.config import DATA_DIR, QDRANT_COLLECTION
    from kb.indexer import collection_stats
    stats = collection_stats()
    print(f"Collection: {QDRANT_COLLECTION}")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()
    print("Last-scan summaries:")
    for f in sorted(DATA_DIR.glob("last_scan_*.json")):
        data = json.loads(f.read_text())
        root = data.get("root", "?")
        n_files = data.get("scanned_files", "?")
        chunks = data.get("chunks_added", "?")
        at = data.get("scanned_at", "?")
        print(f"  · {root}  files={n_files}  chunks={chunks}  at={at}")
    return 0


def parse_args():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
        epilog=HELP_EPILOG,
    )
    # --variant selects which physically-separate KB stack to operate on.
    # Sets the KB_VARIANT env var that kb/config.py reads at import time.
    ap.add_argument("--variant", choices=["personal", "360f"], default="personal",
                    help="Which KB stack to act on. 'personal' → :6333 + chat :8502 "
                         "(Data_Michael_restructured/Personal/*). "
                         "'360f' → :6433 + chat :8503 "
                         "(Data_Michael_restructured/360F/*). Default: personal.")

    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("setup",  help="docker compose up qdrant-personal + qdrant-360f, then create both collections")
    sub.add_parser("mount",  help="mount NAS read-only via SSHFS at /home/michael.gerber/nas (shared by both variants)")
    sub.add_parser("umount", help="unmount the NAS SSHFS mount")

    ai = sub.add_parser("index", help="initial full index of a root (or all DEFAULT_ROOTS for the chosen --variant)")
    ai.add_argument("--root", help="logical root name (default: directory name of --path)")
    ai.add_argument("--path", help="local directory to index (default: every root in kb/config.py)")

    sub.add_parser("reindex", help="delta scan of all DEFAULT_ROOTS for the chosen --variant")

    q = sub.add_parser("query", help="one-shot RAG query from terminal")
    q.add_argument("question", help="the natural-language question")
    q.add_argument("--root", help="filter by root name")
    q.add_argument("--language", choices=["en","de","fr","it","es","nl","pt"])
    q.add_argument("--yymm", help="filter by yymm prefix (e.g. 2023, 2312)")
    q.add_argument("--compound", help="filter by compound prefix (e.g. FBUBS)")
    q.add_argument("--top-k", type=int, default=10)

    c = sub.add_parser("chat", help="launch Streamlit chat UI on the variant's port")
    c.add_argument("--port", type=int, default=None,
                   help="override the variant's default port (8502/8503)")

    sub.add_parser("status", help="collection stats + last-scan info for the chosen --variant")

    rm = sub.add_parser("remove",
                        help="delete every chunk for one root from the chosen "
                             "--variant's collection (use to forget a subset "
                             "without re-deploying Qdrant)")
    rm.add_argument("--root", required=True,
                    help="logical root name as it appears in 'kb.py status' "
                         "(e.g. 'F-Finance', 'A-Admin', 'C-Companies')")
    rm.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt (for scripts)")
    rm.add_argument("--keep-summary", action="store_true",
                    help="keep kb/data/<variant>/last_scan_<root>.json on disk "
                         "(default: also delete it). Useful if you want a "
                         "post-mortem record of what was indexed before removal.")

    return ap.parse_args()


def main() -> int:
    args = parse_args()
    # Set KB_VARIANT BEFORE we import anything from kb/, since kb/config.py
    # reads it at module load. Subcommand handlers do their own kb.* imports.
    os.environ["KB_VARIANT"] = args.variant
    if not args.cmd:
        print("Specify a subcommand. Try ./kb.py --help", file=sys.stderr)
        return 2
    dispatch = {
        "setup":   cmd_setup,
        "mount":   cmd_mount,
        "umount":  cmd_umount,
        "index":   cmd_index,
        "reindex": cmd_reindex,
        "query":   cmd_query,
        "chat":    cmd_chat,
        "status":  cmd_status,
        "remove":  cmd_remove,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
