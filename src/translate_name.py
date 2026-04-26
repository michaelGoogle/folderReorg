"""
Translate an existing folder/file name fragment to English via the local LLM
(plan §12.6). Cached to avoid re-translating the same fragment repeatedly
during the per-file phase 3 pass.
"""

from __future__ import annotations

from functools import lru_cache

from src.llm import chat

TRANSLATE_SYS = """Translate to English. Keep proper nouns, brand names, organisation
names, product names, and people's names in their ORIGINAL form. Translate common nouns
and descriptive words. Preserve numbers and dates exactly.

Output ONLY the translated string — no prose, no quotation marks, no prefixes.
If the input is already English, echo it back unchanged."""


@lru_cache(maxsize=20_000)
def to_english(name: str, lang_hint: str | None = None) -> str:
    """Return the English form. Safe to call for already-English strings."""
    if not name:
        return name
    if lang_hint in (None, "en", "und", "EN", "UND"):
        return name
    user = name
    if lang_hint:
        user += f"\n\n(source language: {lang_hint})"
    try:
        out = chat(TRANSLATE_SYS, user, temperature=0.0).strip()
    except Exception:
        return name
    # Take first line only, strip quotes
    out = out.splitlines()[0].strip().strip('"').strip("'")
    return out or name
