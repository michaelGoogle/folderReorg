"""
Naming helpers — RULE 8 cleanups, shortcut normalisation, token extraction,
final name assembly. Per plan §7.6 + File Naming Convention Manual.
"""

from __future__ import annotations

import re
from pathlib import Path

# Convention regex (§10.2, extended to accept (image) and (password) markers,
# and alphanumeric shortcuts like FT18, FTW18, FT17DU per the user's hierarchy).
CONVENTION_PATTERN = re.compile(
    r"^(?P<shortcut>[A-Z][A-Z0-9]{0,7}) "
    r"(?P<yymm>\d{4}) "
    r"(?P<desc>.+?) "
    r"V(?P<major>\d+)-(?P<minor>\d{2})"
    r"(?:\s+(?P<status>signed|approved|final))?"
    r"(?:\s+\((?P<marker>image|password)\))*"
    r"(?P<ext>\.[A-Za-z0-9]+)$"
)


def safe_name(name: str) -> str:
    """
    Apply RULE 8 cleanups:
    - replace underscores with spaces
    - strip numeric suffixes in parens: "(1)", "(2)", ...
    - strip year-in-parens: "(2023)"
    - drop commas
    - collapse whitespace
    """
    if not name:
        return ""
    name = name.replace("_", " ")
    name = re.sub(r"\s*\(\d+\)\s*", " ", name)
    name = re.sub(r"\s*\(\d{4}\)\s*", " ", name)
    name = re.sub(r",", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def normalise_shortcut(raw: str, fallback: str = "X") -> str:
    """
    Force the LLM's shortcut output into spec: 1-4 uppercase letters, A-Z only.

    The 14B model sometimes produces things like "BKS_Uberblick_UBS_NOV24" or
    "FB-Bank"; we strip everything non-alpha, uppercase, truncate to 4 chars.
    """
    if not raw:
        return fallback
    letters = re.sub(r"[^A-Za-z]", "", raw).upper()
    if not letters:
        return fallback
    return letters[:4]


def extract_meaningful_token(stem: str) -> str | None:
    """
    Pull a useful token out of a junk-looking filename.
      'IMG_1234'          -> '1234'
      'Scan 20240315_001' -> '20240315 001'
      'DSC09847'          -> '09847'
      'random_jibberish'  -> None (too wordy, likely garbage)
    """
    s = re.sub(r"^(IMG|DSC|MVI|VID|SCAN|SCN|DOC|FILE)[-_ ]*", "", stem, flags=re.I)
    s = s.replace("_", " ").strip()
    if re.fullmatch(r"[\d ]+", s):
        return re.sub(r"\s+", " ", s).strip()
    m = re.search(r"\d{4,}", s)
    return m.group(0) if m else None


def already_conforms(filename: str) -> bool:
    """True if the file already matches the naming convention → skip LLM (§11.6)."""
    return CONVENTION_PATTERN.match(filename) is not None


def assemble_new_name(
    shortcut: str,
    yymm: str,
    descriptive: str,
    version: str,
    ext: str,
    status_suffix: str | None = None,
    image_tag: bool = False,
    password_tag: bool = False,
) -> str:
    """
    Final filename per RULE 2:
        <SHORTCUT> <YYMM> <Descriptive Name> <Vx-yy>
          [ signed|approved|final][ (image)][ (password)].<ext>

    `password_tag` is applied to encrypted PDFs we could not extract text from
    — the file is still renamed and filed, but the marker flags that it needs
    manual decryption before the content can be read.
    """
    parts = [shortcut, yymm, safe_name(descriptive), version]
    name = " ".join(p for p in parts if p)
    if status_suffix in {"signed", "approved", "final"}:
        name = f"{name} {status_suffix}"
    if image_tag:
        name = f"{name} (image)"
    if password_tag:
        name = f"{name} (password)"
    if not ext.startswith("."):
        ext = "." + ext
    return name + ext
