"""
Streamlit review UI for the rename plan (plan §8.2).

Runs on the laptop (or aizh — anywhere you have network access to the plan CSV).
Read + edit + save back.

Usage:
    streamlit run review_ui/review_ui.py -- --plan /path/to/rename_plan.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import streamlit as st


def _args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan",     type=Path, required=True,
                    help="Path to rename_plan.csv (read)")
    ap.add_argument("--approved", type=Path, default=None,
                    help="Where to save the approved plan (default: sibling rename_plan_approved.csv)")
    return ap.parse_args()


def main() -> None:
    args = _args()
    approved = args.approved or args.plan.with_name(args.plan.stem + "_approved.csv")

    st.set_page_config(page_title="Rename plan review", layout="wide")
    st.title("Rename plan review")
    st.caption(f"Reading: {args.plan}")
    st.caption(f"Saving to: {approved}")

    if not args.plan.exists():
        st.error(f"Plan not found: {args.plan}")
        return

    df = pd.read_csv(args.plan)
    if "decision" not in df.columns:
        df["decision"] = "approve"

    # Filters — default to MEDIUM confidence only (user's chosen review priority)
    all_conf = sorted(df["confidence"].dropna().unique().tolist())
    default_conf = ["medium"] if "medium" in all_conf else all_conf
    with st.sidebar:
        st.header("Filter")
        conf = st.multiselect("Confidence", all_conf, default=default_conf,
                              help="Default: medium only. Clear to see high too.")
        kinds = st.multiselect("Kind", sorted(df["kind"].dropna().unique().tolist()),
                               default=sorted(df["kind"].dropna().unique().tolist()))
        cluster_filter = st.text_input("Cluster id (blank = all)")
        st.markdown("---")
        st.write(f"Total rows: {len(df):,}")
        st.write(f"By confidence: {df['confidence'].value_counts().to_dict()}")

    view = df
    if conf:
        view = view[view["confidence"].isin(conf)]
    if kinds:
        view = view[view["kind"].isin(kinds)]
    if cluster_filter.strip():
        try:
            cid = int(cluster_filter)
            view = view[view["cluster_id"] == cid]
        except ValueError:
            st.warning("cluster id must be an integer")

    st.write(f"Showing {len(view):,} rows")
    edited = st.data_editor(
        view,
        column_config={
            "decision": st.column_config.SelectboxColumn(
                options=["approve", "edit", "skip"], required=True
            ),
            "proposed_name":   st.column_config.TextColumn(width="large"),
            "proposed_parent": st.column_config.TextColumn(width="medium"),
            "current_path":    st.column_config.TextColumn(width="large"),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("Save approved plan", type="primary"):
            # Merge edits back into the full df by file_id
            merged = df.set_index("file_id")
            for r in edited.to_dict("records"):
                merged.loc[r["file_id"], merged.columns] = r
            merged.reset_index().to_csv(approved, index=False)
            st.success(f"Saved to {approved}")
    with col2:
        st.caption("Edits in-place in the table above are only persisted when you click Save.")


if __name__ == "__main__":
    main()
