"""
Status page — web port of status.py. Same data sources, same logic
(currently-running detection, GPU state, recent state files, recent KB
scans, services, NAS mount). Variant-agnostic — it shows the whole host.
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import re
import subprocess
import time
from datetime import datetime

import streamlit as st

from dashboard._common import variant_selector, fmt_age, ROOT


st.set_page_config(page_title="Status — folder-reorg",
                   page_icon="📊", layout="wide")
st.title("📊 Status")
variant_selector()


# Auto-refresh control
col_refresh, col_now, _ = st.columns([1, 2, 5])
with col_refresh:
    auto = st.toggle("Auto-refresh (5s)", value=False, key="status_auto")
with col_now:
    st.caption(f"as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if auto:
    # Poor-man's auto-refresh — re-run the page every 5 s
    time.sleep(5)
    st.rerun()


# ---------------------------------------------------------------------------
# Tiny shell helpers (mirroring status.py)
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", ""
    except Exception:
        return 1, "", ""


def _pgrep(pattern: str) -> list[str]:
    rc, out, _ = _run(["pgrep", "-af", pattern])
    if rc != 0:
        return []
    return [l for l in out.splitlines() if l.strip() and "grep" not in l]


_STAGE_PATTERNS = [
    ("Stage 3  Phase 0  manifest",          r"src\.phase0_manifest"),
    ("Stage 4  Phase 1  inventory",         r"src\.phase1_inventory"),
    ("Stage 4  Phase 1  extract",           r"src\.phase1_extract"),
    ("Stage 4  Phase 1  lang detect",       r"src\.phase1_lang_detect"),
    ("Stage 5  Phase 2  embed (bge-m3)",    r"src\.phase2_embed"),
    ("Stage 5  Phase 2  cluster (HDBSCAN)", r"src\.phase2_cluster"),
    ("Stage 6  Phase 3  LLM classify",      r"src\.phase3_classify"),
    ("Stage 8  Phase 5  execute (copy)",    r"src\.phase5_execute"),
    ("Stage 9  Phase 6  verify",            r"src\.phase6_verify"),
    ("KB indexer  (kb.scheduled / kb.py reindex)",
     r"kb\.scheduled|kb\.py.*(reindex|index)"),
]


# ---------------------------------------------------------------------------
# CURRENTLY RUNNING
# ---------------------------------------------------------------------------
st.subheader("Currently running")

runpy_lines = _pgrep(r"run\.py")
runpy_alive = bool(runpy_lines)

c1, c2, c3 = st.columns(3)
with c1:
    if runpy_alive:
        st.success(f"▸ Wizard active ({len(runpy_lines)} process(es))")
        for l in runpy_lines:
            st.code(l, language=None)
    else:
        st.info("· No `run.py` wizard / batch active")

with c2:
    matched = []
    for label, pat in _STAGE_PATTERNS:
        lines = _pgrep(pat)
        for l in lines:
            matched.append((label, l))
    if matched:
        st.success(f"▸ Stage subprocess ({len(matched)} found)")
        for label, l in matched[:8]:
            pid = l.split(" ", 1)[0]
            st.markdown(f"**{label}** &nbsp;<span style='color:#888'>pid {pid}</span>",
                        unsafe_allow_html=True)
            st.caption(l)
    else:
        if runpy_alive:
            st.info("· Wizard alive, between stages")
        else:
            st.info("· No pipeline / KB subprocess")

with c3:
    rc, ollama_out, _ = _run(["ollama", "ps"])
    rc_smi, smi, _ = _run([
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    smi_line = ""
    if rc_smi == 0 and smi.strip():
        toks = [t.strip() for t in smi.splitlines()[0].split(",")]
        if len(toks) == 4:
            util, mu, mt, t = toks
            smi_line = (f"util {util}%, mem "
                        f"{int(mu)/1024:.1f}/{int(mt)/1024:.1f} GB, "
                        f"temp {t}°C")

    has_models = (rc == 0 and ollama_out.strip()
                  and len(ollama_out.splitlines()) > 1)
    if has_models:
        st.success("▸ GPU / Ollama")
        for l in ollama_out.splitlines()[1:]:
            cells = l.split()
            if not cells:
                continue
            model = cells[0]
            m = re.search(r"(\d+(?:\.\d+)?)\s*([KMGT]B)\b", l)
            sz = f"{m.group(1)} {m.group(2)}" if m else "?"
            EXPECTED = ("qwen2.5:14b", "bge-m3")
            if any(model.startswith(p) for p in EXPECTED):
                st.markdown(f"**{model}** ({sz})")
            else:
                st.warning(f"⚠ {model} ({sz}) — NOT a folder-reorg model")
    else:
        st.info("· No Ollama models resident in VRAM")
    if smi_line:
        st.caption(smi_line)


# ---------------------------------------------------------------------------
# LAST COMPLETED
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Last completed")

# Most recent fully-completed subset (>= 11/12 stages)
state_dir = ROOT / "data" / "runs"
candidates = []
for col in ("Personal", "360F"):
    cd = state_dir / col
    if cd.is_dir():
        for p in cd.glob("*.state.json"):
            try:
                d = json.loads(p.read_text())
                if len(d.get("completed", [])) >= 11:
                    candidates.append((p, d))
            except Exception:
                continue
candidates.sort(key=lambda pd: pd[0].stat().st_mtime, reverse=True)

# Most recent clean KB scan
clean_scans = []
for v in ("personal", "360f"):
    vd = ROOT / "kb" / "data" / v
    if vd.is_dir():
        for p in vd.glob("last_scan_*.json"):
            try:
                d = json.loads(p.read_text())
                if not d.get("errors"):
                    clean_scans.append((v, p, d))
            except Exception:
                continue
clean_scans.sort(key=lambda vpd: vpd[1].stat().st_mtime, reverse=True)

cc1, cc2 = st.columns(2)
with cc1:
    st.markdown("**Most recently restructured subset**")
    if candidates:
        p, d = candidates[0]
        col, slug = d.get("collection", "?"), d.get("subset", "?")
        nas = d.get("nas_name", "?")
        n_done = len(d.get("completed", []))
        st.success(f"✓ [{col}] {slug}")
        st.caption(f"{nas} • {n_done}/12 stages • "
                   f"saved {fmt_age(p.stat().st_mtime)}")
    else:
        st.info("· No subsets completed yet")

with cc2:
    st.markdown("**Most recent clean KB scan**")
    if clean_scans:
        v, p, d = clean_scans[0]
        st.success(f"✓ [{v}] {d.get('root', '?')}")
        st.caption(
            f"files={d.get('scanned_files', '?')} "
            f"chunks+={d.get('chunks_added', 0)} "
            f"err=0 • {fmt_age(p.stat().st_mtime)}"
        )
    else:
        st.info("· No clean scans yet")


# ---------------------------------------------------------------------------
# RECENT PIPELINE STATE (table)
# ---------------------------------------------------------------------------
st.divider()
n_rows = st.slider("How many rows", 5, 30, 10, key="status_rows")

st.subheader(f"Recent pipeline state (top {n_rows})")
all_states = []
for col in ("Personal", "360F"):
    cd = state_dir / col
    if cd.is_dir():
        for p in cd.glob("*.state.json"):
            try:
                d = json.loads(p.read_text())
                all_states.append((p, d))
            except Exception:
                continue
all_states.sort(key=lambda pd: pd[0].stat().st_mtime, reverse=True)

if not all_states:
    st.info("· No state files")
else:
    rows = []
    N = 12
    for p, d in all_states[:n_rows]:
        n_done = len(d.get("completed", []))
        if n_done >= N:
            mark = "✓"
        elif n_done >= N - 1:
            mark = "✓"
        elif n_done > 0:
            mark = "⚠"
        else:
            mark = "·"
        rows.append({
            "": mark,
            "Collection": d.get("collection", "?"),
            "Slug":       d.get("subset", "?"),
            "NAS name":   d.get("nas_name", "?"),
            "Stages":     f"{n_done}/{N}",
            "Saved":      datetime.fromtimestamp(p.stat().st_mtime)
                                   .strftime("%Y-%m-%d %H:%M"),
            "Age":        fmt_age(p.stat().st_mtime),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# RECENT KB SCANS (table)
# ---------------------------------------------------------------------------
st.subheader(f"Recent KB scans (top {n_rows})")
scans = []
for v in ("personal", "360f"):
    vd = ROOT / "kb" / "data" / v
    if vd.is_dir():
        for p in vd.glob("last_scan_*.json"):
            try:
                d = json.loads(p.read_text())
                scans.append((v, p, d))
            except Exception:
                continue
scans.sort(key=lambda vpd: vpd[1].stat().st_mtime, reverse=True)

if not scans:
    st.info("· No KB scan summaries on disk")
else:
    rows = []
    for v, p, d in scans[:n_rows]:
        errs = len(d.get("errors", []))
        mark = "✓" if errs == 0 else ("⚠" if errs < 50 else "✗")
        rows.append({
            "": mark,
            "Variant":  v,
            "Root":     d.get("root", "?"),
            "Files":    d.get("scanned_files", "?"),
            "New":      d.get("new", 0),
            "Updated":  d.get("updated", 0),
            "Chunks+":  d.get("chunks_added", 0),
            "Skip":     d.get("skip", 0),
            "Err":      errs,
            "Saved":    datetime.fromtimestamp(p.stat().st_mtime)
                                .strftime("%Y-%m-%d %H:%M"),
            "Age":      fmt_age(p.stat().st_mtime),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# CHAT / SERVICES
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Chat UI / services")

col_a, col_b = st.columns(2)
with col_a:
    st.markdown("**Streamlit chat instances**")
    streamlit_lines = _pgrep(r"streamlit run chat_ui")
    by_port = {}
    for l in streamlit_lines:
        m = re.search(r"--server\.port[ =](\d+)", l)
        if m:
            by_port[m.group(1)] = l
    for port, label in [("8502", "personal (legacy)"),
                        ("8503", "360f (legacy)"),
                        ("8500", "dashboard")]:
        if port in by_port:
            st.markdown(f"✓ `:{port}` — {label}")
        else:
            mark = "✗" if port == "8500" else "·"
            st.markdown(f"{mark} `:{port}` — {label} not running")

with col_b:
    st.markdown("**Qdrant containers**")
    rc, out, _ = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    if rc == 0:
        running = {}
        for l in out.splitlines():
            parts = l.split("\t", 1)
            if len(parts) == 2:
                running[parts[0]] = parts[1]
        for variant in ("personal", "360f"):
            wanted = f"qdrant-{variant}"
            match = next((cn for cn in running
                          if cn == wanted or cn.endswith(f"-{wanted}")), None)
            if match:
                st.markdown(f"✓ {match} — {running[match]}")
            else:
                st.markdown(f"✗ qdrant-{variant} not running")
    else:
        st.warning("Cannot query docker (permissions?)")


# ---------------------------------------------------------------------------
# NAS MOUNT
# ---------------------------------------------------------------------------
st.subheader("NAS mount")
NAS_MOUNT = Path("/home/michael.gerber/nas")
rc, out, _ = _run(["findmnt", "-n", str(NAS_MOUNT)])
if rc == 0 and out.strip():
    st.markdown(f"✓ {NAS_MOUNT} mounted")
    st.code(out.strip(), language=None)
    rc2, ls_out, _ = _run(["timeout", "3", "ls",
                           str(NAS_MOUNT / "Data_Michael_restructured")],
                          timeout=5)
    if rc2 == 0:
        n = len([l for l in ls_out.splitlines() if l.strip()])
        st.success(f"reachable: {n} entries under Data_Michael_restructured/")
    else:
        st.warning("findmnt OK but ls timed out — mount may be stale")
else:
    st.error(f"✗ {NAS_MOUNT} is NOT mounted. Fix: `./kb.py mount`")
