#!/usr/bin/env python3
"""
folder-reorg status — one-shot snapshot of pipeline + KB activity on aizh.

Shows what's currently running, what recently finished, and where the GPU /
chat / Qdrant / NAS-mount stacks are. Standalone — does not import run.py
or kb.py and does not modify any state. Safe to run as often as you like.

Usage:
    ./status.py                  one-shot snapshot, exit
    ./status.py --watch 5        refresh every 5 s (Ctrl-C to stop)
    ./status.py -n 10            show 10 recent state files / KB scans (default 5)
    ./status.py --no-color       disable ANSI colors (or set NO_COLOR=1)

Sections:
    PIPELINE          — run.py wizard/batch + currently-active stage subprocess + rsync
    GPU / OLLAMA      — bge-m3 / qwen2.5:14b residency in VRAM
    RECENT STATE      — last N pipeline state-files modified (which subsets advanced a stage)
    RECENT KB SCANS   — last N kb.scheduled per-root summaries (new/updated/chunks/errors)
    CHAT / SERVICES   — Streamlit on :8502/:8503 + Qdrant containers
    NAS MOUNT         — /home/michael.gerber/nas reachability
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
ROOT        = Path(__file__).resolve().parent
STATE_DIR   = ROOT / "data" / "runs"
KB_DATA_DIR = ROOT / "kb" / "data"
NAS_MOUNT   = Path(os.environ.get("KB_NAS_MOUNT", "/home/michael.gerber/nas"))

N_STAGES = 12   # 0..11 — used for the "stages 4/12" indicator


# ---------------------------------------------------------------------------
# Colors (auto-disabled when piped or NO_COLOR set; --no-color overrides)
# ---------------------------------------------------------------------------
def _c(code: str) -> str:
    if (not sys.stdout.isatty()) or os.environ.get("NO_COLOR") or _NO_COLOR:
        return ""
    return code

_NO_COLOR = False  # set via --no-color before any color is rendered


def init_colors():
    global GREEN, YELLOW, RED, BLUE, CYAN, MAGENTA, DIM, BOLD, RESET
    global OK, WARN, FAIL, DOT, ARROW
    GREEN   = _c("\033[32m")
    YELLOW  = _c("\033[33m")
    RED     = _c("\033[31m")
    BLUE    = _c("\033[34m")
    CYAN    = _c("\033[36m")
    MAGENTA = _c("\033[35m")
    DIM     = _c("\033[2m")
    BOLD    = _c("\033[1m")
    RESET   = _c("\033[0m")
    OK    = f"{GREEN}✓{RESET}"
    WARN  = f"{YELLOW}⚠{RESET}"
    FAIL  = f"{RED}✗{RESET}"
    DOT   = f"{DIM}·{RESET}"
    ARROW = f"{CYAN}▸{RESET}"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a command; never raise. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)


def pgrep(pattern: str) -> list[str]:
    """Return matching `ps -af` lines, excluding our own grep."""
    rc, out, _ = run(["pgrep", "-af", pattern])
    if rc != 0:
        return []
    return [l for l in out.splitlines() if l.strip() and "grep" not in l]


# ---------------------------------------------------------------------------
# Time / formatting helpers
# ---------------------------------------------------------------------------
def fmt_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def fmt_age(p: Path) -> str:
    delta = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s//60}m ago"
    if s < 86400:
        return f"{s//3600}h ago"
    return f"{s//86400}d ago"


def banner(text: str) -> None:
    print(f"\n{BOLD}{BLUE}── {text} ──{RESET}")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
# Stage subprocess detection table — order matters for label clarity
_STAGE_PATTERNS: list[tuple[str, str]] = [
    ("Stage 3  Phase 0  manifest",         r"src\.phase0_manifest"),
    ("Stage 4  Phase 1  inventory",        r"src\.phase1_inventory"),
    ("Stage 4  Phase 1  extract",          r"src\.phase1_extract"),
    ("Stage 4  Phase 1  lang detect",      r"src\.phase1_lang_detect"),
    ("Stage 5  Phase 2  embed (bge-m3)",   r"src\.phase2_embed"),
    ("Stage 5  Phase 2  cluster (HDBSCAN)", r"src\.phase2_cluster"),
    ("Stage 6  Phase 3  LLM classify",     r"src\.phase3_classify"),
    ("Stage 8  Phase 5  execute (copy)",   r"src\.phase5_execute"),
    ("Stage 9  Phase 6  verify",           r"src\.phase6_verify"),
    ("Stage 11 Phase 8  KB indexer",       r"kb\.scheduled"),
]


def section_pipeline() -> None:
    banner("PIPELINE")

    # 1. run.py wizard / batch
    lines = pgrep(r"run\.py")
    if not lines:
        print(f"  {DOT} no run.py wizard / batch active")
    else:
        for l in lines:
            # Highlight --batch arg if present
            highlighted = re.sub(r"(--batch \S+)", f"{MAGENTA}\\1{RESET}", l)
            print(f"  {OK} {highlighted}")

    # 2. Currently-active stage subprocess. Some Phase modules
    # (phase0_manifest in particular) spawn a multiprocessing pool of
    # identical worker processes — group those under a single line so the
    # output stays readable.
    print()
    found_stage = False
    for label, pat in _STAGE_PATTERNS:
        ls = pgrep(pat)
        if not ls:
            continue
        # Strip the leading "<pid> " from each line and dedupe by the rest
        by_cmd: dict[str, list[str]] = {}
        for l in ls:
            parts = l.split(" ", 1)
            pid = parts[0] if parts else ""
            cmd = parts[1] if len(parts) > 1 else l
            by_cmd.setdefault(cmd, []).append(pid)
        for cmd, pids in by_cmd.items():
            n = len(pids)
            workers = (f"  {DIM}({n} processes: {pids[0]}…{pids[-1]}){RESET}"
                       if n > 1 else f"  {DIM}(pid {pids[0]}){RESET}")
            print(f"  {ARROW} {label}{workers}")
            print(f"     {DIM}{cmd}{RESET}")
            found_stage = True
    if not found_stage:
        print(f"  {DOT} no pipeline subprocess "
              f"{DIM}(between stages, or pipeline idle){RESET}")

    # 3. NAS rsync (Stage 1 in or Stage 10 out)
    print()
    rs = pgrep(r"rsync.*Data_Michael")
    if not rs:
        print(f"  {DOT} no NAS rsync active")
    else:
        for l in rs:
            direction = "→ NAS" if "Data_Michael_restructured" in l and \
                        "mgzh11" in l.split(" ")[-1] else "← NAS"
            print(f"  {ARROW} rsync {direction}")
            print(f"     {DIM}{l}{RESET}")


def section_gpu() -> None:
    banner("GPU / OLLAMA")
    rc, out, _ = run(["ollama", "ps"])
    if rc != 0:
        print(f"  {WARN} ollama not reachable")
        return
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) <= 1:  # header only
        print(f"  {DOT} no models resident in VRAM")
        return
    for l in lines:
        print(f"  {l}")
    # Compute apps from nvidia-smi (optional; skip if not installed)
    rc, out, _ = run(["nvidia-smi",
                      "--query-compute-apps=pid,process_name,used_memory",
                      "--format=csv,noheader"])
    if rc == 0 and out.strip():
        print()
        print(f"  {DIM}nvidia-smi compute apps:{RESET}")
        for l in out.splitlines():
            print(f"    {l.strip()}")


def section_state_files(n: int) -> None:
    banner(f"RECENT PIPELINE STATE  (top {n} most-recently modified)")
    files: list[Path] = []
    for col in ("Personal", "360F"):
        cd = STATE_DIR / col
        if cd.is_dir():
            files.extend(cd.glob("*.state.json"))
    if not files:
        print(f"  {DOT} no state files under {STATE_DIR.relative_to(ROOT)}")
        return
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[:n]:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        col   = d.get("collection", "?")
        slug  = d.get("subset", "?")
        nas   = d.get("nas_name", "?")
        n_done = len(d.get("completed", []))
        if n_done == N_STAGES - 1:        # 11/12 = all real stages done (Stage 11 may not save)
            marker = OK
        elif n_done >= N_STAGES:           # complete
            marker = OK
        elif n_done > 0:
            marker = WARN
        else:
            marker = DOT
        print(f"  {marker} [{col:<8}] {slug:<26} "
              f"{DIM}{nas:<28}{RESET} "
              f"stages {n_done:>2}/{N_STAGES}  "
              f"at {fmt_mtime(p)} {DIM}({fmt_age(p)}){RESET}")


def section_kb_scans(n: int) -> None:
    banner(f"RECENT KB SCANS  (top {n} most-recently completed)")
    files: list[tuple[str, Path]] = []
    for v in ("personal", "360f"):
        vd = KB_DATA_DIR / v
        if vd.is_dir():
            for p in vd.glob("last_scan_*.json"):
                files.append((v, p))
    if not files:
        print(f"  {DOT} no KB scan summaries under {KB_DATA_DIR.relative_to(ROOT)}")
        return
    files.sort(key=lambda vp: vp[1].stat().st_mtime, reverse=True)
    for v, p in files[:n]:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        root  = d.get("root", "?")
        nf    = d.get("scanned_files", "?")
        new   = d.get("new", 0)
        upd   = d.get("updated", 0)
        ch    = d.get("chunks_added", 0)
        skp   = d.get("skip", 0)
        errs  = len(d.get("errors", []))
        marker = OK if errs == 0 else (WARN if errs < 50 else FAIL)
        # Color the err count if non-zero
        err_str = f"err={errs:<3}"
        if errs:
            err_str = f"{RED}err={errs:<3}{RESET}"
        print(f"  {marker} [{v:<8}] {root:<26} "
              f"files={nf:<5} new={new:<4} upd={upd:<4} "
              f"chunks+={ch:<5} skip={skp:<3} {err_str} "
              f"{DIM}at {fmt_mtime(p)} ({fmt_age(p)}){RESET}")


def section_chat_services() -> None:
    banner("CHAT UI / SERVICES")

    # Streamlit instances
    lines = pgrep(r"streamlit run chat_ui")
    by_port: dict[str, str] = {}
    for l in lines:
        m = re.search(r"--server\.port[ =](\d+)", l)
        if m:
            by_port[m.group(1)] = l
    for port, label in [("8502", "personal"), ("8503", "360f")]:
        if port in by_port:
            # Verify the port is actually listening
            rc, out, _ = run(["ss", "-lntH", f"sport = :{port}"])
            listening = bool(out.strip())
            tag = "" if listening else f"  {YELLOW}(process up but port not listening){RESET}"
            print(f"  {OK} Streamlit {label:<8} on :{port}{tag}")
        else:
            print(f"  {FAIL} Streamlit {label:<8} on :{port} — not running")

    # Qdrant containers. Match by suffix so both `qdrant-personal` and
    # `folderreorg-qdrant-personal` (the docker-compose-prefixed name) are
    # recognised.
    print()
    rc, out, err = run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    if rc != 0:
        print(f"  {WARN} cannot query docker ({_truncate(rc, err or out)})")
    else:
        running: dict[str, str] = {}
        for l in out.splitlines():
            parts = l.split("\t", 1)
            if len(parts) == 2:
                running[parts[0]] = parts[1]
        for variant in ("personal", "360f"):
            # Find any container whose name ends with `-qdrant-<variant>` or
            # equals `qdrant-<variant>` exactly.
            wanted_suffix = f"qdrant-{variant}"
            match = next((cn for cn in running
                          if cn == wanted_suffix or cn.endswith(f"-{wanted_suffix}")),
                         None)
            if match:
                print(f"  {OK} Qdrant {variant:<8} ({match}): "
                      f"{DIM}{running[match]}{RESET}")
            else:
                print(f"  {FAIL} Qdrant {variant:<8} — no container matching "
                      f"*qdrant-{variant} found")


def section_nas_mount() -> None:
    banner("NAS MOUNT")
    rc, out, _ = run(["findmnt", "-n", str(NAS_MOUNT)])
    if rc != 0 or not out.strip():
        print(f"  {FAIL} {NAS_MOUNT} is NOT mounted")
        print(f"  {DIM}fix: ./kb.py mount{RESET}")
        return
    print(f"  {OK} {NAS_MOUNT} mounted")
    print(f"  {DIM}{out.strip()}{RESET}")
    # Quick reachability test — list one entry under the mount with a short timeout
    rc, out, _ = run(["timeout", "3", "ls",
                      str(NAS_MOUNT / "Data_Michael_restructured")], timeout=5)
    if rc == 0:
        n = len([l for l in out.splitlines() if l.strip()])
        print(f"  {OK} reachable: {n} entries under Data_Michael_restructured/")
    else:
        print(f"  {WARN} mount listed in findmnt but ls timed out — NAS may be unreachable")


def _truncate(rc: int, msg: str, n: int = 80) -> str:
    msg = (msg or "").strip().replace("\n", " ")
    if len(msg) > n:
        msg = msg[:n] + "…"
    return f"rc={rc} {msg}" if msg else f"rc={rc}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def render_all(n: int) -> None:
    print(f"{BOLD}folder-reorg status{RESET}  "
          f"{DIM}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}  "
          f"{DIM}host {os.uname().nodename}{RESET}")
    section_pipeline()
    section_gpu()
    section_state_files(n)
    section_kb_scans(n)
    section_chat_services()
    section_nas_mount()
    print()


def main() -> int:
    global _NO_COLOR
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("--watch", type=int, metavar="SEC", default=0,
                    help="refresh every SEC seconds (Ctrl-C to stop)")
    ap.add_argument("-n", type=int, default=5,
                    help="how many recent state files / KB scans to show "
                         "in each list (default: 5)")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI colors (also: NO_COLOR=1 env var)")
    args = ap.parse_args()

    _NO_COLOR = bool(args.no_color)
    init_colors()

    if args.watch <= 0:
        render_all(args.n)
        return 0

    # Watch mode: clear screen between renders
    clear = "clear" if shutil.which("clear") else None
    try:
        while True:
            if clear:
                os.system(clear)
            render_all(args.n)
            print(f"{DIM}── refreshing every {args.watch}s — Ctrl-C to stop ──{RESET}")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
