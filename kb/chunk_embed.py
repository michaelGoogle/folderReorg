"""
Chunk a document's text into ~500-token chunks and embed each chunk with
bge-m3 on the 3090. Chunks overlap by ~50 tokens (char-approximated) to
avoid cutting across sentence boundaries.
"""

from __future__ import annotations

import re
from functools import lru_cache

import numpy as np

from kb.config import (
    CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    EMBED_BATCH_SIZE,
    EMBED_MODEL,
)


# --- Chunking --------------------------------------------------------------


_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜÉÈÀ])|\n{2,}")


def split_text(text: str,
               chunk_chars: int = CHUNK_CHARS,
               overlap_chars: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """
    Split `text` into chunks of ≤ chunk_chars each, with overlap_chars
    overlap between consecutive chunks. Prefers sentence boundaries near
    chunk_chars; falls back to hard split if no boundary is close.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            # Try to roll back to a sentence/paragraph boundary within
            # the last 20% of the chunk window.
            window = text[end - chunk_chars // 5: end]
            last = -1
            for m in _SENT_BOUNDARY.finditer(window):
                last = m.end()
            if last > 0:
                end = end - chunk_chars // 5 + last
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


# --- Embedding -------------------------------------------------------------


@lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL, device="cuda")


def embed(texts: list[str], *, batch_size: int = EMBED_BATCH_SIZE) -> np.ndarray:
    """
    Returns a [N, 1024] float32 numpy array of normalised bge-m3 embeddings.
    """
    if not texts:
        return np.zeros((0, 1024), dtype=np.float32)
    model = _embedder()
    vecs = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    return vecs.astype(np.float32)
