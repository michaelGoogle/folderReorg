"""
Streamlit chat UI for the folder-reorg knowledge base.

Run:
    ./kb.py chat                    (port 8502)
    streamlit run chat_ui/chat_ui.py --server.port 8502

Reach from your laptop:
    http://192.168.1.10:8502        (requires `sudo ufw allow 8502/tcp` on aizh)
"""

from __future__ import annotations

# Ensure the project root (parent of this file's dir) is on sys.path so that
# `import kb.*` works regardless of how streamlit was invoked. Streamlit's
# runner cd's into chat_ui/ before importing, which hides the top-level `kb/`.
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import mimetypes

import streamlit as st

from kb.config import (
    KB_VARIANT, QDRANT_COLLECTION, TOP_K, UI_COLOR, UI_LABEL,
    discover_roots,
)
from kb.indexer import collection_stats
from kb.query import answer


st.set_page_config(
    page_title=f"Folder-reorg KB — {UI_LABEL}",
    layout="wide",
    initial_sidebar_state="collapsed",   # left filter panel starts hidden;
                                         # toggle with the > chevron in header
)

# --- Sticky right-side preview pane ----------------------------------------
# Keep the second column of the page-level 2-column layout fixed in the
# viewport while the chat history (left column) scrolls. Targets the first
# stHorizontalBlock on the page, which is exactly our chat | preview split.
# Uses both `stColumn` and `column` test IDs to cover Streamlit version drift.
st.markdown("""
<style>
[data-testid="stHorizontalBlock"]:first-of-type {
    align-items: flex-start;
}
[data-testid="stHorizontalBlock"]:first-of-type > [data-testid="stColumn"]:nth-of-type(2),
[data-testid="stHorizontalBlock"]:first-of-type > [data-testid="column"]:nth-of-type(2) {
    position: sticky;
    top: 1rem;
    align-self: flex-start;
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
}
</style>
""", unsafe_allow_html=True)
st.title("Folder-reorg · Knowledge Base")


# ---- helpers -------------------------------------------------------------

def _resolve_abs(root_name: str, rel_path: str) -> str | None:
    """Resolve a chunk's (root_name, rel_path) into an absolute file path on
    aizh, for serving as a download. Re-discovers roots on every call so newly
    indexed subsets appear in the download buttons without a Streamlit restart."""
    import os
    paths = {name: str(path) for name, path in discover_roots()}
    base = paths.get(root_name)
    if not base:
        return None
    p = os.path.join(base, rel_path)
    return p if os.path.isfile(p) else None


