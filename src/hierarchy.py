"""
Hierarchy-aware compound shortcut logic.

The user's naming convention concatenates folder shortcuts down the path:

    F - Finance/
    └── FI - Invoices/                    (compound: FI = F + I)
        ├── FICOM - Computer Jakob/      (compound: FICOM = FI + COM)
        │   └── FICOM 2005 Mouse.pdf
        ├── FIOCS - Simon Ochsner/       (compound: FIOCS = FI + OCS)
        └── FTW18 - Withholding 2018/    (compound: FTW18 = FT + W18)

Existing folders may be in "full compound" form (FTW18 = FT+W18) OR in "added
letters only" form (COM, meant to be FI+COM=FICOM). We detect both and always
output the full compound.

Rules when creating a NEW sub-folder (Case B, for files inside messy source
folders without a shortcut prefix):

    additional letters = 1 if the anchor has ≤ 10 shortcut-prefixed children
                         3 if the anchor has > 10

Collision: if the proposed compound is already in use, bump the ADDITIONAL
letters only (e.g. FIGAR → FIGAS → FIGAT). The anchor compound is never changed.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

# A folder is "shortcut-prefixed" if its name matches:
#   one or more UPPERCASE letters, optionally followed by UPPERCASE+DIGITS,
#   then optional whitespace, a dash, optional whitespace, then the human name.
# Accepts: "F - Finance", "FTW18 - Withholding Tax", "FS- SRS Contribution".
FOLDER_TOKEN_RE = re.compile(r"^([A-Z][A-Z0-9]{0,7})\s*-\s*(.+?)\s*$")

LETTER_RE = re.compile(r"[A-Za-z]")
WORD_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*)\b")


class FolderToken(NamedTuple):
    shortcut: str     # as appears in the folder name, e.g. "COM" or "FICOM" or "FTW18"
    human: str        # the rest after " - ", e.g. "Computer Jakob"


class AnchorStep(NamedTuple):
    folder_name: str     # original source folder name, e.g. "COM - Computer Jakob"
    local_shortcut: str  # the prefix as it appears in the source, e.g. "COM"
    compound: str        # cumulative compound shortcut, e.g. "FICOM"
    human: str           # human portion, e.g. "Computer Jakob"
    upgraded_name: str   # target folder name, e.g. "FICOM - Computer Jakob"


class AnchorInfo(NamedTuple):
    """Info about a shortcut-prefixed folder and its direct children, used to
    decide (1) whether a new cluster-folder should use 1 or 3 additional
    letters, and (2) whether a proposed compound collides with a sibling."""
    anchor_compound: str              # e.g. "FI"
    children_compounds: set[str]      # compounds already in use by children, e.g. {"FICOM","FISVA"}
    children_count: int               # total number of shortcut-prefixed children
    additional_letter_mode: int       # 1 or 3 — derived from current children's style


# ---------- Path parsing ---------------------------------------------------


def parse_folder_token(name: str) -> FolderToken | None:
    """Return (shortcut, human) if the folder name is shortcut-prefixed; else None."""
    m = FOLDER_TOKEN_RE.match(name)
    return FolderToken(m.group(1), m.group(2).strip()) if m else None


def compound_chain(
    rel_path: Path | str,
    *,
    compounds_by_folder: dict[str, str] | None = None,
) -> list[AnchorStep]:
    """
    Walk a file's rel_path from root down. Build the chain of shortcut-prefixed
    directory ancestors. Stop at the first non-shortcut folder (or at the file
    itself if every ancestor is shortcut-prefixed).

    If `compounds_by_folder` is provided (keyed by folder rel-path string), it
    is the AUTHORITATIVE source of compounds — built once by build_anchor_index
    with sibling-count normalization applied. Pass it in whenever you have an
    index, so files inside the source see the same compounds the index assigns
    to their parent folders.

    If not provided, falls back to per-call derivation using:
      Rule 1 — a folder whose shortcut starts with the ROOT compound is kept
               as-is, regardless of the immediate parent's compound.
      Rule 2 — if the derived compound equals the immediate parent's compound,
               append the first letter of the child's human name.
    The standalone fallback does NOT apply the sibling-count normalization
    (it can't — it doesn't know how many siblings exist).
    """
    rel = Path(rel_path)
    parts = rel.parts[:-1]  # exclude the filename; we only walk directories
    chain: list[AnchorStep] = []

    if compounds_by_folder is not None:
        # Authoritative-lookup mode
        accumulated_parts: list[str] = []
        for part in parts:
            token = parse_folder_token(part)
            if token is None:
                break
            accumulated_parts.append(part)
            key = str(Path(*accumulated_parts))
            compound = compounds_by_folder.get(key)
            if compound is None:
                # Not in index — should not happen for a well-formed source path,
                # but tolerate by falling back to local derivation from this point.
                break
            chain.append(AnchorStep(
                folder_name=part,
                local_shortcut=token.shortcut,
                compound=compound,
                human=token.human,
                upgraded_name=f"{compound} - {token.human}",
            ))
        return chain

    # Standalone derivation
    parent_compound = ""
    root_compound: str | None = None
    for part in parts:
        token = parse_folder_token(part)
        if token is None:
            break

        if root_compound is None:
            compound = token.shortcut
            root_compound = token.shortcut
        elif token.shortcut.startswith(root_compound):
            compound = token.shortcut
        else:
            compound = parent_compound + token.shortcut

        if compound == parent_compound and parent_compound:
            extra = _first_distinguishing_letter(token.human)
            compound = parent_compound + extra

        upgraded = f"{compound} - {token.human}"
        chain.append(AnchorStep(
            folder_name=part,
            local_shortcut=token.shortcut,
            compound=compound,
            human=token.human,
            upgraded_name=upgraded,
        ))
        parent_compound = compound
    return chain


# Sibling-count threshold for the normalization rule: a parent folder with
# this many or fewer shortcut-prefixed children uses 1 ADDED letter per child;
# more siblings → 3 ADDED letters.
SMALL_SIBLING_THRESHOLD = 10


def _derive_normalized_added(existing_shortcut: str, human: str, k: int) -> str:
    """
    Produce exactly `k` uppercase letters of "added prefix" for an
    added-letters style folder, given its existing shortcut and human name.

    k == 1: take the first letter of the existing shortcut.
    k == 3: take the first 3 letters of the existing shortcut if it has them;
            otherwise pad from significant-word initials in the human name;
            then from consecutive characters of the existing shortcut; then 'X'.

    Examples (all with k=3):
        "ACC", "Account Statement"      -> "ACC"      # already 3
        "A",   "Account Statement"      -> "AAS"      # A + Account, Statement
        "GW",  "Garden Work Invoices"   -> "GWI"      # GW + Invoices
        "X",   ""                        -> "XXX"      # all padding
    """
    if k <= 0:
        return ""
    existing = "".join(c for c in existing_shortcut.upper() if c.isalnum())
    if k == 1:
        if existing:
            return existing[0]
        h = human.strip()
        return h[0].upper() if h else "X"
    if len(existing) >= k:
        return existing[:k]
    letters: list[str] = list(existing)
    skip = {"and", "of", "the", "for", "a", "an", "to", "in", "on", "by"}
    words = [w for w in WORD_RE.findall(human) if w.lower() not in skip]
    # Pad from significant-word initials, skipping duplicates of letters we have
    for w in words:
        if len(letters) >= k:
            break
        c = w[0].upper()
        if c not in letters:
            letters.append(c)
    # Still short? Take more characters from the original shortcut
    i = 1
    while len(letters) < k and i < len(existing):
        if existing[i] not in letters:
            letters.append(existing[i])
        i += 1
    # Last resort: pad with 'X'
    while len(letters) < k:
        letters.append("X")
    return "".join(letters[:k])


def _first_distinguishing_letter(human: str) -> str:
    """
    Return a single uppercase letter derived from the folder's human name —
    used to distinguish a child folder whose shortcut collides with its
    immediate parent's compound.

    Algorithm: first letter of the first significant word (skipping short
    function words like 'and', 'of', 'the'). Falls back to the first letter
    in the name, or 'X' as last resort.
    """
    skip = {"and", "of", "the", "for", "a", "an", "to", "in", "on", "by"}
    words = [w for w in WORD_RE.findall(human) if w.lower() not in skip]
    if words:
        return words[0][0].upper()
    letters = LETTER_RE.findall(human.upper())
    return letters[0] if letters else "X"


def fully_anchored(rel_path: Path | str) -> bool:
    """True if every directory ancestor of the file is shortcut-prefixed."""
    rel = Path(rel_path)
    dir_parts = rel.parts[:-1]
    chain = compound_chain(rel)
    return len(chain) == len(dir_parts) and len(chain) > 0


# ---------- Target path resolution -----------------------------------------


def target_parent_path(chain: list[AnchorStep]) -> Path:
    """
    Given a fully-anchored chain, return the target parent rel_path using each
    folder's UPGRADED (compound) name.

    Example:
        chain = [("F - Finance", "F", "F", "Finance", "F - Finance"),
                 ("FI - Invoices", "FI", "FI", "Invoices", "FI - Invoices"),
                 ("COM - Computer Jakob", "COM", "FICOM", "Computer Jakob", "FICOM - Computer Jakob")]
        → Path("F - Finance/FI - Invoices/FICOM - Computer Jakob")
    """
    return Path(*[step.upgraded_name for step in chain])


# ---------- Preflight anchor index -----------------------------------------


def build_anchor_index(source_root: Path) -> tuple[dict[str, AnchorInfo], dict[str, str]]:
    """
    One pass over the source tree. Returns a 2-tuple:
      1. anchor_info_by_compound — dict[compound, AnchorInfo] used by Case B
         (creating new sub-folders for messy parents).
      2. compounds_by_folder — dict[folder rel_path string, compound]; the
         AUTHORITATIVE compound for every shortcut-prefixed folder in the
         source. Pass this to compound_chain() so file paths see the same
         (normalized) compounds as the index does.

    Rules applied per child folder:
      Rule 1 (root-compound preserve): if the child's existing shortcut
        starts with the root compound, keep it as-is (full-compound style;
        do not normalise length).
      Normalization: otherwise (added-letters style), enforce exactly k
        added letters where k = 1 if the parent has ≤ SMALL_SIBLING_THRESHOLD
        shortcut-prefixed children, or k = 3 if more.
      Rule 2 (same-as-parent): if the result equals the parent's compound,
        append the first letter of the child's human name.
      Rule 3 (sibling bump): if the result collides with a sibling already
        assigned, bump the last alphabetic character until unique.
    """
    children_by_compound: dict[str, list[AnchorStep]] = {"": []}
    compounds_by_folder: dict[str, str] = {}

    def _resolve_child(
        parent_compound: str,
        root_compound: str,
        tok: FolderToken,
        k_for_added: int,
        already_assigned: set[str],
    ) -> str:
        # Rule 1: starts with root → keep as-is, no normalisation.
        if tok.shortcut.startswith(root_compound):
            compound = tok.shortcut
        else:
            # Added-letters: enforce exactly k_for_added.
            added = _derive_normalized_added(tok.shortcut, tok.human, k_for_added)
            compound = parent_compound + added

            # Smart collision (k=1 only): if the chosen letter is taken, try
            # OTHER letters from the existing shortcut before falling back to
            # naive alphabet bumping. Preserves "the letter means something".
            #   AXA wanted FIA (taken by ASI) → try X → FIX
            #   helvetia wanted FIH (taken by Helsana) → try E or L → FIE / FIL
            if k_for_added == 1 and compound in already_assigned:
                tried = {added[0]}
                for c in tok.shortcut.upper():
                    if c.isalpha() and c not in tried:
                        cand = parent_compound + c
                        tried.add(c)
                        if cand not in already_assigned:
                            compound = cand
                            break
                # Then try first letters of significant words in the human name
                if compound in already_assigned:
                    skip = {"and", "of", "the", "for", "a", "an", "to", "in", "on", "by"}
                    for w in WORD_RE.findall(tok.human):
                        if w.lower() in skip:
                            continue
                        c = w[0].upper()
                        if c not in tried:
                            cand = parent_compound + c
                            tried.add(c)
                            if cand not in already_assigned:
                                compound = cand
                                break

        # Rule 2: same-as-parent.
        if compound == parent_compound and parent_compound:
            compound = parent_compound + _first_distinguishing_letter(tok.human)

        # Rule 3: final fallback — naive last-alpha bump until unique.
        while compound in already_assigned:
            compound = _bump_last_alpha(compound)
        return compound

    def _walk(folder: Path, rel_so_far: Path, parent_compound: str, root_compound: str) -> None:
        raw: list[tuple[Path, FolderToken]] = []
        for c in sorted(folder.iterdir(), key=lambda p: p.name):
            if not c.is_dir():
                continue
            t = parse_folder_token(c.name)
            if t is None:
                continue
            raw.append((c, t))
        n_siblings = len(raw)
        k_for_added = 1 if n_siblings <= SMALL_SIBLING_THRESHOLD else 3
        assigned: set[str] = set()
        for child, tok in raw:
            child_compound = _resolve_child(parent_compound, root_compound, tok, k_for_added, assigned)
            assigned.add(child_compound)
            child_rel = rel_so_far / child.name
            compounds_by_folder[str(child_rel)] = child_compound
            step = AnchorStep(
                child.name, tok.shortcut, child_compound, tok.human,
                f"{child_compound} - {tok.human}",
            )
            children_by_compound.setdefault(parent_compound, []).append(step)
            _walk(child, child_rel, child_compound, root_compound)

    # Top-level: each shortcut-prefixed folder here IS the root of its own subtree.
    for top in sorted(source_root.iterdir(), key=lambda p: p.name):
        if not top.is_dir():
            continue
        tok = parse_folder_token(top.name)
        if not tok:
            continue
        compounds_by_folder[top.name] = tok.shortcut
        top_step = AnchorStep(top.name, tok.shortcut, tok.shortcut, tok.human,
                              f"{tok.shortcut} - {tok.human}")
        children_by_compound[""].append(top_step)
        _walk(top, Path(top.name), tok.shortcut, tok.shortcut)

    # Build AnchorInfo per compound. additional_letter_mode now follows the
    # same SMALL_SIBLING_THRESHOLD rule as the normalization above, so Case B
    # (creating new folders) is consistent with the normalisation of existing
    # ones.
    index: dict[str, AnchorInfo] = {}
    for compound, kids in children_by_compound.items():
        kid_compounds = {k.compound for k in kids}
        n = len(kids)
        mode = 3 if n > SMALL_SIBLING_THRESHOLD else 1
        index[compound] = AnchorInfo(
            anchor_compound=compound,
            children_compounds=kid_compounds,
            children_count=n,
            additional_letter_mode=mode,
        )
    return index, compounds_by_folder


# ---------- Generating new compound shortcuts for Case B -------------------


def _derive_letters(proposal: str, k: int) -> str:
    """
    Pick k uppercase letters from a folder-name proposal.

    k=1: first letter of first significant word.
    k>=2: prefer first letters of first k significant words; if fewer words,
          pad from the first word's remaining letters.
    """
    words = [w for w in WORD_RE.findall(proposal) if not w.lower() in {"and", "of", "the", "for", "a"}]
    if not words:
        letters = LETTER_RE.findall(proposal.upper())
        return "".join(letters[:k]) or "X"
    if k == 1:
        return words[0][0].upper()
    # k >= 2
    if len(words) >= k:
        return "".join(w[0].upper() for w in words[:k])
    # Fewer words than k — take first letters of available words, then pad from first word
    taken = [w[0].upper() for w in words]
    first = words[0].upper()
    i = 1
    while len(taken) < k:
        if i < len(first):
            taken.append(first[i])
            i += 1
        else:
            taken.append("X")
    return "".join(taken[:k])


def _bump_letters(letters: str) -> str:
    """Lexicographic bump within A-Z, then extend length if we wrap."""
    chars = list(letters)
    i = len(chars) - 1
    while i >= 0:
        c = chars[i]
        if c.isalpha() and c < "Z":
            chars[i] = chr(ord(c) + 1)
            return "".join(chars)
        chars[i] = "A"
        i -= 1
    return "A" + "".join(chars)  # extended length


def _bump_last_alpha(compound: str) -> str:
    """
    Bump only the last alphabetic character of a compound (leaves any trailing
    digits alone). Used for sibling-collision resolution — e.g. if both
    'FT21DL' and 'FT21DL' are proposed at the same level, the second becomes
    'FT21DM', then 'FT21DN', etc.
    """
    chars = list(compound)
    for i in range(len(chars) - 1, -1, -1):
        if chars[i].isalpha():
            if chars[i] < "Z":
                chars[i] = chr(ord(chars[i]) + 1)
                return "".join(chars)
            # Wrap this position to 'A' and continue bumping earlier positions
            chars[i] = "A"
            continue
    # All letters wrapped → extend compound length
    return compound + "A"


def new_compound(anchor: AnchorInfo, proposal: str) -> tuple[str, str]:
    """
    For Case B — create a new compound shortcut under `anchor` that doesn't
    collide with existing children.

    Returns (full_compound, added_letters). E.g. ("FIGWI", "GWI").
    """
    k = anchor.additional_letter_mode
    letters = _derive_letters(proposal, k)
    candidate = anchor.anchor_compound + letters
    # Cap: don't let compound exceed 8 chars (per naming convention)
    max_compound_len = 8
    # If even the base is too long, truncate the derived letters
    if len(candidate) > max_compound_len:
        over = len(candidate) - max_compound_len
        letters = letters[:max(1, len(letters) - over)]
        candidate = anchor.anchor_compound + letters
    # Bump on collision
    guard = 0
    while candidate in anchor.children_compounds and guard < 676:
        letters = _bump_letters(letters)
        candidate = anchor.anchor_compound + letters
        guard += 1
    return candidate, letters
