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


def _release_gpu_cache() -> None:
    """
    Force PyTorch's CUDA caching allocator to release unused buffers back
    to the GPU driver after each embed() call. Without this, activation
    memory from past batches accumulates inside PyTorch's private cache —
    "free" from PyTorch's perspective but invisible to other processes
    and to nvidia-smi. Over a long-running indexer scan with variable
    chunk counts (different files → different tensor shapes), the cache
    fragments and grows toward the GPU's physical capacity, eventually
    causing 30-60 second stalls per batch as PyTorch fights for its own
    memory back.

    Cost: ~5-20 ms per call (GPU sync). Negligible for files that
    embed in tens of milliseconds, completely irrelevant for files
    that take seconds. Set KB_EMBED_RELEASE_CACHE=0 to disable
    (e.g. for benchmarking or single-shot embed calls).
    """
    try:
        import os, torch
        if os.environ.get("KB_EMBED_RELEASE_CACHE", "1") in ("0", "false", "no"):
            return
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        # Best-effort — never let a cache-release failure break embedding.
        pass


def embed(texts: list[str], *, batch_size: int = EMBED_BATCH_SIZE) -> np.ndarray:
    """
    Returns a [N, 1024] float32 numpy array of normalised bge-m3 embeddings.
    Calls torch.cuda.empty_cache() after each invocation so GPU memory
    stays bounded across long indexer runs (see _release_gpu_cache).
    """
    if not texts:
        return np.zeros((0, 1024), dtype=np.float32)
    model = _embedder()
    try:
        vecs = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return vecs.astype(np.float32)
    finally:
        _release_gpu_cache()