def _render_source(s: dict, idx_key: str) -> None:
    """
    Render one source row:
       filename            [📄 Preview] [⬇ Download] [▾ Expand]
    Details (rel_path, compound/yymm/language tag, score, text excerpt)
    are hidden until the Expand toggle is flipped.

    Buttons:
      · 📄 Preview — sets st.session_state.preview, rendered by the
                     right-side preview pane (same browser tab).
      · ⬇ Download — direct download to disk.
      · ▾ Expand   — reveals metadata + text snippet for this chunk.
    """
    abs_path = _resolve_abs(s.get("root", ""), s["rel_path"])
    mime, _ = mimetypes.guess_type(s["filename"])
    mime = mime or "application/octet-stream"

    # "Synthetic" matches were indexed by filename + folder context only
    # (no text content was extractable — e.g. ZIP, image, OCR-empty PDF).
    # Show a small badge so the user knows the match is shallower than a
    # real-content match.
    is_synthetic = s.get("text_source") == "synthetic"

    # Single inline row: filename | Preview | Download | Expand
    col_name, col_prev, col_dl, col_exp = st.columns([5, 1.4, 1.4, 1.2])

    with col_name:
        if is_synthetic:
            st.markdown(
                f"**{s['filename']}**  "
                f"<span style='font-size:0.75em;background:#FFF3CD;"
                f"color:#664D03;padding:1px 6px;border-radius:8px;"
                f"margin-left:6px;'>📁 filename match</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(f"**{s['filename']}**")

    with col_prev:
        if abs_path:
            if st.button("📄 Preview", key=f"prev_{idx_key}", use_container_width=True):
                st.session_state["preview"] = {
                    "filename": s["filename"],
                    "abs_path": abs_path,
                    "mime":     mime,
                    "rel_path": s["rel_path"],
                }
        else:
            st.button("📄 Preview", key=f"prev_{idx_key}",
                      use_container_width=True, disabled=True)

    with col_dl:
        if abs_path:
            try:
                with open(abs_path, "rb") as f:
                    data = f.read()
                st.download_button(
                    label="⬇ Download",
                    data=data,
                    file_name=s["filename"],
                    mime=mime,
                    key=f"dl_{idx_key}",
                    use_container_width=True,
                )
            except Exception:
                st.button("⬇ Download", key=f"dl_{idx_key}",
                          use_container_width=True, disabled=True)
        else:
            st.button("⬇ Download", key=f"dl_{idx_key}",
                      use_container_width=True, disabled=True)

    with col_exp:
        # st.toggle keeps state per-key without forcing a manual rerun, and
        # sits on the same row visually as the buttons.
        show_details = st.toggle("Expand", key=f"exp_{idx_key}", value=False)

    if show_details:
        tag = " · ".join(filter(None,
            [s.get("compound"), s.get("yymm"), s.get("language")]))
        st.markdown(
            f"_{tag}_  <span style='color:#888'>score={s['score']:.3f}</span>",
            unsafe_allow_html=True,
        )
        st.caption(s["rel_path"])
        if not abs_path:
            st.caption(f"⚠ file not reachable on aizh "
                       f"(root={s.get('root') or '?'})")
        if is_synthetic:
            ext_status = s.get("extraction_status", "?")
            st.info(
                f"📁 **Filename / folder match** — no document text was "
                f"extractable from this file (extraction status: "
                f"`{ext_status}`). The match is based on the filename, "
                f"folder hierarchy, and metadata derivable from the "
                f"naming convention (compound · date · description)."
            )
        st.code(s["text"][:800], language=None)


def _render_preview_pane() -> None:
    """Right-side panel that renders the file selected via the Preview button.
    Lives in its own column; same browser tab as the chat."""
    import base64

    p = st.session_state.get("preview")
    if not p:
        st.info("📄 Click **Preview** on any source — the file appears here.")
        return

    # Header + close
    head_left, head_right = st.columns([5, 1])
    with head_left:
        st.markdown(f"**{p['filename']}**")
        st.caption(p["rel_path"])
    with head_right:
        if st.button("✕", key="close_preview", help="Close preview"):
            st.session_state.preview = None
            st.rerun()

    try:
        with open(p["abs_path"], "rb") as f:
            data = f.read()
    except Exception as e:
        st.error(f"Cannot read {p['abs_path']}: {e}")
        return

    mime = p["mime"]
    size_mb = len(data) / (1024 * 1024)

    # --- Image: native render --------------------------------------------
    if mime.startswith("image/"):
        st.image(data, use_container_width=True)
        return

    # --- PDF: st.pdf if available (and the extra is installed), else iframe
    # Note: hasattr(st, "pdf") is True on recent Streamlit versions even when
    # the optional `streamlit-pdf` extra is NOT installed — calling it raises
    # StreamlitAPIException at runtime. So we try it and fall back on failure.
    if mime == "application/pdf":
        rendered = False
        if hasattr(st, "pdf"):
            try:
                st.pdf(data, height=800)
                rendered = True
            except Exception:
                # streamlit-pdf extra missing → fall through to iframe path
                pass
        if not rendered:
            if size_mb <= 25:
                b64 = base64.b64encode(data).decode("ascii")
                st.markdown(
                    f'<iframe src="data:application/pdf;base64,{b64}" '
                    f'width="100%" height="800px" '
                    f'style="border:1px solid rgba(49,51,63,0.2);'
                    f'border-radius:0.5rem;"></iframe>',
                    unsafe_allow_html=True,
                )
            else:
                st.warning(f"PDF is {size_mb:.1f} MB — too large to render inline. "
                           f"Use Download instead.")
        return

    # --- Plain text / markdown / CSV: render as text ---------------------
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        try:
            st.code(data.decode("utf-8", errors="replace")[:50_000],
                    language={"text/markdown": "markdown",
                              "application/json": "json"}.get(mime))
        except Exception:
            st.warning("Could not decode text. Use Download.")
        return

    # --- Office / archives / binaries: cannot render in browser ----------
    st.info(f"**{mime}** can't be previewed inline. Click Download — your OS "
            f"will open it with the registered app.")
    st.download_button(
        label=f"⬇ Download {p['filename']}",
        data=data,
        file_name=p["filename"],
        mime=mime,
        key="dl_from_preview",
        use_container_width=True,
    )

# -----------------------------------------------------------------------------
# Sidebar: filters + collection stats
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header(f"{UI_LABEL} KB")
    st.caption(f"Variant: **{KB_VARIANT}** · "
               f"Collection: `{QDRANT_COLLECTION}`")
    try:
        stats = collection_stats()
        st.caption(f"{stats.get('points', 0):,} chunks indexed "
                   f"(status: {stats.get('status', '?')})")
    except Exception as e:
        st.error(f"Qdrant not reachable: {e}")
        st.stop()
    st.divider()
    st.header("Scope / filters")

    root = st.text_input("Root name (blank = all)", value="")
    language = st.selectbox("Language",
                            ["(any)", "en", "de", "fr", "it", "es", "nl", "pt"])
    yymm = st.text_input("YYMM prefix (e.g. 2023, 2312)", value="")
    compound = st.text_input("Compound prefix (e.g. FBUBS)", value="")
    top_k = st.slider("Top-k chunks", 3, 30, 5)

    st.markdown("---")
    st.caption("Filters are AND-combined; leave blank to match everything.")

# -----------------------------------------------------------------------------
# Chat history (simple, per-session)
# -----------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []  # list[{"role": "user"/"assistant", "content": str, "sources": list}]
if "preview" not in st.session_state:
    st.session_state.preview = None

# -----------------------------------------------------------------------------
# Two-column layout:
#   left  → chat history + assistant responses with source cards
#   right → file preview pane (driven by st.session_state.preview)
# st.chat_input is anchored to the bottom of the page (outside the columns).
# -----------------------------------------------------------------------------
chat_col, preview_col = st.columns([1, 1], gap="large")

with chat_col:
    for msg_idx, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("sources"):
                st.markdown(f"**Sources ({len(msg['sources'])})**")
                for i, s in enumerate(msg["sources"]):
                    _render_source(s, idx_key=f"hist_{msg_idx}_{i}")

with preview_col:
    st.markdown("##### 📑 File preview")
    _render_preview_pane()

# -----------------------------------------------------------------------------
# Prompt input (anchored to bottom of page, full width)
# -----------------------------------------------------------------------------
query = st.chat_input("Ask about your archive…")
if query:
    st.session_state.messages.append({"role": "user", "content": query})
    with chat_col:
        with st.chat_message("user"):
            st.markdown(query)

        kwargs = {"top_k": top_k}
        if root.strip():     kwargs["root"] = root.strip()
        if language != "(any)": kwargs["language"] = language
        if yymm.strip():     kwargs["yymm_prefix"] = yymm.strip()
        if compound.strip(): kwargs["compound_prefix"] = compound.strip()

        with st.chat_message("assistant"):
            with st.spinner("retrieving + generating…"):
                result = answer(query, **kwargs)
            st.markdown(result.text)
            sources_payload = [
                {
                    "filename": s.filename, "rel_path": s.rel_path,
                    "compound": s.compound, "yymm": s.yymm, "language": s.language,
                    "score": s.score, "text": s.text, "root": s.root,
                    "text_source": getattr(s, "text_source", "extracted"),
                    "extraction_status": getattr(s, "extraction_status", "ok"),
                }
                for s in result.sources
            ]
            if sources_payload:
                new_msg_idx = len(st.session_state.messages)   # index this message will have
                st.markdown(f"**Sources ({len(sources_payload)})**")
                for i, s in enumerate(sources_payload):
                    _render_source(s, idx_key=f"new_{new_msg_idx}_{i}")
    st.session_state.messages.append({
        "role": "assistant",
        "content": result.text,
        "sources": sources_payload,
    })
