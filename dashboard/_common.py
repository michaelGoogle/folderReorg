"""
Shared helpers used across the dashboard's pages.

The dashboard runs as a single Streamlit process serving multiple pages.
A variant selector lives in the sidebar; pages read the selected variant
from `st.session_state["variant"]` rather than the KB_VARIANT env var
(which kb.config reads at import time and can't easily be flipped
mid-process). All Qdrant / data-dir lookups in this module take the
variant as an explicit argument so pages don't accidentally talk to the
wrong stack.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import streamlit as st


# Repo root (this file lives at <repo>/dashboard/_common.py)
ROOT = Path(__file__).resolve().parent.parent

# Per-variant constants. Mirror what kb/config.py would compute, but
# keyed by an explicit `variant` argument so the dashboard can show
# both at once without import-time hacks.
VARIANTS = ("personal", "360f")

VARIANT_META = {
    "personal": {
        "label":      "Personal",
        "qdrant_url": "http://localhost:6333",
        "collection": "folderreorg_personal",
        "data_dir":   ROOT / "kb" / "data" / "personal",
        "state_dir":  ROOT / "data" / "runs" / "Personal",
        "logs_dir":   ROOT / "logs" / "Personal",
        "color":      "#2E7D32",
    },
    "360f": {
        "label":      "360F",
        "qdrant_url": "http://localhost:6433",
        "collection": "folderreorg_360f",
        "data_dir":   ROOT / "kb" / "data" / "360f",
        "state_dir":  ROOT / "data" / "runs" / "360F",
        "logs_dir":   ROOT / "logs" / "360F",
        "color":      "#1565C0",
    },
}


# ---------------------------------------------------------------------------
# Variant selector — single source of truth across pages
# ---------------------------------------------------------------------------
def variant_selector() -> str:
    """
    Render a sidebar variant selectbox and return the chosen variant
    (one of "personal" / "360f"). Persists in st.session_state.
    """
    if "variant" not in st.session_state:
        st.session_state["variant"] = "personal"
    selected = st.sidebar.selectbox(
        "Variant",
        options=list(VARIANTS),
        index=VARIANTS.index(st.session_state["variant"]),
        format_func=lambda v: VARIANT_META[v]["label"],
        key="variant_select",
    )
    if selected != st.session_state["variant"]:
        st.session_state["variant"] = selected
        st.rerun()
    # Color tag under the selector so it's obvious which stack you're acting on
    meta = VARIANT_META[selected]
    st.sidebar.markdown(
        f"<div style='background:{meta['color']};color:white;"
        f"padding:6px 12px;border-radius:6px;margin-top:4px;"
        f"text-align:center;font-weight:600;'>{meta['label']} stack</div>",
        unsafe_allow_html=True,
    )
    return selected


def variant_meta(variant: str | None = None) -> dict:
    return VARIANT_META[variant or get_variant()]


def get_variant() -> str:
    return st.session_state.get("variant", "personal")


# ---------------------------------------------------------------------------
# Qdrant access (variant-explicit; bypasses kb.config's import-time KB_VARIANT)
# ---------------------------------------------------------------------------
def qdrant_client(variant: str | None = None):
    from qdrant_client import QdrantClient
    return QdrantClient(url=variant_meta(variant)["qdrant_url"])


def qdrant_count_chunks(variant: str | None = None,
                        root_filter: str | None = None) -> int:
    """Total chunks in the variant's collection (optionally filtered by root)."""
    from qdrant_client.http import models as qm
    meta = variant_meta(variant)
    client = qdrant_client(variant)
    flt = None
    if root_filter:
        flt = qm.Filter(must=[
            qm.FieldCondition(key="root", match=qm.MatchValue(value=root_filter)),
        ])
    try:
        res = client.count(collection_name=meta["collection"],
                           count_filter=flt, exact=True)
        return int(getattr(res, "count", 0))
    except Exception:
        return 0


def qdrant_collection_info(variant: str | None = None) -> dict:
    """Status snapshot of the variant's collection."""
    meta = variant_meta(variant)
    try:
        client = qdrant_client(variant)
        info = client.get_collection(meta["collection"])
        return {
            "points":  getattr(info, "points_count", None)
                       or getattr(info, "vectors_count", None) or 0,
            "indexed": getattr(info, "indexed_vectors_count", 0),
            "status":  str(getattr(info, "status", "")),
        }
    except Exception as e:
        return {"error": str(e), "points": 0, "indexed": 0, "status": "error"}


# ---------------------------------------------------------------------------
# Subprocess launch + log tail (for kb.py reindex / run.py batch / etc.)
# ---------------------------------------------------------------------------
def bg_log_path(op: str) -> Path:
    """A timestamped, predictable log path under /tmp."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = Path("/tmp") / f"folderreorg-dashboard-{op}-{ts}.log"
    return p


def bg_run(argv: list[str],
           cwd: Path | None = None,
           log_file: Path | None = None,
           env_overrides: dict[str, str] | None = None) -> tuple[int, Path]:
    """
    Spawn `argv` as a detached background process, redirecting stdout +
    stderr to `log_file`. Returns (pid, log_file). The child survives
    Streamlit reruns and SSH disconnects (start_new_session=True puts
    it in its own process group, no SIGHUP propagation).
    """
    cwd = cwd or ROOT
    if log_file is None:
        log_file = bg_log_path("run")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **(env_overrides or {})}
    log_fh = open(log_file, "ab", buffering=0)
    log_fh.write(f"# Started {datetime.now().isoformat(timespec='seconds')}\n"
                 f"# argv = {argv}\n# cwd = {cwd}\n# env_overrides = "
                 f"{env_overrides}\n\n".encode())
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,   # detach: own process group, immune to SIGHUP
    )
    return proc.pid, log_file


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def pgrep_lines(pattern: str) -> list[str]:
    """Match host-side processes via pgrep -af. Empty list on no match
    or pgrep failure."""
    try:
        r = subprocess.run(["pgrep", "-af", pattern],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    return [l for l in r.stdout.splitlines() if l.strip() and "grep" not in l]


def tail_log(path: Path, n_lines: int = 60) -> str:
    """Last n_lines of a log file (best-effort, never raises)."""
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, max(n_lines * 200, 4096))
            f.seek(-chunk, 2)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception as e:
        return f"(log read failed: {e})"


# ---------------------------------------------------------------------------
# Misc small helpers
# ---------------------------------------------------------------------------
def fmt_age(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:    return f"{int(delta)}s ago"
    if delta < 3600:  return f"{int(delta/60)}m ago"
    if delta < 86400: return f"{int(delta/3600)}h ago"
    return f"{int(delta/86400)}d ago"


def fmt_mtime(path: Path) -> str:
    if not path.exists():
        return "—"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def venv_python() -> str:
    """Path to the venv's python — what we should invoke for kb.py / run.py."""
    candidate = ROOT / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else "python3"
