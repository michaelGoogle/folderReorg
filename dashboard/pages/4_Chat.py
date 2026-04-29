"""
Chat page — RAG chat over the active variant's KB. Variant comes from
the sidebar selector; queries route to the right Qdrant collection
explicitly without going through kb.config's import-time KB_VARIANT.

Mostly mirrors chat_ui/chat_ui.py: 2-column layout with chat on the
left and a sticky file-preview pane on the right; per-source
Preview / Download / Expand buttons; synthetic-match badge.
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import base64
import mimetypes

import streamlit as st

from dashboard._common import variant_selector, variant_meta, ROOT


st.set_page_config(page_title="Chat — folder-reorg",
                   page_icon="💬", layout="wide")

# Sticky right preview pane (same trick as chat_ui.py)
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

st.title("💬 Chat")
variant = variant_selector()
meta = variant_meta(variant)
st.caption(
    f"Querying the **{meta['label']}** knowledge base "
    f"(`{meta['collection']}` on `{meta['qdrant_url']}`)."
)


# ---------------------------------------------------------------------------
# Per-variant file resolution (rel_path → absolute on the NAS mount)
# ---------------------------------------------------------------------------
NAS_MOUNT = Path("/home/michael.gerber/nas")
DEST_SUBPATHS = {"personal": "Personal", "360f": "360F"}


def _resolve_abs(variant: str, root_name: str, rel_path: str) -> str | None:
    """Resolve a chunk's (root, rel_path) to an absolute file path under
    the NAS SSHFS mount. Returns None if the file isn't reachable."""
    base = NAS_MOUNT / "Data_Michael_restructured" / DEST_SUBPATHS[variant] / root_name
    p = base / rel_path
    return str(p) if p.is_file() else None


# ---------------------------------------------------------------------------
# Filters (sidebar — collapsed by default)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.divider()
    st.subheader("Filters")
    f_root = st.text_input("Root name (blank = all)", value="")
    f_language = st.selectbox("Language",
                              ["(any)", "en", "de", "fr", "it", "es", "nl", "pt"])
    f_yymm = st.text_input("YYMM prefix (e.g. 2023, 2312)", value="")
    f_compound = st.text_input("Compound prefix (e.g. FBUBS)", value="")
    top_k = st.slider("Top-k chunks", 3, 30, 5)
    st.caption("Filters are AND-combined; leave blank to match everything.")


# ---------------------------------------------------------------------------
# Source card renderer
# ---------------------------------------------------------------------------
def _render_source(s: dict, idx_key: str) -> None:
    abs_path = _resolve_abs(variant, s.get("root", ""), s["rel_path"])
    mime, _ = mimetypes.guess_type(s["filename"])
    mime = mime or "application/octet-stream"
    is_synthetic = s.get("text_source") == "synthetic"

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
            if st.button("📄 Preview", key=f"prev_{idx_key}",
                         use_container_width=True):
                st.session_state["preview"] = {
                    "filename": s["filename"], "abs_path": abs_path,
                    "mime": mime, "rel_path": s["rel_path"],
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
                    "⬇ Download", data=data, file_name=s["filename"],
                    mime=mime, key=f"dl_{idx_key}",
                    use_container_width=True,
                )
            except Exception:
                st.button("⬇ Download", key=f"dl_{idx_key}",
                          use_container_width=True, disabled=True)
        else:
            st.button("⬇ Download", key=f"dl_{idx_key}",
                      use_container_width=True, disabled=True)

    with col_exp:
        show = st.toggle("Expand", key=f"exp_{idx_key}", value=False)

    if show:
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
            st.info(
                f"📁 **Filename / folder match** — no document text was "
                f"extractable (extraction status: "
                f"`{s.get('extraction_status', '?')}`)."
            )
        st.code(s["text"][:800], language=None)


# ---------------------------------------------------------------------------
# Preview pane renderer
# ---------------------------------------------------------------------------
def _render_preview_pane() -> None:
    p = st.session_state.get("preview")
    if not p:
        st.info("📄 Click **Preview** on any source — the file appears here.")
        return
    head_l, head_r = st.columns([5, 1])
    with head_l:
        st.markdown(f"**{p['filename']}**")
        st.caption(p["rel_path"])
    with head_r:
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

    if mime.startswith("image/"):
        st.image(data, use_container_width=True)
        return

    if mime == "application/pdf":
        rendered = False
        if hasattr(st, "pdf"):
            try:
                st.pdf(data, height=800)
                rendered = True
            except Exception:
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
                st.warning(f"PDF is {size_mb:.1f} MB — use Download.")
        return

    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        try:
            st.code(data.decode("utf-8", errors="replace")[:50_000],
                    language={"text/markdown": "markdown",
                              "application/json": "json"}.get(mime))
        except Exception:
            st.warning("Could not decode text. Use Download.")
        return

    st.info(f"**{mime}** can't be previewed inline.")
    st.download_button(f"⬇ Download {p['filename']}", data=data,
                       file_name=p["filename"], mime=mime,
                       key="dl_from_preview", use_container_width=True)


# ---------------------------------------------------------------------------
# Chat history (per-variant — switching variants resets the chat)
# ---------------------------------------------------------------------------
hist_key = f"chat_history_{variant}"
if hist_key not in st.session_state:
    st.session_state[hist_key] = []
if "preview" not in st.session_state:
    st.session_state["preview"] = None

# 2-column layout
chat_col, preview_col = st.columns([1, 1], gap="large")

with chat_col:
    for msg_idx, msg in enumerate(st.session_state[hist_key]):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("sources"):
                st.markdown(f"**Sources ({len(msg['sources'])})**")
                for i, s in enumerate(msg["sources"]):
                    _render_source(s, idx_key=f"hist_{variant}_{msg_idx}_{i}")

with preview_col:
    st.markdown("##### 📑 File preview")
    _render_preview_pane()


# ---------------------------------------------------------------------------
# Prompt input
# ---------------------------------------------------------------------------
query = st.chat_input("Ask about your archive…")
if query:
    st.session_state[hist_key].append({"role": "user", "content": query})
    with chat_col:
        with st.chat_message("user"):
            st.markdown(query)

        from kb.query import answer
        kwargs = {
            "top_k": top_k,
            "qdrant_url": meta["qdrant_url"],
            "collection": meta["collection"],
        }
        if f_root.strip():     kwargs["root"] = f_root.strip()
        if f_language != "(any)": kwargs["language"] = f_language
        if f_yymm.strip():     kwargs["yymm_prefix"] = f_yymm.strip()
        if f_compound.strip(): kwargs["compound_prefix"] = f_compound.strip()

        with st.chat_message("assistant"):
            with st.spinner("retrieving + generating…"):
                result = answer(query, **kwargs)
            st.markdown(result.text)
            sources_payload = [
                {
                    "filename": s.filename, "rel_path": s.rel_path,
                    "compound": s.compound, "yymm": s.yymm,
                    "language": s.language, "score": s.score,
                    "text": s.text, "root": s.root,
                    "text_source": getattr(s, "text_source", "extracted"),
                    "extraction_status": getattr(s, "extraction_status", "ok"),
                }
                for s in result.sources
            ]
            if sources_payload:
                new_idx = len(st.session_state[hist_key])
                st.markdown(f"**Sources ({len(sources_payload)})**")
                for i, s in enumerate(sources_payload):
                    _render_source(s, idx_key=f"new_{variant}_{new_idx}_{i}")

    st.session_state[hist_key].append({
        "role": "assistant",
        "content": result.text,
        "sources": sources_payload,
    })
