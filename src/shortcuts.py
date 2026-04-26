"""
Shortcut collision resolver. Per naming convention: 1-4 uppercase letters,
unique within a given parent folder level.

When the LLM proposes a shortcut that collides with a sibling, bump it:
    "FB" (taken) → "FC" → "FD" ... until free.
If single-letter shortcuts run out, extend length (FB → FBA, FBB, …).
"""

from __future__ import annotations

from src.naming import normalise_shortcut


def _next_shortcut(s: str) -> str:
    """Lexicographic bump within A-Z, then extend length."""
    letters = list(s)
    i = len(letters) - 1
    while i >= 0:
        if letters[i] < "Z":
            letters[i] = chr(ord(letters[i]) + 1)
            return "".join(letters)
        letters[i] = "A"
        i -= 1
    return "A" + "".join(letters)  # extend length


def resolve(proposed: str, taken: set[str], *, max_len: int = 4) -> str:
    """Return a shortcut that's in [A-Z]{1..max_len} and not in `taken`."""
    sc = normalise_shortcut(proposed)
    if sc and sc not in taken and len(sc) <= max_len:
        return sc
    # Bump until free
    while True:
        sc = _next_shortcut(sc or "A")
        if sc not in taken and len(sc) <= max_len:
            return sc
        if len(sc) > max_len:
            # Give up after max_len; return something unique-ish via counter
            n = 0
            while True:
                n += 1
                cand = f"X{n:03d}"[:max_len]
                if cand not in taken:
                    return cand
