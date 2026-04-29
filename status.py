#!/usr/bin/env python3
"""
folder-reorg status — one-shot snapshot of pipeline + KB activity on aizh.

Shows what's currently running, what just finished, and where everything
else is. Standalone — does not import run.py or kb.py. Safe to run as
often as you like. AUTO-REAPS orphaned worker processes (PPID=1, parent
exited) by default — see --no-reap.

Usage:
    ./status.py                  one-shot snapshot, exit
    ./status.py --watch 5        refresh every 5 s (Ctrl-C to stop)
    ./status.py -n 10            show 10 recent state files / KB scans (default 5)
    ./status.py --no-color       disable ANSI colors (or set NO_COLOR=1)
    ./status.py --no-reap        do NOT kill orphaned workers, just flag them
    ./status.py --errors         show per-file errors from each last_scan_*.json
    ./status.py --skipped        show per-file skipped list (path + reason)
    ./status.py --detail         alias for --errors --skipped
    ./status.py --root NAME      filter --errors / --skipped by root substring

Sections (in display order):
    CURRENTLY RUNNING — what's active right NOW (wizard, stage, subset, GPU)
    LAST COMPLETED    — most recent fully-done subset + most recent clean KB scan
    RECENT STATE      — last N pipeline state-files modified
    RECENT KB SCANS   — last N kb.scheduled per-root summaries
    GPU / OLLAMA      — full bge-m3 / qwen2.5:14b residency detail
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


def proc_meta(pid: str) -> tuple[str, str, str]:
    """Return (ppid, etime, stat) for a pid via `ps`. Empty strings on error."""
    rc, out, _ = run(["ps", "-o", "ppid=,etime=,stat=", "-p", pid])
    if rc != 0 or not out.strip():
        return ("", "", "")
    parts = out.strip().split(None, 2)
    while len(parts) < 3:
        parts.append("")
    return tuple(parts)  # type: ignore[return-value]


def is_orphan(pid: str) -> bool:
    """A subprocess is orphaned when its parent has exited and init (PID 1)
    adopted it. For pipeline workers this almost always means the wizard /
    batch driver crashed or was Ctrl-C'd, leaving the multiprocessing pool
    sleeping forever. Safe to kill with `pkill -f <pattern>`."""
    ppid, _etime, _stat = proc_meta(pid)
    return ppid == "1"


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
    # KB indexer: matches all three invocations:
    #   · python -m kb.scheduled          (timer-driven nightly run)
    #   · python kb.py --variant X reindex (manual delta scan)
    #   · python kb.py --variant X index   (initial full index)
    # cmd_reindex / cmd_index call scheduled.main() / delta_scan() in-process,
    # so the running command line is `python kb.py …` not `python -m kb.scheduled`.
    ("KB indexer  (kb.scheduled / kb.py reindex)",
     r"kb\.scheduled\b|kb\.py\b.*\b(?:re)?index\b"),
]


def section_currently_running(reap: bool = True) -> None:
    """The MOST IMPORTANT section: what's actively running RIGHT NOW.

    If reap=True (default), orphaned worker processes (PPID=1) are killed
    silently before they're displayed — but ONLY when no run.py is active
    (otherwise they might be live multiprocessing-pool workers under an
    active wizard). Orphans never survive past one status.py invocation.
    """
    banner("CURRENTLY RUNNING")

    # 1. run.py wizard / batch — the master driver
    runpy_lines = pgrep(r"run\.py")
    runpy_alive = bool(runpy_lines)
    if runpy_alive:
        for l in runpy_lines:
            pid = l.split(" ", 1)[0]
            _, etime, _ = proc_meta(pid)
            highlighted = re.sub(r"(--batch \S+)", f"{MAGENTA}\\1{RESET}", l)
            etime_part = f"  {DIM}(up {etime}){RESET}" if etime else ""
            print(f"  {ARROW} Wizard:  {highlighted}{etime_part}")
    else:
        print(f"  {DOT} Wizard:  no run.py active")

    # 2. Active stage subprocess — with subset extraction
    active_label: str | None = None
    active_subset_hint: str | None = None
    orphans_killed: list[tuple[str, list[str]]] = []
    for label, pat in _STAGE_PATTERNS:
        ls = pgrep(pat)
        if not ls:
            continue
        by_cmd: dict[str, list[str]] = {}
        for l in ls:
            parts = l.split(" ", 1)
            pid, cmd = (parts[0], parts[1] if len(parts) > 1 else l)
            by_cmd.setdefault(cmd, []).append(pid)
        for cmd, pids in by_cmd.items():
            n = len(pids)
            orphan_pids = [p for p in pids if is_orphan(p)]
            n_orphan = len(orphan_pids)
            # AUTO-REAP: when reap=True, no run.py is alive, and the whole
            # pool is orphaned, kill them silently. (We never auto-kill
            # while run.py is active — the workers might be live.)
            if reap and not runpy_alive and n_orphan == n:
                killed = _reap(orphan_pids)
                if killed:
                    orphans_killed.append((label, killed))
                continue  # don't display reaped processes
            # Otherwise, render the (possibly mixed) line
            if n_orphan == n:
                marker = WARN
                detail = (f"  {YELLOW}{n} ORPHANED process(es) "
                          f"({pids[0]}{'…'+pids[-1] if n>1 else ''} — "
                          f"parent exited; pass --no-reap to keep them){RESET}")
            elif n_orphan > 0:
                marker = WARN
                detail = (f"  {YELLOW}{n} process(es) "
                          f"— {n_orphan} ORPHANED{RESET}")
            elif n > 1:
                marker = ARROW
                detail = f"  {DIM}{n} processes ({pids[0]}…{pids[-1]}){RESET}"
            else:
                marker = ARROW
                _, etime, _ = proc_meta(pids[0])
                detail = (f"  {DIM}pid {pids[0]}"
                          + (f", up {etime}" if etime else "")
                          + f"{RESET}")
            print(f"  {marker} Stage:   {label}{detail}")
            # Heuristic: extract the --source argument as a subset hint
            m = re.search(r"--source[ =]([^\s]+(?:\s+[^\s-][^\s]*)*)", cmd)
            if m and not active_subset_hint:
                active_subset_hint = m.group(1).rstrip()
                active_label = label

    if not orphans_killed and active_label is None:
        # Either nothing's running, or only the wizard is (between stages)
        if runpy_alive:
            print(f"  {DOT} Stage:   between stages")
        else:
            print(f"  {DOT} Stage:   none")

    # 3. Active subset — pulled from the most-recently-modified state file
    # (within the last 5 minutes = "currently being worked on")
    most_recent_state = _most_recent_state_file()
    if most_recent_state:
        p, d = most_recent_state
        age = (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds()
        if runpy_alive or age < 300:  # within 5 min OR wizard is alive
            col = d.get("collection", "?")
            slug = d.get("subset", "?")
            n_done = len(d.get("completed", []))
            print(f"  {ARROW} Subset:  [{col}] {slug}  "
                  f"{DIM}stages {n_done}/{N_STAGES}, state saved {fmt_age(p)}{RESET}")
    elif active_subset_hint:
        print(f"  {ARROW} Subset:  {DIM}{active_subset_hint}{RESET}")

    # 4. GPU residency + live load (one-or-two-line summary). For each
    # model in `ollama ps`, print the model name + memory size, then a
    # second line with utilization %, memory used, and temperature from
    # nvidia-smi. We also flag UNEXPECTED models — anything not
    # qwen2.5:14b (Phase 3 / chat) or bge-m3 (embeddings) is external
    # noise (manual `ollama run`, IDE plugins, etc.).
    EXPECTED_MODEL_PREFIXES = ("qwen2.5:14b", "bge-m3")
    rc, out, _ = run(["ollama", "ps"])
    gpu_idle = (rc != 0) or (not out.strip()) or len(out.splitlines()) <= 1
    rc_smi, smi, _ = run([
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    smi_line = ""
    if rc_smi == 0 and smi.strip():
        # Line: "<util>, <mem_used>, <mem_total>, <temp>"
        toks = [t.strip() for t in smi.splitlines()[0].split(",")]
        if len(toks) == 4:
            util, mem_used, mem_total, temp = toks
            mem_used_gb = f"{int(mem_used)/1024:.1f}"
            mem_total_gb = f"{int(mem_total)/1024:.1f}"
            smi_line = (f"util {util}%, mem {mem_used_gb}/{mem_total_gb} GB, "
                        f"temp {temp}°C")

    if rc != 0:
        print(f"  {DOT} GPU:     ollama not reachable")
        if smi_line:
            print(f"           {DIM}{smi_line}{RESET}")
    elif gpu_idle:
        # No models resident, but GPU may still be holding memory from
        # a just-finished workload — surface that via nvidia-smi
        print(f"  {DOT} GPU:     no models resident in VRAM")
        if smi_line:
            print(f"           {DIM}{smi_line}{RESET}")
    else:
        lines = [l for l in out.splitlines() if l.strip()]
        size_re = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]B)\b")
        for l in lines[1:]:
            cells = l.split()
            model = cells[0] if cells else "?"
            m = size_re.search(l)
            size = f"{m.group(1)} {m.group(2)}" if m else "?"
            expected = any(model.startswith(p) for p in EXPECTED_MODEL_PREFIXES)
            if expected:
                print(f"  {ARROW} GPU:     {model} resident ({size})")
            else:
                print(f"  {WARN} GPU:     {model} resident ({size})  "
                      f"{DIM}— NOT a folder-reorg model (qwen2.5:14b / "
                      f"bge-m3 expected). External client?{RESET}")
        if smi_line:
            print(f"           {DIM}{smi_line}{RESET}")

    # 5. Active NAS rsync
    rs = pgrep(r"rsync.*Data_Michael")
    if rs:
        for l in rs:
            direction = ("→ NAS" if "Data_Michael_restructured" in l
                                  and "mgzh11" in l.split(" ")[-1]
                         else "← NAS")
            print(f"  {ARROW} Rsync:   {direction}  {DIM}{l[:100]}…{RESET}")

    # 6. Reaped orphans summary (if any)
    if orphans_killed:
        total = sum(len(pids) for _, pids in orphans_killed)
        print()
        print(f"  {WARN} REAPED {total} orphaned worker(s) — "
              f"left over from a previous Ctrl-C'd run:")
        for label, pids in orphans_killed:
            print(f"     {DIM}· {label}: killed {len(pids)} process(es) "
                  f"({pids[0]}{'…'+pids[-1] if len(pids)>1 else ''}){RESET}")


def section_last_completed() -> None:
    """The SECOND-most-important section: what just finished cleanly."""
    banner("LAST COMPLETED")

    # 1. Most recent fully-completed subset (state file with all stages done)
    candidates: list[tuple[Path, dict]] = []
    for col in ("Personal", "360F"):
        cd = STATE_DIR / col
        if not cd.is_dir():
            continue
        for p in cd.glob("*.state.json"):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            n_done = len(d.get("completed", []))
            if n_done >= N_STAGES - 1:  # 11 or 12 = "done"
                candidates.append((p, d))
    if candidates:
        candidates.sort(key=lambda pd: pd[0].stat().st_mtime, reverse=True)
        p, d = candidates[0]
        col   = d.get("collection", "?")
        slug  = d.get("subset", "?")
        nas   = d.get("nas_name", "?")
        n_done = len(d.get("completed", []))
        print(f"  {OK} Subset:  [{col}] {slug}  "
              f"{DIM}({nas}){RESET}  {n_done}/{N_STAGES} stages  "
              f"at {fmt_mtime(p)} {DIM}({fmt_age(p)}){RESET}")
    else:
        print(f"  {DOT} Subset:  no fully-completed subset state on file")

    # 2. Most recent clean KB scan (errors == 0)
    clean_scans: list[tuple[str, Path, dict]] = []
    for v in ("personal", "360f"):
        vd = KB_DATA_DIR / v
        if not vd.is_dir():
            continue
        for p in vd.glob("last_scan_*.json"):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            if not d.get("errors"):
                clean_scans.append((v, p, d))
    if clean_scans:
        clean_scans.sort(key=lambda vpd: vpd[1].stat().st_mtime, reverse=True)
        v, p, d = clean_scans[0]
        root  = d.get("root", "?")
        nf    = d.get("scanned_files", "?")
        ch    = d.get("chunks_added", 0)
        print(f"  {OK} KB scan: [{v}] {root}  "
              f"files={nf}, chunks+={ch}, err=0  "
              f"at {fmt_mtime(p)} {DIM}({fmt_age(p)}){RESET}")
    else:
        print(f"  {DOT} KB scan: no scan completed without errors yet")


# --- helpers used by the new sections ----------------------------------------
def _most_recent_state_file() -> tuple[Path, dict] | None:
    files: list[Path] = []
    for col in ("Personal", "360F"):
        cd = STATE_DIR / col
        if cd.is_dir():
            files.extend(cd.glob("*.state.json"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    p = files[0]
    try:
        return p, json.loads(p.read_text())
    except Exception:
        return None


def _reap(pids: list[str]) -> list[str]:
    """SIGTERM each pid; return the list of those that were successfully
    signalled. Best-effort — silently ignores already-dead PIDs."""
    killed: list[str] = []
    for pid in pids:
        try:
            os.kill(int(pid), 15)  # SIGTERM
            killed.append(pid)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    return killed


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
# Per-root detail (--errors / --skipped / --detail)
# ---------------------------------------------------------------------------
def _iter_scan_files(root_filter: str | None = None
                     ) -> list[tuple[str, str, Path]]:
    """Return [(variant, root_name, path)] for every last_scan_*.json.

    `root_filter` (case-insensitive substring) restricts to roots whose
    name contains it — used by --root.
    """
    out: list[tuple[str, str, Path]] = []
    for v in ("personal", "360f"):
        vd = KB_DATA_DIR / v
        if not vd.is_dir():
            continue
        for p in sorted(vd.glob("last_scan_*.json")):
            root = p.stem.removeprefix("last_scan_")
            if root_filter and root_filter.lower() not in root.lower():
                continue
            out.append((v, root, p))
    return out


def section_errors(root_filter: str | None, max_per_root: int = 50) -> None:
    """Print the `errors` list from each matching last_scan_*.json,
    grouped by error message family (so 95 errors of the same kind don't
    fill the screen)."""
    from collections import Counter
    files = _iter_scan_files(root_filter)
    if not files:
        banner("ERRORS")
        print(f"  {DOT} no last_scan files match"
              + (f" {root_filter!r}" if root_filter else ""))
        return
    for v, root, p in files:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        errs: list[str] = d.get("errors") or []
        banner(f"ERRORS — [{v}] {root}  ({len(errs)} total)")
        if not errs:
            print(f"  {OK} no errors recorded")
            continue
        # Group by trimmed message prefix
        cats: Counter[str] = Counter()
        for e in errs:
            msg = e.split(": ", 1)[-1].strip()
            cats[msg[:90]] += 1
        # Header: family summary
        for msg, n in cats.most_common():
            print(f"  {RED}{n:>4}{RESET}  {msg}")
        # Detail: list of (file → message) up to max_per_root
        print()
        print(f"  {DIM}— sample of up to {max_per_root} affected files —{RESET}")
        shown = 0
        for e in errs:
            if shown >= max_per_root:
                print(f"  {DIM}… ({len(errs) - shown} more){RESET}")
                break
            # Errors are formatted "<rel_path>: <message>" — show the path
            # and a short message
            path, _, msg = e.partition(": ")
            short = msg[:80] + ("…" if len(msg) > 80 else "")
            print(f"  {WARN} {path}")
            print(f"     {DIM}{short}{RESET}")
            shown += 1


def section_skipped(root_filter: str | None, max_per_root: int = 50) -> None:
    """Print the `skipped` list (per-file skip records with reason).

    Skip records were added in a recent indexer change — older
    last_scan_*.json files only have an aggregate skip COUNT and no
    per-file detail. For those we print a hint instead.
    """
    from collections import Counter
    files = _iter_scan_files(root_filter)
    if not files:
        banner("SKIPPED")
        print(f"  {DOT} no last_scan files match"
              + (f" {root_filter!r}" if root_filter else ""))
        return
    for v, root, p in files:
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        skipped = d.get("skipped")
        skip_count = d.get("skip", 0)
        overflow = d.get("skipped_overflow", 0)
        banner(f"SKIPPED — [{v}] {root}  ({skip_count} files skipped)")
        if not skip_count:
            print(f"  {OK} no skipped files")
            continue
        if not isinstance(skipped, list):
            print(f"  {DIM}per-file skip records not available — this scan "
                  f"predates the indexer change.{RESET}")
            print(f"  {DIM}Run a fresh scan: ./kb.py --variant {v} reindex{RESET}")
            continue
        # Group by reason
        by_reason: Counter[str] = Counter(s.get("reason", "?") for s in skipped)
        for reason, n in by_reason.most_common():
            print(f"  {YELLOW}{n:>4}{RESET}  skip:{reason}")
        if overflow:
            print(f"  {DIM}… plus {overflow} additional skips not recorded "
                  f"(per-root cap of 1000 hit){RESET}")
        # Detail: list affected files, grouped by reason for readability
        print()
        print(f"  {DIM}— sample of up to {max_per_root} affected files "
              f"(grouped by reason) —{RESET}")
        shown = 0
        for reason in [r for r, _ in by_reason.most_common()]:
            for s in skipped:
                if s.get("reason") != reason:
                    continue
                if shown >= max_per_root:
                    break
                print(f"  {DIM}skip:{reason:<14}{RESET} {s.get('path', '?')}")
                shown += 1
            if shown >= max_per_root:
                remaining = skip_count - shown
                if remaining > 0:
                    print(f"  {DIM}… ({remaining} more){RESET}")
                break


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def render_all(n: int, reap: bool = True) -> None:
    print(f"{BOLD}folder-reorg status{RESET}  "
          f"{DIM}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}  "
          f"{DIM}host {os.uname().nodename}{RESET}")
    # Most important sections at the top
    section_currently_running(reap=reap)
    section_last_completed()
    # Reference sections below
    section_state_files(n)
    section_kb_scans(n)
    section_gpu()
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
                    help="refresh every SEC seconds (Ctrl-C to stop). "
                         "Ignored with --errors / --skipped / --detail.")
    ap.add_argument("-n", type=int, default=5,
                    help="how many recent state files / KB scans to show "
                         "in each list (default: 5)")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI colors (also: NO_COLOR=1 env var)")
    ap.add_argument("--no-reap", action="store_true",
                    help="do NOT auto-kill orphaned worker processes "
                         "(PPID=1, parent exited). Default: kill them "
                         "silently when no run.py is active so they can't "
                         "linger forever after a Ctrl-C.")

    # Detail flags — drill into one or all roots' last_scan_*.json
    ap.add_argument("--errors", action="store_true",
                    help="show the per-file errors list from each "
                         "last_scan_<root>.json (skips the dashboard)")
    ap.add_argument("--skipped", action="store_true",
                    help="show the per-file skipped list (path + reason) "
                         "from each last_scan_<root>.json. Requires the "
                         "indexer change that records skip details — older "
                         "scans only have an aggregate count.")
    ap.add_argument("--detail", action="store_true",
                    help="shorthand for --errors --skipped")
    ap.add_argument("--root", metavar="NAME",
                    help="filter --errors / --skipped to roots whose name "
                         "contains NAME (case-insensitive substring). E.g. "
                         "--root F-Finance shows F-Finance for both variants.")
    ap.add_argument("--max-per-root", type=int, default=50,
                    help="how many per-file detail lines to show per root "
                         "in --errors / --skipped output (default: 50)")
    args = ap.parse_args()

    _NO_COLOR = bool(args.no_color)
    init_colors()

    # Detail mode short-circuits the dashboard: it's typically piped to less
    # or grep, so we don't want the noisy live status above the data.
    if args.errors or args.skipped or args.detail:
        if args.errors or args.detail:
            section_errors(args.root, max_per_root=args.max_per_root)
        if args.skipped or args.detail:
            section_skipped(args.root, max_per_root=args.max_per_root)
        print()
        return 0

    reap = not args.no_reap

    if args.watch <= 0:
        render_all(args.n, reap=reap)
        return 0

    # Watch mode: clear screen between renders
    clear = "clear" if shutil.which("clear") else None
    try:
        while True:
            if clear:
                os.system(clear)
            render_all(args.n, reap=reap)
            print(f"{DIM}── refreshing every {args.watch}s — Ctrl-C to stop ──{RESET}")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
