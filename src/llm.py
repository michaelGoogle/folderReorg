"""
Thin wrapper around the Ollama HTTP API (plan §7.3) with retry + json extraction.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from src.config import LLM_MODEL, OLLAMA_URL


class LLMError(RuntimeError):
    pass


def chat(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    num_ctx: int = 8192,
    timeout: int = 300,
    max_retries: int = 3,
) -> str:
    """Send a single chat turn, return the assistant content."""
    model = model or LLM_MODEL
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as e:
            last_err = e
            time.sleep(2 ** attempt)  # 1, 2, 4 s exponential backoff
    raise LLMError(f"Ollama chat failed after {max_retries} attempts: {last_err}")


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_INLINE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def extract_json(raw: str) -> dict[str, Any] | None:
    """
    Pull a JSON object from arbitrary LLM prose.
    Handles fenced ```json blocks and inline {…} objects.
    Returns None if no parseable object found.
    """
    if not raw:
        return None
    # Fenced first
    m = _JSON_FENCE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Inline — try the outermost {...} match
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        try:
            return json.loads(m2.group(0))
        except json.JSONDecodeError:
            # Sometimes models produce multiple objects; try each non-nested one
            for mm in _JSON_INLINE.finditer(raw):
                try:
                    return json.loads(mm.group(0))
                except json.JSONDecodeError:
                    continue
    return None


def embed(texts: list[str], *, model: str = "bge-m3") -> list[list[float]]:
    """
    Use Ollama's /api/embeddings endpoint. Kept here as a reference path —
    the main pipeline embeds via sentence-transformers for batch throughput
    (§6.2), but this is useful for ad-hoc calls.
    """
    out: list[list[float]] = []
    for t in texts:
        r = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": model, "prompt": t},
            timeout=60,
        )
        r.raise_for_status()
        out.append(r.json()["embedding"])
    return out
