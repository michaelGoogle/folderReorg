#!/usr/bin/env python3
"""
Interactive pipeline runner for the folder-reorg project.

Walks you through the same nine stages as docs/run-on-aizh.md, prompting at
each step. Progress is persisted to data/runs/<subset>.state.json so you can
Ctrl-C and resume later with `--resume`.

You can open parallel terminal windows at any point to poll things like:
    tail -f logs/phase3_<subset>.log
    nvidia-smi
    pgrep -af phase3_classify
    ss -lntp | grep 8501
    ollama ps

Usage:
    ./run.py                              # prompts for subset
    ./run.py --subset C-Companies --nas-name "C - Companies"
    ./run.py --resume                     # pick up where the last run left off

"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "data" / "runs"
REVIEW_URL = "http://192.168.1.10:8501"
NAS_HOST = "mgzh11"
NAS_DST_ROOT = "/volume1/Data_Michael_restructured"
LOGS_DIR = ROOT / "logs"

# ---------------------------------------------------------------------------
#  Collections
#
#  A "collection" is a group of top-level NAS source folders that share the
#  same rsync-in root and the same restructured-output subpath. Add more here
#  (e.g. other users' archives) and they show up in the picker.
#
#  Example destinations after running:
#    Personal subset "F - Finance"  → /volume1/Data_Michael_restructured/Personal/F-Finance/
#    360F subset "360F-A-Admin"     → /volume1/Data_Michael_restructured/360F/A-Admin/
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dc  # (dataclass already imported later; alias avoids shadowing)


@_dc(frozen=True)
class Collection:
    name: str              # shown in the menu, e.g. "Personal" / "360F"
    source_root: str       # NAS path above the source folder, e.g. /volume1/Data_Michael
    dest_subpath: str      # subfolder under NAS_DST_ROOT, e.g. "Personal"
    name_glob: str = "*"   # glob to filter folders under source_root (e.g. "360F-*")
    strip_prefix: str = "" # prefix to strip from NAS name when computing local slug


COLLECTIONS: list[Collection] = [
    Collection(name="Personal",
               source_root="/volume1/Data_Michael",
               dest_subpath="Personal"),
    Collection(name="360F",
               source_root="/volume1",
               dest_subpath="360F",
               name_glob="360F-*",
               strip_prefix="360F-"),
]


def find_collection_for(nas_name: str) -> Collection:
    """
    Pick the collection whose name_glob matches nas_name.

    Specific globs (like "360F-*") win over catch-alls ("*") regardless of
    the order in COLLECTIONS — otherwise the Personal entry (glob="*") would
    eat every name, including 360F ones.
    """
    import fnmatch
    catchall: Collection | None = None
    for col in COLLECTIONS:
        if col.name_glob == "*":
            catchall = col
            continue
        if fnmatch.fnmatch(nas_name, col.name_glob):
            return col
    return catchall or COLLECTIONS[0]


def find_collection_by_name(name: str) -> Collection | None:
    for col in COLLECTIONS:
        if col.name == name:
            return col
    return None


def derive_subset_slug(col: Collection, nas_name: str) -> str:
    """Turn a NAS folder name into a filesystem-safe local slug.

    Examples:
        (Personal, "F - Finance")    -> "F-Finance"
        (Personal, "C - Companies")  -> "C-Companies"
        (360F,     "360F-A-Admin")   -> "A-Admin"
    """
    name = nas_name
    if col.strip_prefix and name.startswith(col.strip_prefix):
        name = name[len(col.strip_prefix):]
    name = re.sub(r"\s*-\s*", "-", name).replace(" ", "-").replace("_", "-")
    name = re.sub(r"-+", "-", name).strip("-")
    return name

# ---------------------------------------------------------------------------
#  Colour helpers (tty-aware, respect NO_COLOR)
# ---------------------------------------------------------------------------

def _c(code: str) -> str:
    if os.environ.get("NO_COLOR") is not None or not sys.stdout.isatty():
        return ""
    return code

GREEN  = _c("\033[32m")
YELLOW = _c("\033[33m")
RED    = _c("\033[31m")
BLUE   = _c("\033[34m")
CYAN   = _c("\033[36m")
BOLD   = _c("\033[1m")
DIM    = _c("\033[2m")
RESET  = _c("\033[0m")

OK   = f"{GREEN}✓{RESET}"
WARN = f"{YELLOW}⚠{RESET}"
FAIL = f"{RED}✗{RESET}"
PEND = f"{DIM}·{RESET}"

def banner(text: str) -> None:
    line = "─" * 72
    print()
    print(f"{BLUE}{line}{RESET}")
    print(f"{BOLD}{text}{RESET}")
    print(f"{BLUE}{line}{RESET}")

def hint(text: str) -> None:
    print(f"{DIM}{text}{RESET}")

# ---------------------------------------------------------------------------
#  State
# ---------------------------------------------------------------------------

@dataclass
class Context:
    collection:        Collection     # which top-level grouping this subset belongs to
    subset:            str            # local slug, e.g. "A-Admin" / "F-Finance"
    nas_name:          str            # exact NAS folder name, e.g. "360F-A-Admin" / "F - Finance"
    source_from_mount: bool = False   # if True, read sources directly from the SSHFS mount
                                      # instead of rsync'ing them into source_local/ first.
    source_local:      Path = field(init=False)
    target_local:      Path = field(init=False)
    log_path:          Path = field(init=False)

    def __post_init__(self):
        # ALL local scratch directories are namespaced by collection so the
        # same slug under different collections (e.g. Personal "F-Finance"
        # vs 360F "F-Finance") never collide. The collection segment uses
        # `dest_subpath` ("Personal" / "360F") which is the same string we
        # use for the NAS destination — keeps everything consistent.
        coll = self.collection.dest_subpath
        self.target_local = ROOT / "target_local" / coll / self.subset
        self.log_path     = LOGS_DIR / coll / f"phase3_{self.subset}.log"
        if self.source_from_mount:
            # Read sources directly from the SSHFS-mounted NAS. The mount
            # exposes /volume1/ at NAS_MOUNT, so we just strip the /volume1
            # prefix from the collection's source_root and prepend the mount.
            # (Mount path is intrinsically namespaced via the NAS folder
            # name, so no collection prefix needed here.)
            self.source_local = self._mount_source_path()
        else:
            self.source_local = ROOT / "source_local" / coll / self.subset

    def _mount_source_path(self) -> Path:
        """Compute the local path to sources via the SSHFS mount."""
        # Lazy import — avoids circular deps; kb.config owns NAS_MOUNT.
        from kb.config import NAS_MOUNT
        src_root = self.collection.source_root
        if src_root.startswith("/volume1"):
            suffix = src_root[len("/volume1"):].lstrip("/")
            base = NAS_MOUNT / suffix if suffix else NAS_MOUNT
        else:
            # Fallback — assume source_root's leaf is what the mount exposes
            base = NAS_MOUNT
        return base / self.nas_name

    # Full remote paths the stages need
    @property
    def nas_source_full(self) -> str:
        return f"{self.collection.source_root}/{self.nas_name}"

    @property
    def nas_dest_parent(self) -> str:
        return f"{NAS_DST_ROOT}/{self.collection.dest_subpath}"

    @property
    def nas_dest_full(self) -> str:
        return f"{self.nas_dest_parent}/{self.subset}"

@dataclass
class State:
    ctx: Context
    completed: set[int] = field(default_factory=set)

    @property
    def file(self) -> Path:
        # New layout: data/runs/<collection>/<slug>.state.json
        return (STATE_DIR / self.ctx.collection.dest_subpath
                / f"{self.ctx.subset}.state.json")

    def save(self) -> None:
        # Ensure both the legacy STATE_DIR and the per-collection subdir exist.
        self.file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "collection":        self.ctx.collection.name,
            "subset":            self.ctx.subset,
            "nas_name":          self.ctx.nas_name,
            "source_from_mount": self.ctx.source_from_mount,
            "completed":         sorted(self.completed),
        }
        self.file.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, subset: str,
             collection: Optional["Collection"] = None) -> Optional["State"]:
        """
        Load a saved state file for `subset`.

        New layout: data/runs/<collection>/<slug>.state.json — the path is
        unique per (collection, slug). Pass `collection` to disambiguate
        when the same slug exists under different collections (e.g. Personal
        "F-Finance" vs 360F "F-Finance").

        Legacy fallback: data/runs/<slug>.state.json (no namespace) is also
        consulted, but ONLY if its `collection` field matches the requested
        `collection` (or `collection` is None). This prevents the historical
        Personal F-Finance state from being mistakenly loaded for 360F
        F-Finance.

        Returns None when no matching state file exists.
        """
        # 1. Prefer new namespaced path when collection is known.
        if collection is not None:
            p_new = STATE_DIR / collection.dest_subpath / f"{subset}.state.json"
            if p_new.exists():
                return cls._from_path(p_new)
            # 2. Legacy file, but only if its collection field matches.
            p_legacy = STATE_DIR / f"{subset}.state.json"
            if p_legacy.exists():
                try:
                    data = json.loads(p_legacy.read_text())
                except Exception:
                    return None
                if data.get("collection") == collection.name:
                    return cls._from_data(data)
                # Collision: legacy file is for a DIFFERENT collection.
                # Refuse silently — caller will treat as "no state" and
                # start fresh under the new namespaced path.
                return None
            return None

        # 3. No collection hint — try new layout (any collection subdir),
        #    then legacy. Used by --resume which already picked the file.
        for sub in (c.dest_subpath for c in COLLECTIONS):
            p = STATE_DIR / sub / f"{subset}.state.json"
            if p.exists():
                return cls._from_path(p)
        p_legacy = STATE_DIR / f"{subset}.state.json"
        if p_legacy.exists():
            return cls._from_path(p_legacy)
        return None

    @classmethod
    def _from_path(cls, p: Path) -> Optional["State"]:
        try:
            data = json.loads(p.read_text())
        except Exception:
            return None
        return cls._from_data(data)

    @classmethod
    def _from_data(cls, data: dict) -> "State":
        # Back-compat: state files written before the Collection concept
        # have no "collection" field — default them to "Personal".
        col_name = data.get("collection", "Personal")
        col = find_collection_by_name(col_name) or COLLECTIONS[0]
        ctx = Context(
            collection=col,
            subset=data["subset"],
            nas_name=data["nas_name"],
            source_from_mount=bool(data.get("source_from_mount", False)),
        )
        return cls(ctx=ctx, completed=set(data.get("completed", [])))


def _migrate_legacy_log_files() -> None:
    """
    One-time migration: move pre-namespacing Phase 3 logs from
    logs/phase3_<slug>.log into logs/<collection>/phase3_<slug>.log
    by looking up the slug's collection from the matching state file
    (which the state-file migration has already namespaced by the time
    this runs).

    Files whose slug has no matching state file under any collection are
    left in place with a `(skipped)` note — they're likely from old
    one-off manual runs (`phase3_full.log`, `phase3_*_v2.log`, etc.) and
    we don't want to guess.
    """
    if not LOGS_DIR.exists():
        return
    moved = 0
    skipped: list[str] = []
    for p in sorted(LOGS_DIR.glob("phase3_*.log")):  # depth-1 only
        slug = p.stem.removeprefix("phase3_")
        # Find which collection owns this slug by looking for its state file.
        owner: Collection | None = None
        for col in COLLECTIONS:
            if (STATE_DIR / col.dest_subpath / f"{slug}.state.json").exists():
                owner = col
                break
        if owner is None:
            skipped.append(f"{p.name}: no state file under any collection")
            continue
        dest_dir = LOGS_DIR / owner.dest_subpath
        dest = dest_dir / p.name
        if dest.exists():
            skipped.append(
                f"{p.name}: already migrated ({dest.relative_to(ROOT)} exists)")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        p.rename(dest)
        moved += 1
        print(f"  {OK} migrated {p.name}  →  {dest.relative_to(ROOT)}")
    if moved:
        print(f"{OK} log-file migration: moved {moved} legacy phase3 log(s) "
              f"into per-collection subdirs.")
    for s in skipped:
        print(f"  {DIM}skipped {s}{RESET}")


def _migrate_legacy_state_files() -> None:
    """
    One-time migration: move pre-namespacing state files from
    data/runs/<slug>.state.json into data/runs/<collection>/<slug>.state.json
    using the file's own `collection` field. Safe to run repeatedly.

    Files where the destination already exists are LEFT IN PLACE with a
    warning (so no automated overwrite). Files we can't parse are skipped.
    """
    if not STATE_DIR.exists():
        return
    moved = 0
    skipped: list[str] = []
    for p in sorted(STATE_DIR.glob("*.state.json")):  # depth-1 only
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(f"  {WARN} migrate: cannot parse {p.name}: {e}")
            continue
        col_name = data.get("collection", "Personal")
        col = find_collection_by_name(col_name)
        if col is None:
            skipped.append(f"{p.name}: unknown collection {col_name!r}")
            continue
        dest_dir = STATE_DIR / col.dest_subpath
        dest = dest_dir / p.name
        if dest.exists():
            skipped.append(
                f"{p.name}: already migrated ({dest.relative_to(ROOT)} exists)")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        p.rename(dest)
        moved += 1
        print(f"  {OK} migrated {p.name}  →  {dest.relative_to(ROOT)}")
    if moved:
        print(f"{OK} state-file migration: moved {moved} legacy state file(s) "
              f"into per-collection subdirs.")
    for s in skipped:
        print(f"  {DIM}skipped {s}{RESET}")

# ---------------------------------------------------------------------------
#  Command runners
# ---------------------------------------------------------------------------

def run_stream(cmd: list[str] | str, cwd: Path = ROOT, shell: bool = False,
               env: dict | None = None) -> int:
    """Run a subprocess, streaming stdout+stderr to our terminal. Returns exit code.
    `env` (if given) is merged on top of os.environ for the child process."""
    display = cmd if isinstance(cmd, str) else shlex.join(cmd)
    print(f"{DIM}$ {display}{RESET}")
    child_env = {**os.environ, **env} if env else None
    try:
        p = subprocess.Popen(
            cmd if shell else cmd,
            cwd=str(cwd), shell=shell,
            stdout=None, stderr=subprocess.STDOUT,
            env=child_env,
        )
        return p.wait()
    except KeyboardInterrupt:
        print(f"\n{WARN} Interrupt — terminating subprocess …")
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try: p.kill()
            except Exception: pass
        return 130

def run_capture(cmd: list[str] | str, shell: bool = False, timeout: int = 30) -> tuple[int, str, str]:
    r = subprocess.run(
        cmd, shell=shell, cwd=str(ROOT),
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr

def spawn_detached(argv: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "wb")
    return subprocess.Popen(
        argv, cwd=str(ROOT),
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

# ---------------------------------------------------------------------------
#  GPU coordination
# ---------------------------------------------------------------------------

def _pause_kb_indexer() -> None:
    """
    SIGTERM any running `python -m kb.scheduled` processes so they release
    bge-m3 from VRAM before a pipeline phase that also needs the GPU.

    The indexer is content-addressed and idempotent — Stage 11 (or the next
    nightly timer) will resume cleanly with no progress lost; chunks already
    in Qdrant are skipped via the mtime fast-path.

    No-op if no indexer is running.
    """
    rc, out, _ = run_capture(
        ["pgrep", "-f", r"python.*-m kb\.scheduled"], timeout=5,
    )
    pids = [int(p) for p in out.split() if p.strip().isdigit()]
    if not pids:
        return
    print(f"{DIM}pausing KB indexer (PID{'s' if len(pids) > 1 else ''}: "
          f"{', '.join(str(p) for p in pids)}) to free GPU for this stage{RESET}")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Give them a few seconds to actually shut down + release CUDA memory
    for _ in range(20):
        time.sleep(0.5)
        if not any(_pid_alive(p) for p in pids):
            break


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _unload_ollama_models() -> None:
    """
    Tell Ollama to release any held models from GPU memory immediately.
    Without this, models stay resident for OLLAMA_KEEP_ALIVE (default 5 min)
    after the last request, holding ~9 GB for qwen2.5:14b.

    Called at the end of a successful run so the GPU is clean for whatever
    runs next (KB indexer waking up, chat queries, another pipeline run).
    """
    import urllib.error
    import urllib.request
    base = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=10) as r:
            data = json.load(r)
        loaded = [m.get("name") for m in data.get("models", []) if m.get("name")]
    except (urllib.error.URLError, OSError, ValueError):
        return
    if not loaded:
        return
    print(f"{DIM}releasing Ollama models from GPU: {', '.join(loaded)}{RESET}")
    for name in loaded:
        try:
            req = urllib.request.Request(
                f"{base}/api/generate",
                data=json.dumps({"model": name, "keep_alive": 0,
                                 "prompt": ""}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15).read()
            print(f"  {OK} unloaded {name}")
        except Exception as e:
            print(f"  {WARN} could not unload {name}: {e}")


# ---------------------------------------------------------------------------
#  Prompt helpers
# ---------------------------------------------------------------------------

# --- Auto-run: when enabled, every prompt auto-defaults after a timeout. ---
# Set in main() from CLI args. Phase 4 (Streamlit review) is skipped when
# AUTO_RUN is true — there's no human at the browser, and Phase 5 falls back
# to rename_plan.csv (which already has decision=approve on every row).
AUTO_RUN: bool = False
AUTO_RUN_TIMEOUT: int = 60


def _input_with_timeout(prompt_text: str, timeout: int | None) -> str | None:
    """
    Read one line from stdin, waiting up to `timeout` seconds.
    Returns the line (without trailing newline), or None on timeout.
    `timeout=None` means wait forever (normal interactive behaviour).

    Linux/macOS only — uses select.select on file descriptors. The wizard
    runs on aizh (Linux), so this is fine.
    """
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    if timeout is None or timeout <= 0:
        return sys.stdin.readline().rstrip("\n")
    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        # Stdin not selectable (e.g. weird pipe); fall back to blocking read
        return sys.stdin.readline().rstrip("\n")
    if not ready:
        return None  # timed out
    return sys.stdin.readline().rstrip("\n")


def _auto_timeout() -> int | None:
    """Helper: returns AUTO_RUN_TIMEOUT iff AUTO_RUN is enabled, else None."""
    return AUTO_RUN_TIMEOUT if AUTO_RUN else None


def prompt(text: str, default: str = "") -> str:
    timeout = _auto_timeout()
    suffix = f" [{default}]" if default else ""
    countdown = f" {DIM}(auto in {timeout}s){RESET}" if timeout else ""
    try:
        line = _input_with_timeout(f"{CYAN}?{RESET} {text}{suffix}{countdown}: ", timeout)
    except (KeyboardInterrupt, EOFError):
        print()
        raise SystemExit(130)
    if line is None:
        # Timed out
        print(f"  {DIM}(auto: {default!r}){RESET}")
        return default
    return line.strip() or default


def prompt_choice(text: str, choices: dict[str, str], default: str) -> str:
    timeout = _auto_timeout()
    keys = "/".join(f"{BOLD}{k.upper() if k == default else k}{RESET}" for k in choices)
    while True:
        countdown = f" {DIM}(auto-{default} in {timeout}s){RESET}" if timeout else ""
        try:
            line = _input_with_timeout(f"{CYAN}?{RESET} {text} [{keys}]{countdown}: ", timeout)
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(130)
        if line is None:
            # Timed out → take the default
            print(f"  {DIM}(auto: {default}){RESET}")
            return default
        raw = (line.strip().lower() or default)
        if raw in choices:
            return raw
        print(f"  options: " + ", ".join(f"{k}={v}" for k, v in choices.items()))


def confirm(text: str, default: bool = True) -> bool:
    d = "y" if default else "n"
    ans = prompt_choice(text, {"y": "yes", "n": "no"}, d)
    return ans == "y"

# ---------------------------------------------------------------------------
#  Subset discovery
# ---------------------------------------------------------------------------

def list_collection_subsets(col: Collection) -> tuple[list[str], dict[str, str]]:
    """
    For one collection, return (available source subsets, already-restructured map).

    The map keys are local-slug form (what the destination folder is called),
    and values are the destination-folder mtime as 'YYYY-MM-DD HH:MM' (or "")
    if mtime couldn't be parsed. Treat the map's keys as the "done" set —
    `slug in done` works because dict membership tests keys.
    """
    import fnmatch as _fn
    # Directories only (excludes stray log files, sidecar entries, etc.)
    rc, out, _ = run_capture(
        ["ssh", NAS_HOST,
         f"find '{col.source_root}' -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null"],
        timeout=15,
    )
    if rc != 0:
        return [], set()
    names = []
    for line in out.splitlines():
        n = line.strip()
        if not n or n.startswith(("@", "#", ".")):
            continue
        if n.lower() in ("archive", "_archive"):
            continue
        if not _fn.fnmatch(n, col.name_glob):
            continue
        names.append(n)
    # Already-restructured: list each destination subfolder with its mtime
    # (most-recent file touch / rsync). Output: 'YYYY-MM-DD HH:MM\t<slug>' per line.
    rc2, out2, _ = run_capture(
        ["ssh", NAS_HOST,
         f"find '{NAS_DST_ROOT}/{col.dest_subpath}/' -mindepth 1 -maxdepth 1 "
         f"-type d -printf '%TY-%Tm-%Td %TH:%TM\\t%f\\n' 2>/dev/null || true"],
        timeout=15,
    )
    done: dict[str, str] = {}
    if rc2 == 0:
        for line in out2.splitlines():
            line = line.rstrip("\r\n")
            if not line:
                continue
            ts, sep, slug = line.partition("\t")
            if not sep:
                # Defensive: a line without a tab — treat the whole thing as slug.
                done[line.strip()] = ""
            else:
                done[slug.strip()] = ts.strip()
    return sorted(names), done


def pick_subset_interactive() -> Context:
    print(f"{BLUE}Discovering subsets on {NAS_HOST} …{RESET}")
    grouped: list[tuple[Collection, list[str], dict[str, str]]] = []
    for col in COLLECTIONS:
        names, done = list_collection_subsets(col)
        grouped.append((col, names, done))

    total = sum(len(n) for _, n, _ in grouped)
    if total == 0:
        print(f"{FAIL} Could not list any subsets. Is SSH to {NAS_HOST} working?")
        nas = prompt("NAS folder name (manual entry)")
        col = find_collection_for(nas)
        slug = derive_subset_slug(col, nas)
        return Context(collection=col, subset=slug, nas_name=nas)

    # Print grouped, numbered menu
    print()
    print(f"{BOLD}Available source subsets:{RESET}")
    entries: list[tuple[Collection, str]] = []
    n = 0
    for col, names, done in grouped:
        if not names:
            continue
        print()
        print(f"  {BOLD}{col.name}{RESET}  {DIM}({col.source_root}"
              + (f" · {col.name_glob}" if col.name_glob != "*" else "")
              + f"){RESET}")
        for name in names:
            n += 1
            slug = derive_subset_slug(col, name)
            if slug in done:
                ts = done[slug]
                ts_part = f", at {ts}" if ts else ""
                mark = f"  {GREEN}(restructured{ts_part}){RESET}"
            else:
                mark = ""
            print(f"  {n:>3}. {name}{mark}")
            entries.append((col, name))
    print()

    while True:
        pick = prompt("Pick by number (or type a name)")
        if pick.isdigit() and 1 <= int(pick) <= len(entries):
            col, nas_name = entries[int(pick) - 1]
            break
        # Try matching by exact name in any collection
        matches = [(c, nn) for (c, nn) in entries if nn == pick]
        if len(matches) == 1:
            col, nas_name = matches[0]
            break
        print(f"  {FAIL} not a valid choice")

    default_slug = derive_subset_slug(col, nas_name)
    subset = prompt("Local slug (used for directory names)", default=default_slug)
    print(f"{OK} {col.name} / {nas_name} → slug {BOLD}{subset}{RESET} "
          f"→ {NAS_DST_ROOT}/{col.dest_subpath}/{subset}")
    return Context(collection=col, subset=subset, nas_name=nas_name)

# ---------------------------------------------------------------------------
#  Stage definitions
# ---------------------------------------------------------------------------

@dataclass
class Stage:
    id: int
    title: str
    description: str
    run: Callable[[Context], bool]
    long: bool = False          # warn user it takes >1 min
    optional: bool = False

def stage_preflight(ctx: Context) -> bool:
    print("Checks:")
    ok = True
    # ollama
    rc, out, _ = run_capture(["ollama", "list"])
    if rc == 0 and "qwen2.5:14b" in out and "bge-m3" in out:
        print(f"  {OK} ollama has qwen2.5:14b + bge-m3")
    else:
        print(f"  {FAIL} ollama models missing. Run:  ollama pull qwen2.5:14b-instruct-q4_K_M && ollama pull bge-m3")
        ok = False
    # venv
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        print(f"  {OK} .venv/bin/python present")
    else:
        print(f"  {FAIL} .venv missing. Run:  uv venv --python 3.12 && uv pip install -r requirements.txt")
        ok = False
    # SSH to NAS
    rc, out, _ = run_capture(["ssh", NAS_HOST, "echo ok"], timeout=10)
    if rc == 0 and "ok" in out:
        print(f"  {OK} SSH to {NAS_HOST} works")
    else:
        print(f"  {FAIL} SSH to {NAS_HOST} failed")
        ok = False
    # NAS destination dir + grouped sub-share
    rc, _, _ = run_capture(["ssh", NAS_HOST, f"[ -w '{NAS_DST_ROOT}' ]"], timeout=10)
    if rc == 0:
        print(f"  {OK} {NAS_HOST}:{NAS_DST_ROOT}/ exists and is writable")
        # Ensure the collection's sub-share exists (mkdir -p is idempotent).
        dest_parent = ctx.nas_dest_parent
        rc2, _, _ = run_capture(
            ["ssh", NAS_HOST, f"mkdir -p '{dest_parent}' && [ -w '{dest_parent}' ]"],
            timeout=15,
        )
        if rc2 == 0:
            print(f"  {OK} {NAS_HOST}:{dest_parent}/ exists and is writable "
                  f"(collection: {ctx.collection.name})")
        else:
            print(f"  {WARN} could not create/write {dest_parent}. "
                  f"Phase 7 will fail until this is fixed.")
    else:
        print(f"  {WARN} {NAS_HOST}:{NAS_DST_ROOT}/ missing or not writable.")
        print(f"     Create the shared folder 'Data_Michael_restructured' via "
              f"DSM → Control Panel → Shared Folder (one-time).")
        # Not a hard blocker until Phase 7

    # SSHFS mount liveness check (only relevant when --source-from-mount).
    # A stale mount makes os.stat() return ENOTCONN; later stages would
    # crash with cryptic tracebacks. Catch it here with a clear fix hint.
    if ctx.source_from_mount:
        from kb.config import NAS_MOUNT
        # Probe a CHILD path (not the mount root): stat'ing the mount root
        # often succeeds even on a stale mount because the FUSE handle is
        # still alive — it's only when we ask for a child that the SSH
        # round-trip happens and ENOTCONN surfaces.
        probe = NAS_MOUNT / "Data_Michael_restructured"
        try:
            _ = probe.exists()
            print(f"  {OK} SSHFS mount alive ({NAS_MOUNT})")
        except OSError as e:
            print(f"  {FAIL} SSHFS mount appears stale: {e}")
            print(f"     Fix:")
            print(f"       fusermount3 -uz {NAS_MOUNT}")
            print(f"       ./kb.py mount")
            ok = False
    return ok

def stage_rsync_in(ctx: Context) -> bool:
    if ctx.source_from_mount:
        # Mount mode: source_local already points at the SSHFS mount path.
        # No rsync needed — just sanity-check the mount is present and the
        # expected folder exists.
        print(f"{DIM}source-from-mount mode: skipping rsync-in.{RESET}")
        if not ctx.source_local.exists():
            print(f"{FAIL} source folder not reachable via the mount: "
                  f"{ctx.source_local}")
            print(f"  is the NAS mounted? try:  ./kb.py mount")
            return False
        # Quick count for reassurance
        try:
            n = sum(1 for p in ctx.source_local.rglob("*") if p.is_file())
            print(f"{OK} {ctx.source_local} reachable ({n:,} files)")
        except Exception as e:
            print(f"{WARN} could not walk {ctx.source_local}: {e}")
        return True
    src = f"{NAS_HOST}:{ctx.nas_source_full}/"
    dst = ctx.source_local
    dst.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync", "-a", "--info=progress2",
        "--rsync-path=/usr/bin/rsync",
        src, str(dst) + "/",
    ]
    rc = run_stream(cmd)
    if rc != 0:
        print(f"{FAIL} rsync from NAS failed (exit {rc})")
        return False
    # Quick stats
    rc2, out, _ = run_capture(["find", str(dst), "-type", "f"], timeout=30)
    n = len(out.splitlines()) if rc2 == 0 else 0
    du = shutil.disk_usage(dst)
    print(f"{OK} mirrored {n:,} files to {dst}  "
          f"(free here: {du.free // (1<<30):,} GiB)")
    return True

# Sentinel file inside data/ that records WHICH subset currently owns the
# working data (CSVs, embeddings, extracted text). Used by the startup
# stale-state detector below to invalidate per-subset state when another
# subset has overwritten data/ since this one's last save.
DATA_OWNER_FILE = ROOT / "data" / ".current_subset"

# Stages whose marked-complete status depends on data/<*.csv/*.npy/*>.
# These all consume or produce files in the shared data/ directory, so
# they must be re-run whenever data/ is wiped or reassigned to a different
# subset. (Stages 0, 1 don't touch data/; stage 10 rsyncs target_local;
# stage 11 reads Qdrant.)
DATA_DEPENDENT_STAGES = {2, 3, 4, 5, 6, 7, 8, 9}


def _write_data_owner(slug: str) -> None:
    DATA_OWNER_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_OWNER_FILE.write_text(slug + "\n")


def _read_data_owner() -> str | None:
    try:
        return DATA_OWNER_FILE.read_text().strip()
    except (FileNotFoundError, OSError):
        return None


def stage_reset(ctx: Context) -> bool:
    targets = [
        ROOT / "data" / "extracted_text",
    ]
    csvs = list((ROOT / "data").glob("*.csv"))
    npys = list((ROOT / "data").glob("*.npy"))
    n = sum(1 for _ in targets if _.exists()) + len(csvs) + len(npys)
    if n == 0:
        print(f"{OK} data dir already clean")
        # Still record ownership so subsequent invalidation logic works
        _write_data_owner(ctx.subset)
        return True
    print(f"Will delete {len(csvs)} CSVs, {len(npys)} .npy files, and extracted_text/ ({sum(1 for _ in (ROOT/'data'/'extracted_text').rglob('*')) if (ROOT/'data'/'extracted_text').exists() else 0} files)")
    if not confirm("proceed"):
        return False
    for p in csvs + npys:
        try: p.unlink()
        except FileNotFoundError: pass
    if (ROOT / "data" / "extracted_text").exists():
        shutil.rmtree(ROOT / "data" / "extracted_text")
    (ROOT / "data" / "extracted_text").mkdir(parents=True)
    # Mark this subset as the current owner of data/. The next startup will
    # check this against state.ctx.subset to detect cross-subset interference.
    _write_data_owner(ctx.subset)
    print(f"{OK} reset done  (data/ now owned by {ctx.subset})")
    return True


def _invalidate_stale_data_stages(state: State) -> None:
    """
    Auto-recovery for the shared data/ directory: if another subset has
    overwritten data/ since this state was last saved, the data-dependent
    stages must re-run. We detect that by comparing data/.current_subset
    against state.ctx.subset, and un-mark stages 2..9 from completed when
    they don't match.

    Idempotent: if no invalidation is needed, this is a no-op.
    """
    owner = _read_data_owner()
    completed_data_stages = state.completed & DATA_DEPENDENT_STAGES
    if not completed_data_stages:
        return  # nothing to invalidate
    if owner == state.ctx.subset:
        return  # data/ still owned by this subset → state is consistent

    # Mismatch (or never-recorded) → invalidate.
    invalidated = sorted(completed_data_stages)
    state.completed -= DATA_DEPENDENT_STAGES
    state.save()
    if owner is None:
        reason = "no owner recorded in data/"
    else:
        reason = f"data/ now owned by {owner!r}, not {state.ctx.subset!r}"
    print(f"{WARN} working-data invalidated: {reason}")
    print(f"  un-marked stages {invalidated} so they re-run with this subset's "
          f"source files. (Stages 0, 1, 10, 11 left intact.)")
    print()

def _python(mod: str, *args: str) -> list[str]:
    return [str(ROOT / ".venv" / "bin" / "python"), "-m", mod, *args]

def stage_phase0(ctx: Context) -> bool:
    rc = run_stream(_python("src.phase0_manifest", "--source", str(ctx.source_local)))
    return rc == 0

def stage_phase1(ctx: Context) -> bool:
    for args in [
        _python("src.phase1_inventory", "--source", str(ctx.source_local)),
        _python("src.phase1_extract"),
        _python("src.phase1_lang_detect"),
    ]:
        rc = run_stream(args)
        if rc != 0:
            return False
    # Nice readout
    rc, out, _ = run_capture([
        str(ROOT / ".venv" / "bin" / "python"), "-c",
        "import pandas as pd; "
        "e=pd.read_csv('data/extraction_results.csv'); "
        "l=pd.read_csv('data/inventory_lang.csv'); "
        "print('status:', e.status.value_counts().to_dict()); "
        "print('lang:',   l.lang.value_counts().head().to_dict())",
    ])
    if rc == 0:
        print(out.strip())
    return True

def stage_phase2(ctx: Context) -> bool:
    # Free GPU first — bge-m3 in the embed step needs ~2 GB; running indexer
    # holds ~8 GB; together with chat-UI bge-m3 + Ollama qwen, we hit OOM.
    _pause_kb_indexer()
    rc = run_stream(_python("src.phase2_embed"))
    if rc != 0:
        return False
    rc = run_stream(_python("src.phase2_cluster",
                            "--min-cluster-size", "5", "--min-samples", "3"))
    return rc == 0

def stage_phase3(ctx: Context) -> bool:
    print(f"{WARN} This is the long phase — roughly 1 file/sec with qwen2.5:14b.")
    hint(f"  → in another terminal you can follow:  tail -f {ctx.log_path}")
    hint(f"  → or check GPU:                        nvidia-smi  (or  watch -n2 nvidia-smi)")
    hint(f"  → or watch Ollama:                     ollama ps")
    print()
    mode = prompt_choice(
        "Run in foreground (see live tqdm) or background (log to file, wait with polling)",
        {"f": "foreground", "b": "background"}, "f",
    )
    if mode == "f":
        rc = run_stream(_python("src.phase3_classify", "--source", str(ctx.source_local)))
        return rc == 0
    # Background mode
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = _python("src.phase3_classify", "--source", str(ctx.source_local))
    p = spawn_detached(argv, ctx.log_path)
    print(f"{OK} started Phase 3 in background (PID {p.pid}); log: {ctx.log_path}")
    print("Polling every 15 s. Ctrl-C to detach (the job continues).")
    try:
        while True:
            if p.poll() is not None:
                break
            time.sleep(15)
            tail = tail_last(ctx.log_path, 2)
            print(f"  {DIM}[running]{RESET} {tail}")
    except KeyboardInterrupt:
        print(f"\n{WARN} detaching. Phase 3 still running (PID {p.pid}).")
        print("  Resume this stage later with:  ./run.py --resume")
        return False
    rc = p.returncode
    if rc == 0:
        print(f"{OK} Phase 3 done. Log: {ctx.log_path}")
        return True
    print(f"{FAIL} Phase 3 exited {rc}. Inspect {ctx.log_path}")
    return False

def tail_last(path: Path, n: int) -> str:
    if not path.exists():
        return "(no log yet)"
    try:
        text = path.read_bytes()[-4096:].decode("utf-8", errors="replace")
    except Exception:
        return "(log read error)"
    # strip carriage-return tqdm lines
    text = text.replace("\r", "\n")
    lines = [l for l in text.splitlines() if l.strip()]
    return " | ".join(lines[-n:]) or "(empty)"

def stage_phase4_review(ctx: Context) -> bool:
    plan = ROOT / "data" / "rename_plan.csv"
    approved = ROOT / "data" / "rename_plan_approved.csv"
    if not plan.exists():
        print(f"{FAIL} data/rename_plan.csv missing — run Phase 3 first.")
        return False
    # In --auto-run mode, no human is at the browser. Skip launching the UI
    # entirely; Phase 5's plan-loading already falls back to rename_plan.csv
    # (where every row was pre-set to decision=approve by Phase 3).
    if AUTO_RUN:
        print(f"{DIM}auto-run: skipping Streamlit launch. "
              f"Phase 5 will use the un-edited rename_plan.csv "
              f"({sum(1 for _ in plan.open()) - 1} rows, all pre-approved).{RESET}")
        return True
    # Kill any existing streamlit
    run_capture("pgrep -af streamlit | grep -v grep | awk '{print $1}' | xargs -r kill || true", shell=True)
    time.sleep(1)
    venv_sl = ROOT / ".venv" / "bin" / "streamlit"
    argv = [
        str(venv_sl), "run", str(ROOT / "review_ui" / "review_ui.py"),
        "--server.address", "0.0.0.0",
        "--server.port", "8501",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--", "--plan", str(plan),
    ]
    log_path = LOGS_DIR / "streamlit.log"
    p = spawn_detached(argv, log_path)
    time.sleep(4)
    print(f"{OK} Streamlit launched (PID {p.pid}).")
    print(f"  {BOLD}Open:{RESET} {BLUE}{REVIEW_URL}{RESET}")
    print(f"  Log: {log_path}")
    print()
    print("Review the plan, edit/skip rows, click 'Save approved plan'.")
    print(f"Return here when you're done — type {BOLD}done{RESET} (or {BOLD}abort{RESET} to stop).")
    try:
        while True:
            raw = input(f"{CYAN}?{RESET} status: ").strip().lower()
            if raw == "done":
                if approved.exists():
                    break
                print(f"  {FAIL} {approved} not found. Did you click Save? Try again.")
            elif raw == "abort":
                try: p.terminate()
                except Exception: pass
                return False
            elif raw in ("url", "?"):
                print(f"  {BLUE}{REVIEW_URL}{RESET}")
            elif raw in ("log", "tail"):
                print("  " + tail_last(log_path, 5))
    finally:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except Exception: pass
    print(f"{OK} approved plan saved: {approved}")
    return True

def stage_phase5(ctx: Context) -> bool:
    plan = ROOT / "data" / "rename_plan_approved.csv"
    if not plan.exists():
        plan = ROOT / "data" / "rename_plan.csv"
        print(f"{WARN} using un-reviewed {plan} (no approved plan present)")
    ctx.target_local.parent.mkdir(parents=True, exist_ok=True)
    rc = run_stream(_python(
        "src.phase5_execute",
        "--plan",   str(plan),
        "--target", str(ctx.target_local),
        "--log",    str(ROOT / "data" / "execution_log.csv"),
    ))
    return rc == 0

def stage_phase6(ctx: Context) -> bool:
    rc = run_stream(_python(
        "src.phase6_verify",
        "--source", str(ctx.source_local),
        "--target", str(ctx.target_local),
    ))
    return rc == 0

def stage_phase7(ctx: Context) -> bool:
    # Ensure the collection's destination parent + leaf folder exist on NAS.
    # Note the slug (not the NAS name) is used for the leaf — e.g. 360F-A-Admin
    # becomes 360F/A-Admin/ (per Collection.strip_prefix).
    rc, _, _ = run_capture(
        ["ssh", NAS_HOST,
         f"[ -w '{NAS_DST_ROOT}' ] && mkdir -p '{ctx.nas_dest_full}'"],
        timeout=20,
    )
    if rc != 0:
        print(f"{FAIL} cannot create {ctx.nas_dest_full} on NAS.")
        print("  Make sure 'Data_Michael_restructured' shared folder exists (DSM Control Panel).")
        return False
    src = str(ctx.target_local) + "/"
    dst = f"{NAS_HOST}:{ctx.nas_dest_full}/"
    cmd = [
        "rsync", "-a", "--info=progress2",
        "--rsync-path=/usr/bin/rsync",
        src, dst,
    ]
    rc = run_stream(cmd)
    if rc != 0:
        print(f"{FAIL} rsync to NAS failed (exit {rc})")
        return False
    # Verify counts match on NAS
    rc, out, _ = run_capture(
        ["ssh", NAS_HOST,
         f"find '{ctx.nas_dest_full}' -type f | wc -l; "
         f"find '{ctx.nas_dest_full}' -type d | wc -l; "
         f"du -sh '{ctx.nas_dest_full}'"],
        timeout=60,
    )
    if rc == 0:
        print(f"{OK} NAS-side counts at {ctx.nas_dest_full}:")
        for line in out.splitlines():
            print(f"    {line}")
    return True


def stage_phase8_kb(ctx: Context) -> bool:
    """
    Trigger the KB delta scan for the variant that matches this subset's
    collection. Personal pipeline → KB_VARIANT=personal; 360F pipeline →
    KB_VARIANT=360f. The indexer auto-discovers any subset under the variant's
    base (Data_Michael_restructured/Personal/* or .../360F/*), so the new
    files just-rsync'd in Phase 7 are picked up without any config edit.

    Idempotent — if the indexer is already running (e.g. nightly timer fired
    in parallel), the second run skips files whose mtime+size haven't
    changed (mtime fast-path).
    """
    # Map collection name → KB_VARIANT
    variant = "personal" if ctx.collection.name.lower() == "personal" else "360f"
    print(f"Updating KB index (variant: {BOLD}{variant}{RESET}) — auto-discovers all subsets "
          f"under Data_Michael_restructured/{ctx.collection.dest_subpath}/")
    rc = run_stream(
        _python("kb.scheduled"),
        env={"KB_VARIANT": variant, "PYTHONPATH": str(ROOT)},
    )
    # Whether or not the indexer succeeded, free the GPU now that the run is
    # over — qwen2.5:14b otherwise sits in VRAM for OLLAMA_KEEP_ALIVE (5 min
    # default) holding ~9 GB. bge-m3 inside kb.scheduled is already gone
    # (subprocess exited).
    _unload_ollama_models()
    if rc != 0:
        print(f"{WARN} kb.scheduled exited non-zero ({rc}); some files may have been "
              f"skipped. The nightly timer will retry. You can also run manually:")
        print(f"  ./kb.py --variant {variant} reindex")
        return False
    host = "private" if variant == "personal" else "360f"
    print(f"{OK} KB index updated. Newly-indexed content is searchable now at "
          f"https://{host}.vitalus.net")
    return True


STAGES: list[Stage] = [
    Stage(0, "Pre-flight checks",
          "Verify ollama models, venv, SSH to NAS, and destination share.",
          stage_preflight),
    Stage(1, "Mirror subset from NAS to aizh",
          "rsync mgzh11:/volume1/Data_Michael/<NAS>/ → source_local/<SUBSET>/",
          stage_rsync_in),
    Stage(2, "Reset working data",
          "Delete data/*.csv, data/*.npy, data/extracted_text/ (destructive).",
          stage_reset),
    Stage(3, "Phase 0 — SHA-256 manifest",
          "Writes data/source_manifest.csv. Fast.",
          stage_phase0),
    Stage(4, "Phase 1 — inventory + extract + language",
          "Three back-to-back commands. Produces inventory/extraction/lang CSVs.",
          stage_phase1),
    Stage(5, "Phase 2 — embeddings + clustering",
          "bge-m3 on GPU, then HDBSCAN.",
          stage_phase2),
    Stage(6, "Phase 3 — LLM classification",
          "The long one (≈ 15–30 min). Choose foreground or background mode.",
          stage_phase3, long=True),
    Stage(7, "Phase 4 — Streamlit review",
          "Launches the UI; waits for you to save the approved plan.",
          stage_phase4_review),
    Stage(8, "Phase 5 — execute (copy + verify)",
          "Copy source → target_local with per-file SHA-256 verification.",
          stage_phase5),
    Stage(9, "Phase 6 — verify (counts, hashes, lint)",
          "Four checks. If any fail, STOP before Phase 7.",
          stage_phase6),
    Stage(10, "Phase 7 — rsync target_local → NAS",
           "Writes to mgzh11:/volume1/Data_Michael_restructured/<NAS>/ (never the source).",
           stage_phase7),
    Stage(11, "Phase 8 — update KB index (delta scan)",
           "Auto-fires kb.scheduled for the matching variant (Personal → KB_VARIANT=personal, "
           "360F → KB_VARIANT=360f). Auto-discovers any new subset under the variant's base.",
           stage_phase8_kb),
]

# ---------------------------------------------------------------------------
#  Main loop
# ---------------------------------------------------------------------------

def print_progress(state: State, current: int | None) -> None:
    mode = "  [source-from-mount]" if state.ctx.source_from_mount else ""
    banner(
        f"Folder Reorg Pipeline  —  {state.ctx.collection.name}/{state.ctx.subset}{mode}  "
        f"(NAS: '{state.ctx.nas_name}' → {state.ctx.nas_dest_full})"
    )
    for s in STAGES:
        if s.id in state.completed:
            glyph, col = OK, DIM
        elif current is not None and s.id == current:
            glyph, col = f"{YELLOW}▶{RESET}", BOLD
        else:
            glyph, col = PEND, DIM
        long_tag = f" {YELLOW}(long){RESET}" if s.long else ""
        print(f"  {glyph} {col}{s.id:>2}.  {s.title}{long_tag}{RESET}")
    print(f"  state: {DIM}{state.file}{RESET}")

def print_stage_details(s: Stage) -> None:
    banner(f"Stage {s.id} — {s.title}")
    print(s.description)
    print()

def next_pending(state: State, from_id: int = 0) -> int | None:
    for s in STAGES:
        if s.id < from_id:
            continue
        if s.id not in state.completed:
            return s.id
    return None

def menu(state: State, cur: int) -> str:
    choices = {"r": "run", "s": "skip (mark done)", "b": "back", "j": "jump", "l": "list", "q": "quit"}
    return prompt_choice("action", choices, "r")

def prompt_jump(state: State) -> int | None:
    raw = prompt(f"jump to stage id (0..{len(STAGES)-1})")
    if raw.isdigit() and 0 <= int(raw) < len(STAGES):
        return int(raw)
    print(f"  {FAIL} not a valid id")
    return None

HELP_DESCRIPTION = """\
Folder Reorg Pipeline — Interactive Wizard

Walks you through every stage of processing ONE source subset (e.g.
"F - Finance") end-to-end: rsync from the NAS, inventory, text extraction,
language detection, embedding, clustering, LLM classification, Streamlit
review, local copy, verification, and rsync back to the NAS.

Progress is persisted to data/runs/<subset>.state.json, so you can Ctrl-C
at any point and pick up later with --resume. You can open other terminal
windows in parallel to check progress — see the "Parallel terminals"
section below.
"""

HELP_EPILOG = """\
Examples:
  ./run.py                                       # pick a subset from a list
  ./run.py --subset F-Finance --nas-name "F - Finance"        # Personal
  ./run.py --subset A-Admin --nas-name "360F-A-Admin"         # 360F (auto-detected)
  ./run.py --collection 360F --nas-name "360F-A-Admin"        # explicit
  ./run.py --resume                              # continue the last run
  ./run.py --subset C-Companies --nas-name "C - Companies" --auto-run
                                                 # unattended; auto-r every 60s
  ./run.py --resume --auto-run --auto-run-timeout 30

  # ---- batch / overnight runs (multiple subsets, no human at terminal) ----
  ./run.py --batch all --source-from-mount
                                                 # every Personal+360F subset
  ./run.py --batch personal --source-from-mount  # all Personal subsets
  ./run.py --batch 360f --source-from-mount      # all 360F subsets
  ./run.py --batch 1,4,7,8,10 --source-from-mount
                                                 # menu numbers from picker
  ./run.py --batch 16-25 --source-from-mount     # range (here: most of 360F)
  ./run.py --batch 1,4,7-10,16-20 --source-from-mount
                                                 # mix; restructured ones are
                                                 # RE-DONE by default
  ./run.py --batch 1,4,7-10,16-20 --skip-restructured --source-from-mount
                                                 # skip already-restructured ones
                                                 # resume + auto-r every 30s
  ./run.py --subset A-Admin --nas-name "360F-A-Admin" --source-from-mount
                                                 # skip rsync-in; read direct from NAS mount

Collections (source root → destination layout):
  Personal  /volume1/Data_Michael/*  →  /volume1/Data_Michael_restructured/Personal/<slug>/
  360F      /volume1/360F-*          →  /volume1/Data_Michael_restructured/360F/<slug>/
                                         (the leading "360F-" is stripped from the slug)

Stages (the wizard walks these in order):
   0  Pre-flight checks           ollama models, venv, NAS SSH, dest subshare
   1  Mirror subset from NAS      rsync → source_local/<subset>/
   2  Reset working data          clear data/*.csv, data/*.npy, extracted_text/
   3  Phase 0 — manifest          SHA-256 baseline of source
   4  Phase 1 — inventory+extract+lang
   5  Phase 2 — embed + cluster   bge-m3 + HDBSCAN
   6  Phase 3 — LLM classification  (the LONG one, ~15–30 min)
   7  Phase 4 — Streamlit review  UI at http://192.168.1.10:8501
   8  Phase 5 — execute           copy to target_local/ with SHA-256 verify
   9  Phase 6 — verify            counts, hashes, convention lint
  10  Phase 7 — rsync to NAS      writes under Data_Michael_restructured/<collection>/<slug>/
  11  Phase 8 — update KB index   auto-fires kb.scheduled for the matching variant
                                  (auto-discovers the new subset; ~1-15 min depending on size)

Action menu at each stage (default is R; press Enter):
  r  run the stage
  s  skip (mark done without running)  — used when a stage is already complete
  b  back one stage
  j  jump to a specific stage id
  l  redraw progress list
  q  save state and quit (resume later with --resume)

Unattended mode (--auto-run):
  Every prompt auto-defaults (usually to 'r') after --auto-run-timeout
  seconds. The countdown shows in each prompt as "(auto-X in Ns)". Press
  any key + Enter at the prompt to override; Ctrl-C aborts the wizard.
  Phase 4 (Streamlit review) is SKIPPED — no human reviews the plan,
  so Phase 5 falls back to rename_plan.csv (every row pre-set to
  decision=approve by Phase 3).

Source-from-mount mode (--source-from-mount):
  Skip Stage 1 (rsync-in) entirely. Sources are read directly from the
  SSHFS-mounted NAS (./kb.py mount). Zero local disk for source_local/.
  Phase 5's copy step reads over SSHFS (~28 MB/s) instead of local SSD,
  so big subsets take ~30 min longer; Phase 3 unchanged. Good for
  quick one-off runs where you want to avoid the rsync pull.

Parallel-terminal helpers (run these in another SSH window while the wizard is
running):
  tail -f logs/phase3_<subset>.log        # live Phase 3 progress
  nvidia-smi                              # GPU load (watch -n2 nvidia-smi for live)
  ollama ps                               # which model is resident in VRAM
  ss -lntp | grep 8501                    # confirm Streamlit listening
  pgrep -af phase3_classify | grep -v grep
  wc -l data/rename_plan.csv              # plan size once Phase 3 finishes

Files to know about:
  docs/run-on-aizh.md    — the step-by-step manual with copy/pasteable commands
  docs/pipeline-overview.md — architecture / what each phase does
  data/runs/<subset>.state.json — persistent state, safe to delete to start over
"""


# ---------------------------------------------------------------------------
#  Batch mode (overnight unattended runs across many subsets)
# ---------------------------------------------------------------------------

def _discover_all_entries(verbose: bool = True
        ) -> tuple[list[tuple[Collection, str]], dict[tuple[str, str], str]]:
    """
    Discover every subset across every collection, in the SAME order the
    interactive picker uses (Personal first, then 360F, both alphabetical).

    Returns:
      entries       — list of (Collection, nas_name) in menu order. Index i
                      in this list corresponds to menu number i+1.
      restructured  — dict {(collection_name, nas_name): mtime_str} for every
                      subset that already has a destination folder on the
                      NAS. mtime_str is 'YYYY-MM-DD HH:MM' (or "" if mtime
                      could not be parsed). Membership tests still work via
                      `(col.name, nas_name) in restructured`.
    """
    entries: list[tuple[Collection, str]] = []
    restructured: dict[tuple[str, str], str] = {}
    if verbose:
        print(f"{BLUE}Discovering subsets on {NAS_HOST} …{RESET}")
    for col in COLLECTIONS:
        names, done = list_collection_subsets(col)
        if verbose and names:
            print()
            print(f"  {BOLD}{col.name}{RESET}  {DIM}({col.source_root}"
                  + (f" · {col.name_glob}" if col.name_glob != "*" else "")
                  + f"){RESET}")
        for name in names:
            slug = derive_subset_slug(col, name)
            n = len(entries) + 1
            mark = ""
            if slug in done:
                ts = done[slug]
                restructured[(col.name, name)] = ts
                ts_part = f", at {ts}" if ts else ""
                mark = f"  {GREEN}(restructured{ts_part}){RESET}"
            if verbose:
                print(f"  {n:>3}. {name}{mark}")
            entries.append((col, name))
    return entries, restructured


def _resolve_nas_name(slug: str,
                      collection: Collection | None) -> str | None:
    """
    Look up the actual NAS folder name for a given local slug, by asking
    discover_roots() for every subset and finding the one whose slug
    matches. Returns None when no match exists (NAS unreachable, slug
    typo, subset hasn't been created yet, etc.) so the caller can fall
    back to a heuristic / interactive prompt.

    `collection`, if provided, restricts the search to that collection
    so a slug existing in BOTH (e.g. "F-Finance" under Personal AND
    360F) is disambiguated.
    """
    try:
        entries, _ = _discover_all_entries(verbose=False)
    except Exception:
        return None
    for col, nas_name in entries:
        if collection is not None and col.name != collection.name:
            continue
        if derive_subset_slug(col, nas_name) == slug:
            return nas_name
    return None


def _smart_nas_name_default(slug: str) -> str:
    """
    Reverse-derive a plausible NAS folder name from a slug.

    Heuristic: split on the first '-' (the letter-prefix separator) and
    expand it to ' - '; replace remaining '-' with single spaces (those
    were word separators inside the original NAS name).

    Examples:
      "F-Finance"           -> "F - Finance"
      "G-Gesundheit-Health" -> "G - Gesundheit Health"
      "X-perm"              -> "X - perm"

    Used only as a fallback when `_resolve_nas_name` returns None
    (typically because the NAS isn't reachable). Always offered as a
    `prompt()` default so the user can override at the keyboard.
    """
    parts = slug.split("-", 1)
    if len(parts) != 2:
        return slug
    head, rest = parts
    return f"{head} - {rest.replace('-', ' ')}"


def _parse_batch_spec(spec: str,
                      entries: list[tuple[Collection, str]]
                      ) -> list[tuple[Collection, str]]:
    """
    Resolve a --batch SPEC into the selected entries.

    Accepted forms (case-insensitive, whitespace ignored):
      all                 → every entry
      personal            → every entry in collection "Personal"
      360f                → every entry in collection "360F"
      <int>               → menu number (1-based)
      <int>-<int>         → inclusive range
      mix:  1,4,7-10,16   → comma-combined
    """
    s = (spec or "").strip().lower()
    if not s:
        raise SystemExit("--batch: empty spec")

    if s == "all":
        return list(entries)
    if s in ("personal", "360f"):
        wanted = "Personal" if s == "personal" else "360F"
        return [(c, n) for (c, n) in entries if c.name == wanted]

    # Number / range parsing
    chosen_idx: list[int] = []
    for raw_tok in s.split(","):
        tok = raw_tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise SystemExit(f"--batch: bad range token {raw_tok!r}")
            if lo > hi:
                lo, hi = hi, lo
            for i in range(lo, hi + 1):
                if not (1 <= i <= len(entries)):
                    raise SystemExit(
                        f"--batch: index {i} out of range "
                        f"(have {len(entries)} entries)")
                chosen_idx.append(i)
        else:
            try:
                i = int(tok)
            except ValueError:
                raise SystemExit(
                    f"--batch: not a number / range / keyword: {raw_tok!r}")
            if not (1 <= i <= len(entries)):
                raise SystemExit(
                    f"--batch: index {i} out of range "
                    f"(have {len(entries)} entries)")
            chosen_idx.append(i)

    # Deduplicate while preserving order
    seen: set[int] = set()
    out: list[tuple[Collection, str]] = []
    for i in chosen_idx:
        if i in seen:
            continue
        seen.add(i)
        out.append(entries[i - 1])
    return out


def _run_one_subset_unattended(state: State) -> bool:
    """
    Drive every pending stage for one subset to completion, with no menu and
    no prompts. Returns True iff every stage from `cur` to the end succeeded.

    On the first failed stage we stop processing this subset (so a broken
    Phase 6 doesn't push half-verified data to the NAS), record the state,
    and return False — the batch caller moves on to the next subset.
    """
    cur = next_pending(state)
    while cur is not None:
        print_progress(state, cur)
        print_stage_details(STAGES[cur])
        try:
            ok = STAGES[cur].run(state.ctx)
        except KeyboardInterrupt:
            # Bubble up so the batch loop can decide whether to abort entirely
            raise
        except Exception as e:
            print(f"\n{FAIL} stage {cur} raised: {e}")
            ok = False
        if ok:
            state.completed.add(cur)
            state.save()
            print(f"\n{OK} stage {cur} complete")
            cur = next_pending(state, cur + 1)
        else:
            print(f"\n{FAIL} stage {cur} failed in unattended mode — "
                  f"giving up on this subset.")
            return False
    return True


def run_batch(spec: str,
              source_from_mount: bool,
              skip_restructured: bool,
              countdown_secs: int = 10) -> int:
    """
    Run a list of subsets back-to-back unattended. Returns 0 if every
    selected subset finished cleanly (or was deliberately skipped), 1 if any
    subset failed or was interrupted.

    By default, ALREADY-RESTRUCTURED subsets are re-processed (with a
    `(re-do; previously restructured at …)` tag in the plan). Pass
    `skip_restructured=True` to leave them alone.
    """
    entries, restructured = _discover_all_entries(verbose=True)
    if not entries:
        print(f"{FAIL} no subsets discovered — is SSH to {NAS_HOST} reachable?")
        return 1
    selected = _parse_batch_spec(spec, entries)
    if not selected:
        print(f"{FAIL} --batch spec resolved to zero subsets")
        return 1

    # Show the resolved plan and give a Ctrl-C window
    print()
    banner(f"BATCH PLAN  ({len(selected)} subset(s))")
    will_run: list[tuple[Collection, str]] = []
    for i, (col, nas_name) in enumerate(selected, 1):
        slug = derive_subset_slug(col, nas_name)
        is_done = (col.name, nas_name) in restructured
        ts = restructured.get((col.name, nas_name), "")
        ts_part = f", at {ts}" if ts else ""
        if is_done and skip_restructured:
            print(f"  {i:>3}. {WARN} {col.name} / {nas_name}  "
                  f"{DIM}(already restructured{ts_part} — will skip; "
                  f"omit --skip-restructured to re-do){RESET}")
        else:
            tag = (f"  {DIM}(re-do; previously restructured{ts_part}){RESET}"
                   if is_done else "")
            print(f"  {i:>3}. {OK} {col.name} / {nas_name} → "
                  f"{BOLD}{slug}{RESET}{tag}")
            will_run.append((col, nas_name))

    print()
    if source_from_mount:
        hint("Source mode: read directly from SSHFS mount "
             "(no local copy under source_local/).")
    else:
        hint("Source mode: rsync NAS → source_local/ per subset "
             "(needs ~50–80 GB free per subset on aizh).")
    if not will_run:
        # Only reachable when --skip-restructured filtered everything out.
        print(f"{WARN} nothing to do — every selected subset is already "
              f"restructured and --skip-restructured is set. "
              f"Omit the flag to re-process them.")
        return 0

    print(f"\n{BOLD}{CYAN}Starting in {countdown_secs}s — press Ctrl-C to abort.{RESET}")
    try:
        for remaining in range(countdown_secs, 0, -1):
            print(f"  {remaining}…", end="\r", flush=True)
            time.sleep(1)
        print(" " * 20, end="\r")  # clear the countdown line
    except KeyboardInterrupt:
        print(f"\n{WARN} batch aborted before start")
        return 1

    # Drive each subset
    started = datetime.now()
    results: list[tuple[str, str, str, str]] = []   # (col, nas_name, slug, status)
    for i, (col, nas_name) in enumerate(will_run, 1):
        slug = derive_subset_slug(col, nas_name)
        banner(f"[{i}/{len(will_run)}]  {col.name} / {nas_name}  →  {slug}")
        try:
            ctx = Context(
                collection=col,
                subset=slug,
                nas_name=nas_name,
                source_from_mount=source_from_mount,
            )
        except Exception as e:
            print(f"{FAIL} failed to build Context for {slug}: {e}")
            results.append((col.name, nas_name, slug, "context-error"))
            continue

        state = State.load(slug, collection=col) or State(ctx=ctx)
        # If a previously-saved state used a different source mode (mount vs
        # rsync), re-base it onto the current mode so paths line up.
        if state.ctx.source_from_mount != source_from_mount:
            state.ctx = ctx
        # If another subset has touched data/ since this state was saved,
        # un-mark stages 2..9 so they re-run with this subset's working
        # data instead of crashing on stale CSVs.
        _invalidate_stale_data_stages(state)
        state.save()

        try:
            ok = _run_one_subset_unattended(state)
        except KeyboardInterrupt:
            print(f"\n{WARN} batch interrupted by user during {slug}.")
            results.append((col.name, nas_name, slug, "interrupted"))
            # Print summary of what we did so far before exiting
            _print_batch_summary(results, datetime.now() - started)
            return 1
        results.append((col.name, nas_name, slug, "ok" if ok else "fail"))

    _print_batch_summary(results, datetime.now() - started)
    bad = [r for r in results if r[3] not in ("ok",)]
    return 0 if not bad else 1


def _print_batch_summary(results: list[tuple[str, str, str, str]],
                         elapsed) -> None:
    banner("BATCH SUMMARY")
    for col_name, nas_name, slug, status in results:
        if status == "ok":
            marker = OK
        elif status in ("context-error", "interrupted", "fail"):
            marker = FAIL
        else:
            marker = WARN
        print(f"  {marker} [{col_name:<8}] {nas_name:<32} → {slug:<24} "
              f"{DIM}[{status}]{RESET}")
    n_ok = sum(1 for *_, s in results if s == "ok")
    n_fail = len(results) - n_ok
    print(f"\n{BOLD}{n_ok} ok · {n_fail} not-ok{RESET}   "
          f"elapsed: {elapsed}")
    if n_fail:
        print(f"{DIM}Resume any failed subset with: "
              f"./run.py --subset <slug> --resume{RESET}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG,
    )
    ap.add_argument("--subset",
                    help="Local slug for this subset (used in path names), "
                         "e.g. F-Finance, A-Admin. If omitted, the wizard "
                         "lists the available NAS subsets and prompts you.")
    ap.add_argument("--nas-name",
                    help="The EXACT NAS folder name, e.g. 'F - Finance' or "
                         "'360F-A-Admin'. If omitted, derived from --subset "
                         "or picked interactively.")
    ap.add_argument("--collection",
                    choices=[c.name for c in COLLECTIONS],
                    help="Which collection (Personal or 360F) this subset "
                         "belongs to. If omitted, auto-detected from the NAS "
                         "folder name via each collection's name-glob.")
    ap.add_argument("--resume", action="store_true",
                    help="Ignore --subset/--collection; load the most "
                         "recently updated state file from data/runs/ and "
                         "continue that run.")
    ap.add_argument("--auto-run", action="store_true",
                    help="Unattended mode: every prompt auto-defaults to its "
                         "default after --auto-run-timeout seconds (so each "
                         "stage just runs). Phase 4 (Streamlit review) is "
                         "skipped entirely — Phase 5 falls back to the "
                         "un-edited rename_plan.csv (every row pre-approved). "
                         "Press any key + Enter at any prompt to override.")
    ap.add_argument("--auto-run-timeout", type=int, default=60, metavar="SEC",
                    help="Seconds to wait at each prompt before auto-defaulting. "
                         "Only effective with --auto-run. Default: 60.")
    ap.add_argument("--source-from-mount", action="store_true",
                    help="Skip Stage 1 rsync-in. Read source files directly "
                         "from the SSHFS-mounted NAS at /home/michael.gerber/nas/. "
                         "Saves ~60 GB of local disk per subset (nothing lands "
                         "in source_local/). Trade-off: Phase 5 reads each "
                         "file over SSHFS once (~28 MB/s) instead of local "
                         "SSD, so big subsets take ~30 min longer. Phase 3 "
                         "(LLM) timing is unchanged either way. Requires the "
                         "NAS mount (./kb.py mount).")
    ap.add_argument("--batch", metavar="SPEC",
                    help="Run multiple subsets back-to-back unattended "
                         "(implies --auto-run). SPEC accepts: "
                         "'all' | 'personal' | '360f' | menu numbers as shown "
                         "by the interactive picker (1-based), with commas and "
                         "ranges. Examples: '1,4,7,10' · '16-25' · "
                         "'1,4,7-10,16-20'. Already-restructured subsets are "
                         "RE-DONE by default; pass --skip-restructured to "
                         "leave them alone. On a stage failure the current "
                         "subset is abandoned and the batch continues with "
                         "the next.")
    ap.add_argument("--skip-restructured", action="store_true",
                    help="With --batch: skip any subset whose destination "
                         "folder already exists on the NAS. Default is to "
                         "re-process them (the run picks up where the prior "
                         "wizard left off via the per-subset state file, so "
                         "completed stages are auto-skipped — only changed "
                         "or never-run stages execute again).")
    # Back-compat: --include-restructured was the previous name for the
    # NOW-DEFAULT behaviour. Accept it silently so old invocations keep
    # working; it has no effect since processing-everything is the default.
    ap.add_argument("--include-restructured", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--batch-countdown", type=int, default=10, metavar="SEC",
                    help="With --batch: seconds to wait after printing the "
                         "plan before starting work, so you can Ctrl-C if the "
                         "selection looks wrong. Default: 10. Set to 0 to skip.")
    return ap.parse_args()

def main() -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    args = parse_args()

    # One-time migration of pre-namespacing scratch files. Cheap, idempotent,
    # and prints what it does. Order matters: state files first, so the log
    # migration can use them to look up each slug's collection.
    _migrate_legacy_state_files()
    _migrate_legacy_log_files()

    global AUTO_RUN, AUTO_RUN_TIMEOUT
    # --batch is unattended by definition; force --auto-run on.
    if args.batch and not args.auto_run:
        args.auto_run = True
    AUTO_RUN = bool(args.auto_run)
    AUTO_RUN_TIMEOUT = max(1, int(args.auto_run_timeout))

    # Batch mode short-circuits everything else: discover, resolve, run.
    if args.batch:
        if args.subset or args.resume:
            print(f"{FAIL} --batch is mutually exclusive with --subset / --resume.")
            return 2
        # Back-compat notice for users still passing the old flag.
        if args.include_restructured:
            print(f"{DIM}note: --include-restructured is now the default; "
                  f"the flag is accepted for back-compat but has no effect. "
                  f"Pass --skip-restructured to opt out.{RESET}")
        return run_batch(
            spec=args.batch,
            source_from_mount=bool(args.source_from_mount),
            skip_restructured=bool(args.skip_restructured),
            countdown_secs=max(0, int(args.batch_countdown)),
        )

    if AUTO_RUN:
        print(f"{BOLD}{CYAN}--auto-run enabled.{RESET} "
              f"Each prompt auto-defaults after {AUTO_RUN_TIMEOUT}s. "
              f"Phase 4 (Streamlit review) is skipped — "
              f"Phase 5 will use the un-edited plan.")
        print(f"{DIM}Press Ctrl-C at any time to abort. Any keystroke + Enter "
              f"overrides the auto-default at a prompt.{RESET}")
        print()

    state: State | None = None
    if args.resume:
        # Pick the most recently modified state file. Search BOTH the new
        # per-collection subdirs (data/runs/<col>/<slug>.state.json) AND the
        # legacy depth-1 location (data/runs/<slug>.state.json).
        files: list[Path] = []
        if STATE_DIR.exists():
            files = sorted(
                list(STATE_DIR.glob("*.state.json"))         # legacy
                + list(STATE_DIR.glob("*/*.state.json")),    # new namespaced
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        if not files:
            print(f"{FAIL} no state files in {STATE_DIR}")
            return 1
        # Load directly from the chosen path so we don't have to round-trip
        # through (subset, collection) lookup logic.
        state = State._from_path(files[0])
        if state is None:
            print(f"{FAIL} could not parse {files[0]}")
            return 1
        print(f"{OK} resumed state: {state.file}")
        _invalidate_stale_data_stages(state)
    elif args.subset:
        # Resolve --collection up front so we can use it to look up nas_name.
        col_hint: Collection | None = None
        if args.collection:
            col_hint = find_collection_by_name(args.collection)
            if col_hint is None:
                print(f"{FAIL} unknown --collection {args.collection!r}. "
                      f"Valid: {', '.join(c.name for c in COLLECTIONS)}")
                return 1
        # nas_name resolution priority:
        #   1. Explicit --nas-name (always wins)
        #   2. Live NAS lookup: ask discover_roots() for the actual folder
        #      whose slug == args.subset (under col_hint if given). Reliable
        #      even when slug -> nas_name reverse derivation is ambiguous
        #      (e.g. "G-Gesundheit-Health" -> "G - Gesundheit Health"
        #      with a SPACE between the words, not a hyphen).
        #   3. Heuristic derivation: split on first '-' for the prefix,
        #      replace remaining '-' with spaces. Prompt with this default.
        if args.nas_name:
            nas_name = args.nas_name
        else:
            looked_up = _resolve_nas_name(args.subset, col_hint)
            if looked_up:
                nas_name = looked_up
                print(f"{DIM}[nas-name auto-resolved from NAS: "
                      f"{nas_name!r}]{RESET}")
            else:
                nas_name = prompt(
                    "NAS folder name (exact)",
                    default=_smart_nas_name_default(args.subset),
                )
        col = col_hint or find_collection_for(nas_name)
        if not args.collection:
            print(f"{DIM}[collection auto-detected from name: {col.name}]{RESET}")
        ctx = Context(
            collection=col,
            subset=args.subset,
            nas_name=nas_name,
            source_from_mount=bool(args.source_from_mount),
        )
        # Pass collection to State.load so a same-slug state file from a
        # DIFFERENT collection (e.g. legacy Personal F-Finance) is not
        # mistakenly loaded as if it belonged to this run.
        existing = State.load(ctx.subset, collection=col)
        if existing is None:
            state = State(ctx=ctx)
        else:
            # The completed list is what we care about preserving from
            # disk. The CTX (nas_name, source_from_mount, collection)
            # comes from the current invocation's CLI args + live NAS
            # lookup — a stale state file might have an outdated value
            # (e.g. nas_name='G - Gesundheit-Health' saved by a pre-fix
            # run, vs the correct 'G - Gesundheit Health' resolved now).
            # Rebase the state's ctx to ours and re-save if anything
            # changed, so subsequent stages use the right paths.
            if (existing.ctx.nas_name != ctx.nas_name
                or existing.ctx.source_from_mount != ctx.source_from_mount
                or existing.ctx.collection.name != ctx.collection.name):
                print(f"{WARN} state-file ctx was stale; refreshing it:")
                if existing.ctx.nas_name != ctx.nas_name:
                    print(f"  nas_name:           "
                          f"{existing.ctx.nas_name!r}  →  {ctx.nas_name!r}")
                if existing.ctx.source_from_mount != ctx.source_from_mount:
                    print(f"  source_from_mount:  "
                          f"{existing.ctx.source_from_mount}  →  "
                          f"{ctx.source_from_mount}")
                if existing.ctx.collection.name != ctx.collection.name:
                    print(f"  collection:         "
                          f"{existing.ctx.collection.name!r}  →  "
                          f"{ctx.collection.name!r}")
                existing.ctx = ctx
                existing.save()
            state = existing
        # Auto-recover from cross-subset interference in shared data/.
        _invalidate_stale_data_stages(state)
    else:
        ctx = pick_subset_interactive()
        # Honour the CLI flag even if the subset was picked interactively.
        if args.source_from_mount and not ctx.source_from_mount:
            ctx.source_from_mount = bool(args.source_from_mount)
            ctx.__post_init__()   # re-derive source_local
        existing = State.load(ctx.subset, collection=ctx.collection)
        if existing and confirm(f"Resume existing state ({len(existing.completed)} stages done)", default=True):
            state = existing
        else:
            state = State(ctx=ctx)
            state.save()
        _invalidate_stale_data_stages(state)

    cur = next_pending(state)
    while cur is not None:
        print_progress(state, cur)
        print_stage_details(STAGES[cur])
        try:
            action = menu(state, cur)
        except SystemExit:
            raise
        if action == "r":
            ok = False
            try:
                ok = STAGES[cur].run(state.ctx)
            except KeyboardInterrupt:
                print(f"\n{WARN} stage interrupted")
            if ok:
                state.completed.add(cur)
                state.save()
                print(f"\n{OK} stage {cur} complete")
                cur = next_pending(state, cur + 1)
            else:
                # In --auto-run mode, the menu would auto-default back to
                # 'r' on the next iteration → infinite retry loop on a
                # deterministic failure. Bail with a clear exit code so
                # the operator can fix the underlying issue and re-run.
                if AUTO_RUN:
                    print(f"\n{FAIL} stage {cur} failed under --auto-run; "
                          f"aborting to avoid an infinite retry loop.")
                    print(f"  state saved at: {state.file}")
                    print(f"  fix the issue, then resume with: "
                          f"./run.py --resume  "
                          f"(or re-invoke with --subset / --collection)")
                    return 1
                print(f"\n{FAIL} stage {cur} did not finish cleanly — "
                      f"fix & retry, or skip/quit")
        elif action == "s":
            if confirm(f"Mark stage {cur} complete WITHOUT running it", default=False):
                state.completed.add(cur)
                state.save()
                cur = next_pending(state, cur + 1)
        elif action == "b":
            cur = max(0, cur - 1)
        elif action == "j":
            j = prompt_jump(state)
            if j is not None:
                cur = j
        elif action == "l":
            # 'l' just re-renders the progress listing (the next loop iteration will).
            pass
        elif action == "q":
            print(f"{OK} state saved: {state.file}  — resume with  ./run.py --resume")
            return 0

    # All done
    banner("ALL STAGES COMPLETE")
    print(f"Subset {BOLD}{state.ctx.subset}{RESET} "
          f"({state.ctx.collection.name}/{state.ctx.nas_name}) is live on:")
    print(f"  {BOLD}{NAS_HOST}:{state.ctx.nas_dest_full}/{RESET}")
    print()
    print("Clean up local mirrors to reclaim disk on aizh:")
    coll = state.ctx.collection.dest_subpath
    print(f"  rm -rf source_local/{coll}/{state.ctx.subset} "
          f"target_local/{coll}/{state.ctx.subset}")
    print()
    print("Next subset: run ./run.py again.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
