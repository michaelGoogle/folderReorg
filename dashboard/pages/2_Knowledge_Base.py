"""
Knowledge Base page — wraps `kb.py status / reindex / remove / cache-flush`
for the variant selected in the sidebar. Each long-running operation
spawns a detached subprocess that survives Streamlit reruns; the page
tails its log so the user can watch progress.
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import time

import streamlit as st

from dashboard._common import (
    variant_selector, variant_meta, get_variant,
    qdrant_collection_info, qdrant_count_chunks,
    bg_run, bg_log_path, is_pid_alive, pgrep_lines, tail_log,
    fmt_age, fmt_mtime, venv_python, ROOT,
)


st.set_page_config(page_title="Knowledge Base — folder-reorg",
                   page_icon="🔍", layout="wide")
st.title("🔍 Knowledge Base")
variant = variant_selector()
meta = variant_meta(variant)
st.caption(f"Active stack: **{meta['label']}** — Qdrant collection "
           f"`{meta['collection']}` on `{meta['qdrant_url']}`")


# ---------------------------------------------------------------------------
# Collection overview
# ---------------------------------------------------------------------------
info = qdrant_collection_info(variant)
mc1, mc2, mc3 = st.columns(3)
with mc1:
    st.metric("Indexed chunks", f"{info.get('points', 0):,}")
with mc2:
    st.metric("HNSW indexed", f"{info.get('indexed', 0):,}",
              help="Points with the HNSW index built. Builds lazily; "
                   "not all points need to be HNSW-indexed for chat to work.")
with mc3:
    st.metric("Status", info.get("status", "?"))


# ---------------------------------------------------------------------------
# Per-root last-scan summary table
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Per-root last-scan summary")

scans: list[tuple[Path, dict]] = []
for p in sorted(meta["data_dir"].glob("last_scan_*.json")):
    try:
        scans.append((p, json.loads(p.read_text())))
    except Exception:
        continue

if not scans:
    st.info("No `last_scan_<root>.json` files yet — nothing's been indexed "
            "for this variant.")
else:
    scans.sort(key=lambda pd: pd[0].stat().st_mtime, reverse=True)
    rows = []
    for p, d in scans:
        rows.append({
            "Root":        d.get("root", p.stem.removeprefix("last_scan_")),
            "Files":       d.get("scanned_files", "?"),
            "New":         d.get("new", 0),
            "Updated":     d.get("updated", 0),
            "Unchanged":   d.get("unchanged", 0),
            "Chunks+":     d.get("chunks_added", 0),
            "Skip":        d.get("skip", 0),
            "Errors":      len(d.get("errors", []) or []),
            "Scanned":     fmt_mtime(p),
            "Age":         fmt_age(p.stat().st_mtime),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🔄 Reindex")
st.markdown(
    "Runs a delta scan over every restructured subset under "
    f"`Data_Michael_restructured/{meta['label']}/`. Existing files hit the "
    "fast path (~30 s for thousands of unchanged files); only new / changed "
    "files get extracted and embedded."
)

# Detect a reindex already in flight for this variant
reindex_marker = f"kb_session_reindex_{variant}"
running_pid = st.session_state.get(reindex_marker, {}).get("pid")
running_log = st.session_state.get(reindex_marker, {}).get("log")
if running_pid and not is_pid_alive(running_pid):
    # Earlier launch finished
    st.session_state[reindex_marker] = {"pid": None, "log": running_log}
    running_pid = None

ri_cols = st.columns([1, 1, 4])
with ri_cols[0]:
    if running_pid:
        st.success(f"▸ Running (pid {running_pid})")
    else:
        if st.button("▶ Start reindex", key=f"reindex_btn_{variant}",
                     type="primary"):
            log = bg_log_path(f"reindex-{variant}")
            pid, log = bg_run(
                [venv_python(), str(ROOT / "kb.py"),
                 "--variant", variant, "reindex"],
                cwd=ROOT, log_file=log,
            )
            st.session_state[reindex_marker] = {"pid": pid, "log": str(log)}
            st.rerun()

with ri_cols[1]:
    if running_pid:
        if st.button("⏹ Stop (SIGINT)", key=f"reindex_stop_{variant}"):
            import os, signal
            try:
                os.kill(running_pid, signal.SIGINT)
                st.toast(f"Sent SIGINT to {running_pid}", icon="🛑")
            except Exception as e:
                st.error(f"Stop failed: {e}")
            time.sleep(2)
            st.rerun()

with ri_cols[2]:
    if running_log:
        log_path = Path(running_log)
        if log_path.exists():
            st.caption(f"log: `{running_log}`")

# Live log tail
if running_log:
    log_path = Path(running_log)
    if log_path.exists():
        with st.expander("Live log (last 60 lines)",
                         expanded=bool(running_pid)):
            st.code(tail_log(log_path, 60) or "(empty)", language=None)
            if running_pid:
                st.caption("Auto-refresh below; toggle on to watch live.")
                if st.toggle("Auto-refresh log (3s)",
                             key=f"reindex_autoref_{variant}"):
                    time.sleep(3)
                    st.rerun()


# ---------------------------------------------------------------------------
# Remove a root
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🗑 Remove a root from the KB")
st.markdown(
    "Deletes every chunk for one root from this variant's Qdrant "
    "collection plus the per-root `last_scan_<root>.json`. Does NOT touch "
    "the source files on the NAS or the pipeline state file."
)

available_roots = sorted(
    p.stem.removeprefix("last_scan_") for p in meta["data_dir"].glob("last_scan_*.json")
)
if not available_roots:
    st.info("No roots to remove — none have been indexed for this variant.")
else:
    rm_cols = st.columns([2, 1, 3])
    with rm_cols[0]:
        target_root = st.selectbox("Root", available_roots,
                                   key=f"rm_select_{variant}")
    with rm_cols[1]:
        keep_summary = st.checkbox("Keep last_scan",
                                   key=f"rm_keep_{variant}",
                                   help="Keep kb/data/<variant>/last_scan_<root>.json "
                                        "for post-mortem reference.")
    with rm_cols[2]:
        n_chunks = qdrant_count_chunks(variant, root_filter=target_root)
        st.caption(f"`{target_root}` has **{n_chunks:,} chunks** in `{meta['collection']}`")

    confirm_text = st.text_input(
        f"Type the root name **{target_root}** to confirm:",
        key=f"rm_confirm_{variant}",
    )
    if st.button(f"⚠ Remove {target_root}", key=f"rm_btn_{variant}",
                 type="primary", disabled=(confirm_text != target_root)):
        argv = [venv_python(), str(ROOT / "kb.py"),
                "--variant", variant, "remove", "--root", target_root, "-y"]
        if keep_summary:
            argv.append("--keep-summary")
        log = bg_log_path(f"remove-{variant}-{target_root}")
        pid, log = bg_run(argv, cwd=ROOT, log_file=log)
        # Wait briefly for completion (remove is fast)
        for _ in range(20):
            if not is_pid_alive(pid):
                break
            time.sleep(0.5)
        out = tail_log(log, 30)
        st.success(f"Done — see log: {log}")
        st.code(out or "(no output)", language=None)


# ---------------------------------------------------------------------------
# Extraction cache
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🧹 Extraction cache")
st.markdown(
    "The extraction cache (`extraction_cache.json`) records sha256 → status "
    "for files that consistently fail extraction (password-protected, "
    "corrupt, unsupported format). On the next reindex, cached files skip "
    "the slow `extract()` call entirely. Flush after installing a new "
    "extractor (e.g. `apt install antiword` for `.doc`)."
)

# Cache contents
cache_path = meta["data_dir"] / "extraction_cache.json"
if cache_path.exists():
    try:
        cache = json.loads(cache_path.read_text())
        from collections import Counter
        by_status: Counter = Counter(
            e.get("status", "?") for e in cache.get("entries", {}).values()
        )
        st.caption(f"`{cache_path.relative_to(ROOT)}` "
                   f"({fmt_age(cache_path.stat().st_mtime)}) "
                   f"— {sum(by_status.values()):,} entries")
        rows = [{"Status": k, "Count": v} for k, v in by_status.most_common()]
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.info("Cache file exists but is empty.")
    except Exception as e:
        st.error(f"Cache file unreadable: {e}")
else:
    st.info("No extraction cache yet — will be created on the next reindex.")

cache_cols = st.columns([1, 4])
with cache_cols[0]:
    if st.button("🧹 Flush cache", key=f"cache_flush_{variant}"):
        argv = [venv_python(), str(ROOT / "kb.py"),
                "--variant", variant, "cache-flush", "-y"]
        log = bg_log_path(f"cache-flush-{variant}")
        pid, log = bg_run(argv, cwd=ROOT, log_file=log)
        for _ in range(10):
            if not is_pid_alive(pid):
                break
            time.sleep(0.5)
        st.success(f"Flushed — log: {log}")
        st.code(tail_log(log, 20), language=None)
        st.rerun()


# ---------------------------------------------------------------------------
# Errors / skipped drilldown
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📋 Per-root errors / skipped detail")

if not scans:
    st.info("Nothing to drill into.")
else:
    drilldown_root = st.selectbox(
        "Root",
        [d.get("root", p.stem.removeprefix("last_scan_")) for p, d in scans],
        key=f"drill_root_{variant}",
    )
    selected = next(((p, d) for p, d in scans
                     if d.get("root", p.stem.removeprefix("last_scan_"))
                        == drilldown_root), None)
    if selected:
        p, d = selected
        errors = d.get("errors", []) or []
        skipped = d.get("skipped", []) or []

        tab_e, tab_s = st.tabs([f"Errors ({len(errors)})",
                                f"Skipped ({len(skipped)})"])

        with tab_e:
            if not errors:
                st.success("No errors.")
            else:
                from collections import Counter
                cats: Counter = Counter()
                for e in errors:
                    msg = e.split(": ", 1)[-1].strip()
                    cats[msg[:90]] += 1
                st.markdown("**Grouped by message family**")
                rows = [{"Count": c, "Message": m}
                        for m, c in cats.most_common()]
                st.dataframe(rows, use_container_width=True, hide_index=True)
                st.markdown("**Sample of affected files (up to 50)**")
                for e in errors[:50]:
                    path, _, msg = e.partition(": ")
                    short = msg[:160] + ("…" if len(msg) > 160 else "")
                    st.markdown(f"- `{path}`  \n  &nbsp;&nbsp;&nbsp;{short}")
                if len(errors) > 50:
                    st.caption(f"… and {len(errors) - 50} more")

        with tab_s:
            if not skipped:
                # Older scans don't have per-file skip records — only count
                if d.get("skip", 0) > 0:
                    st.info(
                        f"This scan recorded **{d.get('skip')}** skip count "
                        f"but no per-file detail (likely a pre-fix run). "
                        f"Run a fresh reindex to populate the skipped list."
                    )
                else:
                    st.success("No files skipped.")
            else:
                from collections import Counter
                by_reason: Counter = Counter(s.get("reason", "?") for s in skipped)
                st.markdown("**Grouped by reason**")
                rows = [{"Count": c, "Reason": f"skip:{r}"}
                        for r, c in by_reason.most_common()]
                st.dataframe(rows, use_container_width=True, hide_index=True)
                st.markdown("**Sample of affected files (up to 50)**")
                for s in skipped[:50]:
                    st.markdown(
                        f"- `skip:{s.get('reason', '?')}` &nbsp; "
                        f"`{s.get('path', '?')}`"
                    )
                if len(skipped) > 50:
                    st.caption(f"… and {len(skipped) - 50} more")
                if d.get("skipped_overflow", 0):
                    st.caption(f"… plus {d['skipped_overflow']} additional "
                               f"skips not recorded (per-root cap of 1000 hit).")
