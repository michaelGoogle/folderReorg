"""
RAG retrieval + answer generation.

Query flow:
  1. Embed query with bge-m3.
  2. Qdrant similarity search with optional metadata filters.
  3. Build RAG prompt from top-k chunks.
  4. Send to Ollama qwen2.5:14b.
  5. Return answer + cited sources.
"""

from __future__ import annotations

import requests
from dataclasses import dataclass

from kb.chunk_embed import embed
from kb.config import (
    LLM_MODEL,
    OLLAMA_URL,
    QDRANT_COLLECTION,
    QDRANT_URL,
    TOP_K,
)


@dataclass
class Source:
    rel_path: str
    filename: str
    compound: str | None
    yymm: str | None
    language: str
    score: float
    text: str
    chunk_id: int
    n_chunks: int
    root: str = ""       # logical root name (e.g. "F-Finance-restructured")
    # NEW (added with synthetic-context indexing):
    # text_source = "extracted" → chunk text is real document content
    # text_source = "synthetic" → chunk text was built from filename +
    #                              folder + parsed convention because the
    #                              file had no extractable text
    # extraction_status = "ok" / "password" / "corrupt" / "too_large" /
    #                     "unsupported" / "empty" / "no_chunks" / "unreadable"
    text_source: str = "extracted"
    extraction_status: str = "ok"


@dataclass
class Answer:
    text: str
    sources: list[Source]
    retrieved: int


# --- Retrieval -------------------------------------------------------------


def search(query: str,
           *,
           top_k: int = TOP_K,
           root: str | None = None,
           language: str | None = None,
           yymm_prefix: str | None = None,
           compound_prefix: str | None = None,
           qdrant_url: str | None = None,
           collection: str | None = None) -> list[Source]:
    """
    Vector search.

    `qdrant_url` and `collection` default to the values resolved at
    import time from kb.config (which read KB_VARIANT once). Pass them
    explicitly when the caller needs to switch variants per call —
    e.g. the unified dashboard's chat page where the user toggles
    Personal / 360F at runtime.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm

    url = qdrant_url or QDRANT_URL
    coll = collection or QDRANT_COLLECTION
    client = QdrantClient(url=url)
    qvec = embed([query])[0].tolist()

    must: list = []
    if root:
        must.append(qm.FieldCondition(key="root", match=qm.MatchValue(value=root)))
    if language:
        must.append(qm.FieldCondition(key="language", match=qm.MatchValue(value=language)))
    if yymm_prefix:
        must.append(qm.FieldCondition(key="yymm",
                                       match=qm.MatchText(text=yymm_prefix)))
    if compound_prefix:
        must.append(qm.FieldCondition(key="compound",
                                       match=qm.MatchText(text=compound_prefix)))
    flt = qm.Filter(must=must) if must else None

    # qdrant-client >= 1.10 renamed `search` → `query_points`.
    result = client.query_points(
        collection_name=coll,
        query=qvec,
        query_filter=flt,
        limit=top_k,
        with_payload=True,
    )
    hits = getattr(result, "points", result)
    return [
        Source(
            rel_path=(h.payload or {}).get("rel_path", ""),
            filename=(h.payload or {}).get("filename", ""),
            compound=(h.payload or {}).get("compound"),
            yymm=(h.payload or {}).get("yymm"),
            language=(h.payload or {}).get("language", "und"),
            score=float(h.score),
            text=(h.payload or {}).get("text", ""),
            chunk_id=int((h.payload or {}).get("chunk_id", 0)),
            n_chunks=int((h.payload or {}).get("n_chunks", 1)),
            root=(h.payload or {}).get("root", ""),
            # Default to "extracted" / "ok" so chunks indexed before this
            # field existed render unchanged in the chat UI.
            text_source=(h.payload or {}).get("text_source", "extracted"),
            extraction_status=(h.payload or {}).get("extraction_status", "ok"),
        )
        for h in hits
    ]


# --- RAG prompt + LLM ------------------------------------------------------


RAG_SYSTEM = """You are a precise assistant that answers questions about Michael Gerber's \
personal document archive. You must ground every factual claim in the provided document \
excerpts, and cite the source filename for each claim. If the excerpts do not contain \
the answer, say so explicitly — do NOT guess or invent figures.

Answer in English by default; switch to German if the user writes in German.

Format:
  <answer>

  Sources:
    - <filename>  [compound · yymm]
    - …
"""


def _build_user_prompt(query: str, sources: list[Source]) -> str:
    blocks = []
    for i, s in enumerate(sources, 1):
        header = f"[{i}] {s.filename}"
        if s.compound:
            header += f"  ({s.compound}"
            if s.yymm:
                header += f" · {s.yymm}"
            header += ")"
        blocks.append(header + "\n" + s.text[:1500])
    excerpts = "\n\n".join(blocks) if blocks else "(no matching excerpts)"
    return (
        f"Question: {query}\n\n"
        f"Document excerpts (top {len(sources)} by semantic similarity):\n\n"
        f"{excerpts}"
    )


def answer(query: str, **search_kwargs) -> Answer:
    sources = search(query, **search_kwargs)
    user = _build_user_prompt(query, sources)
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": RAG_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.1, "num_ctx": 8192},
            },
            timeout=180,
        )
        r.raise_for_status()
        txt = r.json()["message"]["content"]
    except Exception as e:
        txt = f"(LLM error: {e})"
    return Answer(text=txt, sources=sources, retrieved=len(sources))
