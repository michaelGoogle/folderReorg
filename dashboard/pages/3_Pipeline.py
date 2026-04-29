"""
Pipeline page — wraps `run.py`. Browses subsets discovered on the NAS,
shows per-subset state, and lets you launch single-subset or batch runs
in detached subprocesses with live log tail.
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import os
import time
from datetime import datetime

import streamlit as st

from dashboard._common import (
    variant_selector, variant_meta, get_variant,
    bg_run, bg_log_path, is_pid_alive, pgrep_lines, tail_log,
    fmt_age, fmt_mtime, venv_python, ROOT,
)


st.set_page_config(page_title="Pipeline — folder-reorg",
                   page_icon="🛠", layout="wide")
st.title("🛠 Pipeline")
variant = variant_selector()
meta = variant_meta(variant)
st.caption(
    f"Active stack: **{meta['label']}** — "
    f"NAS source root, destination, and pipeline state files all "
    f"resolve under this collection."
)


# ---------------------------------------------------------------------------
# Discover subsets via run.py's helpers (live SSH to NAS — slow, ~3-10 s)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner="Discovering subsets on NAS …")
def _discover():
    # Lazy import — run.py is heavy and we don't want to load it on every rerun
    import run
    entries, restructured = run._discover_all_entries(verbose=False)
    return [
        {
            "collection": col.name,
            "nas_name":   nas,
            "slug":       run.derive_subset_slug(col, nas),
            "restructured_at": restructured.get((col.name, nas), ""),
        }
        for col, nas in entries
    ]


# Filter to the active variant
all_entries = _discover()
entries = [e for e in all_entries if e["collection"] == meta["label"]]


# ---------------------------------------------------------------------------
# Per-subset state file lookup
# ---------------------------------------------------------------------------
def _state_for(slug: str) -> dict | None:
    p = meta["state_dir"] / f"{slug}.state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _state_summary(state: dict | None, restructured_at: str) -> tuple[str, str]:
    """Return (status_marker, human_label)."""
    if not state:
        if restructured_at:
            return ("✓", f"restructured at {restructured_at} (no state file)")
        return ("·", "fresh — never run")
    n_done = len(state.get("completed", []))
    if n_done >= 12:
        return ("✓", f"done ({n_done}/12)")
    if n_done >= 11:
        return ("✓", f"done ({n_done}/12 — Stage 11 not saved)")
    if n_done > 0:
        return ("⚠", f"partial ({n_done}/12)")
    return ("·", "started but no progress")


# ---------------------------------------------------------------------------
# Subset table with per-subset run buttons
# ---------------------------------------------------------------------------
st.divider()
st.subheader(f"Subsets in {meta['label']}")
st.caption(f"{len(entries)} subset(s) discovered on the NAS")

# Refresh button
if st.button("🔄 Refresh from NAS", key=f"refresh_subsets_{variant}",
             help="Re-runs the SSH discovery (clears cache)"):
    _discover.clear()
    st.rerun()

# Build table
rows = []
for e in entries:
    state = _state_for(e["slug"])
    mark, label = _state_summary(state, e["restructured_at"])
    rows.append({
        "": mark,
        "NAS name":  e["nas_name"],
        "Slug":      e["slug"],
        "Status":    label,
        "Last save": fmt_mtime(meta["state_dir"] / f"{e['slug']}.state.json")
                     if state else "—",
    })
st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Single-subset run
# ---------------------------------------------------------------------------
st.divider()
st.subheader("▶ Run / resume a single subset")

if not entries:
    st.warning("No subsets discovered. Is the NAS mount alive?")
else:
    sub_cols = st.columns([3, 2, 2])
    with sub_cols[0]:
        sel_slug = st.selectbox(
            "Subset",
            [e["slug"] for e in entries],
            format_func=lambda s: next(
                (f"{e['slug']}  ({e['nas_name']})" for e in entries
                 if e["slug"] == s), s,
            ),
            key=f"single_sel_{variant}",
        )
        sel = next(e for e in entries if e["slug"] == sel_slug)
        sel_state = _state_for(sel_slug)
    with sub_cols[1]:
        auto_run = st.checkbox("--auto-run", value=True,
                               key=f"single_auto_{variant}",
                               help="Auto-default every prompt; skip Phase 4 review.")
        source_from_mount = st.checkbox("--source-from-mount", value=True,
                                        key=f"single_mount_{variant}",
                                        help="Read source via SSHFS instead of "
                                             "rsyncing 60 GB to local SSD.")
    with sub_cols[2]:
        if sel_state:
            st.markdown(
                f"**State exists.** {len(sel_state.get('completed', []))}/12 "
                f"stages done. The wizard will resume from the first pending "
                f"stage."
            )
        else:
            st.markdown("**No state.** The wizard will start from Stage 0.")

    # Detect a run already in flight for this subset
    marker = f"pipeline_session_run_{variant}_{sel_slug}"
    rec = st.session_state.get(marker, {})
    running_pid = rec.get("pid")
    if running_pid and not is_pid_alive(running_pid):
        st.session_state[marker] = {**rec, "pid": None}
        running_pid = None

    btn_cols = st.columns([1, 1, 1, 4])
    with btn_cols[0]:
        if running_pid:
            st.success(f"▸ Running (pid {running_pid})")
        else:
            if st.button("▶ Run", key=f"single_run_btn_{variant}",
                         type="primary"):
                argv = [
                    venv_python(), str(ROOT / "run.py"),
                    "--subset", sel_slug,
                    "--collection", meta["label"],
                    "--nas-name", sel["nas_name"],
                ]
                if auto_run: argv.append("--auto-run")
                if source_from_mount: argv.append("--source-from-mount")
                log = bg_log_path(f"run-{meta['label']}-{sel_slug}")
                pid, log = bg_run(argv, cwd=ROOT, log_file=log)
                st.session_state[marker] = {"pid": pid, "log": str(log)}
                st.rerun()

    with btn_cols[1]:
        if running_pid:
            if st.button("⏹ Stop", key=f"single_stop_{variant}"):
                import os, signal
                try: os.kill(running_pid, signal.SIGINT)
                except Exception: pass
                time.sleep(2)
                st.rerun()

    with btn_cols[2]:
        if sel_state and st.button("🗑 Delete state",
                                    key=f"single_del_state_{variant}",
                                    help="Clear the state file so the wizard "
                                         "starts from Stage 0 next time."):
            (meta["state_dir"] / f"{sel_slug}.state.json").unlink(missing_ok=True)
            st.success(f"Deleted state file for {sel_slug}")
            st.rerun()

    # Live log tail
    log_str = rec.get("log")
    if log_str:
        log_path = Path(log_str)
        if log_path.exists():
            with st.expander(
                    "Live log (last 60 lines)",
                    expanded=bool(running_pid),
            ):
                st.code(tail_log(log_path, 60) or "(empty)", language=None)
                if running_pid and st.toggle(
                        "Auto-refresh log (5s)",
                        key=f"single_autoref_{variant}"):
                    time.sleep(5)
                    st.rerun()


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📦 Batch — run multiple subsets")

st.markdown(
    "Pick a SPEC keyword (`all` / `personal` / `360f`) or a comma-separated "
    "list of menu numbers (1-based, in the same order as the interactive "
    "picker would show). The batch runs unattended — closes Phase 4, "
    "auto-defaults all prompts, and on a stage failure abandons that subset "
    "and moves to the next."
)

bc1, bc2 = st.columns([2, 3])
with bc1:
    spec_mode = st.radio(
        "What to run", ["all", "personal", "360f", "by numbers"],
        horizontal=True, key="batch_mode",
    )
    if spec_mode == "by numbers":
        spec = st.text_input("Numbers (e.g. 1,4,7-10,16-20)",
                             value="", key="batch_numbers")
    else:
        spec = spec_mode
with bc2:
    batch_skip_restructured = st.checkbox(
        "--skip-restructured (leave already-done subsets alone)",
        value=False, key="batch_skip",
    )
    batch_source_from_mount = st.checkbox(
        "--source-from-mount (no local 60 GB rsync)",
        value=True, key="batch_mount",
    )
    batch_countdown = st.slider("--batch-countdown (sec)", 0, 30, 0,
                                key="batch_countdown",
                                help="Seconds before batch starts; lets you "
                                     "Ctrl-C if the plan looks wrong. "
                                     "0 to skip the countdown entirely.")

# Detect batch in flight
batch_marker = "pipeline_session_batch"
brec = st.session_state.get(batch_marker, {})
batch_pid = brec.get("pid")
if batch_pid and not is_pid_alive(batch_pid):
    st.session_state[batch_marker] = {**brec, "pid": None}
    batch_pid = None

bb_cols = st.columns([1, 1, 4])
with bb_cols[0]:
    if batch_pid:
        st.success(f"▸ Batch running (pid {batch_pid})")
    else:
        disabled = (spec_mode == "by numbers" and not spec.strip())
        if st.button("▶ Start batch", key="batch_start", type="primary",
                     disabled=disabled):
            argv = [venv_python(), str(ROOT / "run.py"),
                    "--batch", spec,
                    "--batch-countdown", str(batch_countdown)]
            if batch_skip_restructured: argv.append("--skip-restructured")
            if batch_source_from_mount: argv.append("--source-from-mount")
            log = bg_log_path(f"batch-{spec_mode.replace(' ', '_')}")
            pid, log = bg_run(argv, cwd=ROOT, log_file=log)
            st.session_state[batch_marker] = {"pid": pid, "log": str(log),
                                              "started": time.time()}
            st.rerun()

with bb_cols[1]:
    if batch_pid:
        if st.button("⏹ Stop batch (SIGINT)", key="batch_stop"):
            import os, signal
            try: os.kill(batch_pid, signal.SIGINT)
            except Exception: pass
            time.sleep(2)
            st.rerun()

# Batch log
batch_log = brec.get("log")
if batch_log:
    log_path = Path(batch_log)
    if log_path.exists():
        with st.expander("Batch log (last 80 lines)",
                         expanded=bool(batch_pid)):
            st.code(tail_log(log_path, 80) or "(empty)", language=None)
            if batch_pid and st.toggle(
                    "Auto-refresh batch log (10s)",
                    key="batch_autoref"):
                time.sleep(10)
                st.rerun()


# ---------------------------------------------------------------------------
# Active wizard / stage summary
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Active processes")

active = pgrep_lines(r"run\.py")
if active:
    st.success(f"{len(active)} run.py process(es) active")
    for l in active:
        st.code(l, language=None)
else:
    st.info("No run.py wizard / batch active.")
