"""
Folder-reorg dashboard — single Streamlit app consolidating:

  · Status   — snapshot of pipeline + KB activity (port of status.py)
  · Chat     — KB chat UI (port of chat_ui/chat_ui.py, variant from sidebar)
  · KB       — kb.py operations (status / reindex / remove / cache-flush)
  · Pipeline — run.py operations (subset list, run, batch, resume, log tail)

Variant selector lives in the sidebar; one selection applies across all
pages. Bind to 0.0.0.0:8500 for LAN access at http://192.168.1.10:8500.
No Cloudflare — purely local.

Launch (from repo root):
  ./dashboard/launch.sh

Or manually:
  .venv/bin/streamlit run dashboard/dashboard.py \\
      --server.address 0.0.0.0 --server.port 8500 \\
      --server.headless true --browser.gatherUsageStats false
"""
from __future__ import annotations

# Make the project root importable from page modules so they can do
# `from kb.indexer import …`, `from src import …`, etc.
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from dashboard._common import variant_selector, variant_meta


st.set_page_config(
    page_title="folder-reorg dashboard",
    page_icon="🗂",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Sidebar (shared across all pages) -------------------------------------
st.sidebar.title("folder-reorg")
st.sidebar.caption("Personal + 360F unified dashboard")
variant = variant_selector()
st.sidebar.divider()
st.sidebar.markdown(
    "**Pages**\n\n"
    "Use the page list above to switch between Status, Chat, KB, and Pipeline. "
    "The variant selector applies to all pages."
)
st.sidebar.divider()
st.sidebar.caption(
    "LAN access: this dashboard is reachable on the local network "
    "at `http://<aizh-ip>:8500`. It is not exposed via Cloudflare Tunnel."
)


# --- Landing page ----------------------------------------------------------
meta = variant_meta(variant)
st.title("🗂 folder-reorg dashboard")
st.markdown(
    f"**Active variant**: <span style='background:{meta['color']};color:white;"
    f"padding:2px 10px;border-radius:6px;'>{meta['label']}</span> "
    f"— Qdrant `{meta['collection']}` on `{meta['qdrant_url']}`",
    unsafe_allow_html=True,
)

st.markdown("""
### Pages

| Page | Purpose |
|---|---|
| 📊 **Status** | What's running right now, GPU state, recent KB scans, last-completed pipeline subsets |
| 💬 **Chat** | RAG chat over the active variant's knowledge base |
| 🔍 **Knowledge Base** | KB status, manual reindex, remove a root, flush extraction cache |
| 🛠 **Pipeline** | Walk subsets through the 11-stage restructuring pipeline; batch & resume |

### Switching variants
The sidebar **Variant** selector controls which stack each page acts on.
Select once and every page reads the choice — the chat queries the right
collection, KB ops talk to the right Qdrant, pipeline state file paths
resolve under the right collection subdirectory.

### Reference docs
- [`docs/run-on-aizh.md`](https://github.com/michaelGoogle/folderReorg/blob/main/docs/run-on-aizh.md) — operational runbook
- [`docs/knowledge-base.md`](https://github.com/michaelGoogle/folderReorg/blob/main/docs/knowledge-base.md) — KB architecture
- [`docs/setup.md`](https://github.com/michaelGoogle/folderReorg/blob/main/docs/setup.md) — first-time host install
""")

# Quick health sanity check
from dashboard._common import qdrant_collection_info, pgrep_lines
col_info = qdrant_collection_info(variant)
n_points = col_info.get("points", 0)
streamlit_old = pgrep_lines(r"streamlit run chat_ui")

cols = st.columns(3)
with cols[0]:
    st.metric("Indexed chunks", f"{n_points:,}",
              help=f"Total points in `{meta['collection']}`")
with cols[1]:
    st.metric("Qdrant status", col_info.get("status", "?"))
with cols[2]:
    n_old = sum(1 for l in streamlit_old if "8502" in l or "8503" in l)
    st.metric("Old chat instances", n_old,
              help="Pre-dashboard Streamlit chat processes (8502/8503). "
                   "Kill once you've verified the dashboard works.")
