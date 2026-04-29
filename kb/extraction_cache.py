"""
Persistent skip-cache for files where text extraction reliably fails.

When the indexer encounters a file that returns a non-"ok" extraction
status (password-protected, corrupt, unsupported, too-large, empty), it
records the file's sha256 + status in this cache. On subsequent scans,
files whose sha matches a cached failure are routed straight to the
synthetic-context indexing path — no `extract()` call, no MuPDF noise,
no wasted CPU.

Why sha-keyed (not path-keyed): the existing Qdrant-side mtime+size and
sha fast paths already cover the per-path case ("same file, same place,
unchanged"). The cache covers two gaps the Qdrant fast path can't:

  1. Qdrant data wiped (e.g. `docker compose down -v`, manual `kb.py
     remove`). Every file looks "new"; without the cache, every
     password PDF gets re-extracted.

  2. File rename / move / copy. Same content (sha) at a different
     rel_path → Qdrant lookup fails. The cache hits, extraction is
     skipped.

File: kb/data/<variant>/extraction_cache.json

Format:
  {
    "version": 1,
    "entries": {
      "<sha256>": {
        "status":      "password" | "corrupt" | "unsupported" | "too_large" | "empty" | "no_chunks",
        "first_seen":  "ISO-8601 timestamp",
        "last_seen":   "ISO-8601 timestamp",
        "filename":    "<basename of the file the last time we saw it>"
      },
      ...
    }
  }

Tiny on disk (~100 bytes per entry); bounded for safety at 100,000 entries.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

# Bump this when a code change in extract.py would invalidate cache
# entries (e.g. adding antiword for .doc means previously-"unsupported"
# entries should be retried). The whole cache is dropped on mismatch.
SCHEMA_VERSION = 1

# These extraction failures are deterministic for a given content (sha256):
# password-protected stays password-protected, corrupt stays corrupt, etc.
# Cache hits for these statuses skip the `extract()` call entirely.
#
# Excluded from caching:
#   · "ok"         — successful extractions are already in Qdrant; no point
#                    caching here. Including them would just bloat the file.
#   · "unreadable" — could be transient (permission flip, NAS hiccup).
#                    Always retry these.
PERSISTENT_FAILURE_STATUSES: frozenset[str] = frozenset({
    "password", "corrupt", "unsupported", "too_large", "empty", "no_chunks",
})

# Hard cap to prevent runaway growth on weird datasets. ~100K * 100 B = ~10 MB.
MAX_ENTRIES = 100_000

CACHE_FILENAME = "extraction_cache.json"


def _path(variant_data_dir: Path) -> Path:
    return variant_data_dir / CACHE_FILENAME


def load(variant_data_dir: Path) -> dict:
    """
    Load the cache for one variant. Returns a fresh empty dict when the
    file doesn't exist, can't be parsed, or has a mismatched version
    (treated as a forced reset).
    """
    p = _path(variant_data_dir)
    if not p.exists():
        return {"version": SCHEMA_VERSION, "entries": {}}
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": SCHEMA_VERSION, "entries": {}}
    if d.get("version") != SCHEMA_VERSION:
        # Schema bumped — drop the cache so the next scan repopulates with
        # the new logic.
        return {"version": SCHEMA_VERSION, "entries": {}}
    if not isinstance(d.get("entries"), dict):
        d["entries"] = {}
    return d


def save(cache: dict, variant_data_dir: Path) -> None:
    """
    Atomically write the cache to disk. Tempfile + rename so a Ctrl-C
    mid-write doesn't leave a half-written file that breaks the next load.
    """
    variant_data_dir.mkdir(parents=True, exist_ok=True)
    target = _path(variant_data_dir)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".extraction_cache.", suffix=".tmp",
        dir=str(variant_data_dir),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, target)
    except Exception:
        # Best-effort cleanup on failure
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def lookup(cache: dict, sha: str) -> dict | None:
    """Return the cache entry for `sha` if present, else None."""
    if not sha:
        return None
    return cache.get("entries", {}).get(sha)


def is_known_failure(cache: dict, sha: str) -> tuple[bool, str | None]:
    """
    True if `sha` is recorded as a persistent extraction failure.
    Returns (hit, status). `status` is one of PERSISTENT_FAILURE_STATUSES
    when hit=True, else None.
    """
    entry = lookup(cache, sha)
    if not entry:
        return False, None
    status = entry.get("status")
    if status in PERSISTENT_FAILURE_STATUSES:
        return True, status
    return False, None


def record_failure(cache: dict, sha: str, status: str, filename: str) -> None:
    """
    Record a persistent extraction failure. No-op for statuses outside
    PERSISTENT_FAILURE_STATUSES. Updates last_seen on every hit so we
    can age out very old entries later if desired.
    """
    if not sha or status not in PERSISTENT_FAILURE_STATUSES:
        return
    entries: dict = cache.setdefault("entries", {})
    if len(entries) >= MAX_ENTRIES and sha not in entries:
        # Cap exceeded — silently drop new additions rather than evicting,
        # since extractions just fall through to a real `extract()` call
        # (no functional regression, just no skip-cache benefit).
        return
    now = datetime.now().isoformat(timespec="seconds")
    existing = entries.get(sha)
    if existing:
        existing["status"] = status
        existing["last_seen"] = now
        existing["filename"] = filename or existing.get("filename", "")
    else:
        entries[sha] = {
            "status":     status,
            "first_seen": now,
            "last_seen":  now,
            "filename":   filename or "",
        }


def forget(cache: dict, sha: str) -> bool:
    """Remove a sha from the cache. Returns True if it was present."""
    return cache.get("entries", {}).pop(sha, None) is not None


def stats(cache: dict) -> dict:
    """Aggregate counters for status reporting."""
    from collections import Counter
    by_status: Counter[str] = Counter()
    for entry in cache.get("entries", {}).values():
        by_status[entry.get("status", "?")] += 1
    return {
        "total":     sum(by_status.values()),
        "by_status": dict(by_status),
    }
