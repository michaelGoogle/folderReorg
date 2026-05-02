"""
Indexer — delta scan of one or more roots into the Qdrant collection.

Per-file flow:
  1. sha256(file) → content-addressed key.
  2. Look up in Qdrant; if sha256 matches, skip.
  3. Otherwise delete any existing chunks for this (root, rel_path), extract,
     chunk, embed, upsert.

Per-root flow after the per-file pass:
  · List all file_ids currently indexed for this root.
  · Diff against the set of files actually seen on disk.
  · Delete chunks for files that disappeared.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import time
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from kb.chunk_embed import embed, split_text
from kb.config import (
    EMBED_DIM,
    QDRANT_COLLECTION,
    QDRANT_URL,
)
from kb.extract import extract

# Stable namespace for deterministic point UUIDs (generated once, hardcoded).
_UUID_NS = uuid.UUID("d94f0f11-7b25-4fc9-9c4c-0d39a8b4c12f")


# --- Scan config (loaded from scan_config.toml at repo root) -------------
# All four operational knobs (extension deny, max file size, per-file
# timeout, auto-deny threshold) live in a TOML file you can edit without
# touching code. Env vars still override per-run.
#
# Hierarchy (highest priority wins):
#   1. Env var (KB_EXTENSION_DENY, KB_MAX_FILE_SIZE_MB, …)
#   2. scan_config.toml entry
#   3. Hard-coded fallback (only used when the file is missing or the key
#      isn't present — kept minimal and safety-oriented)
#
# Override the file's location with KB_SCAN_CONFIG=/path/to/some.toml.
_SCAN_CONFIG_PATH = Path(os.environ.get(
    "KB_SCAN_CONFIG",
    str(Path(__file__).resolve().parent.parent / "scan_config.toml"),
))

# Hard-coded fallback values — used ONLY when scan_config.toml is missing
# or doesn't contain a given key. Keep these conservative; the canonical
# defaults live in scan_config.toml.
_FALLBACK_EXTENSION_DENY = (
    ".xlsm",
    ".mp4", ".avi", ".mov", ".mkv", ".webm",
    ".png", ".jpg", ".jpeg",
)
_FALLBACK_MAX_FILE_SIZE_MB = 10.0
_FALLBACK_FILE_TIMEOUT_SECONDS = 180
_FALLBACK_AUTO_DENY_THRESHOLD = 0

if _SCAN_CONFIG_PATH.is_file():
    try:
        with open(_SCAN_CONFIG_PATH, "rb") as _f:
            _cfg = tomllib.load(_f)
    except Exception as _e:
        print(f"  ⚠ failed to parse {_SCAN_CONFIG_PATH}: {_e} — using fallbacks")
        _cfg = {}
else:
    print(f"  ⚠ {_SCAN_CONFIG_PATH} not found — using fallback defaults")
    _cfg = {}

_file_ext_deny = tuple(_cfg.get("extension_deny", _FALLBACK_EXTENSION_DENY))
_file_max_size_mb = float(_cfg.get("max_file_size_mb", _FALLBACK_MAX_FILE_SIZE_MB))
_file_timeout_s = int(_cfg.get("file_timeout_seconds", _FALLBACK_FILE_TIMEOUT_SECONDS))
_file_auto_deny = int(_cfg.get("auto_deny_threshold", _FALLBACK_AUTO_DENY_THRESHOLD))

# Normalise extension entries: lower-case, prepend "." if missing.
def _norm_ext(e: str) -> str:
    e = e.strip().lower()
    return e if e.startswith(".") else "." + e


# Extension deny — env var REPLACES file's list entirely.
_env_deny = os.environ.get("KB_EXTENSION_DENY")
if _env_deny is None:
    EXTENSION_DENY: frozenset[str] = frozenset(
        _norm_ext(e) for e in _file_ext_deny if e and e.strip()
    )
elif _env_deny.strip() == "":
    EXTENSION_DENY = frozenset()
else:
    EXTENSION_DENY = frozenset(
        _norm_ext(x) for x in _env_deny.split(",") if x.strip()
    )

# Numeric knobs — env var overrides file value.
MAX_FILE_SIZE_MB = float(
    os.environ.get("KB_MAX_FILE_SIZE_MB", str(_file_max_size_mb))
)
MAX_FILE_SIZE_BYTES = int(MAX_FILE_SIZE_MB * 1024 * 1024) if MAX_FILE_SIZE_MB > 0 else 0

FILE_TIMEOUT_SECONDS = int(
    os.environ.get("KB_FILE_TIMEOUT_SECONDS", str(_file_timeout_s))
)
AUTO_DENY_THRESHOLD = int(
    os.environ.get("KB_AUTO_DENY_THRESHOLD", str(_file_auto_deny))
)
# Legacy env var — only honored when neither KB_AUTO_DENY_THRESHOLD nor
# the TOML's auto_deny_threshold key is "intentionally" set. Kept for
# back-compat with anyone who set KB_AUTO_DENY_ON_TIMEOUT=0 in their env.
if ("KB_AUTO_DENY_THRESHOLD" not in os.environ
        and (os.environ.get("KB_AUTO_DENY_ON_TIMEOUT", "").strip().lower()
             in ("0", "false", "no", "off"))):
    AUTO_DENY_THRESHOLD = 0  # legacy disable still wins

_HAS_SIGALRM = hasattr(signal, "SIGALRM")  # Linux/macOS only — not Windows


class _FileProcessingTimeout(Exception):
    """Raised by SIGALRM handler when a file exceeds FILE_TIMEOUT_SECONDS."""
    pass


def _alarm_handler(signum, frame):  # pragma: no cover (signal-driven)
    raise _FileProcessingTimeout()

# File-naming convention regex (same pattern as src/naming.py)
_CONVENTION_RE = re.compile(
    r"^(?P<shortcut>[A-Z][A-Z0-9]{0,7}) "
    r"(?P<yymm>\d{4}) "
    r"(?P<desc>.+?) "
    r"V(?P<major>\d+)-(?P<minor>\d{2})"
    r"(?:\s+(?P<status>signed|approved|final))?"
    r"(?:\s+\((?P<marker>image|password)\))*"
    r"(?P<ext>\.[A-Za-z0-9]+)$"
)


# --- helpers ---------------------------------------------------------------


def sha256_file(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def parse_convention(name: str) -> dict:
    """Pull (shortcut, yymm, desc, version, marker) from a conforming filename."""
    m = _CONVENTION_RE.match(name)
    if not m:
        return {}
    return {
        "shortcut": m.group("shortcut"),
        "yymm": m.group("yymm"),
        "descriptive": m.group("desc"),
        "version": f"V{m.group('major')}-{m.group('minor')}",
        "status_marker": m.group("status") or m.group("marker"),
    }


def detect_language(text: str) -> tuple[str, float]:
    """Lingua-based language detection (same as phase1_lang_detect.py)."""
    text = text.replace("\n", " ").strip()[:2000]
    if len(text) < 20:
        return ("und", 0.0)
    try:
        from lingua import Language, LanguageDetectorBuilder
        langs = [Language.ENGLISH, Language.GERMAN, Language.FRENCH,
                 Language.ITALIAN, Language.SPANISH, Language.DUTCH, Language.PORTUGUESE]
        detector = LanguageDetectorBuilder.from_languages(*langs).with_preloaded_language_models().build()
        confs = detector.compute_language_confidence_values(text)
        if not confs:
            return ("und", 0.0)
        iso = {"ENGLISH": "en", "GERMAN": "de", "FRENCH": "fr", "ITALIAN": "it",
               "SPANISH": "es", "DUTCH": "nl", "PORTUGUESE": "pt"}
        top = confs[0]
        return (iso.get(top.language.name, "und"), float(top.value))
    except Exception:
        return ("und", 0.0)


# --- Qdrant ----------------------------------------------------------------


def _qdrant():
    from qdrant_client import QdrantClient
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(client=None) -> None:
    from qdrant_client.http import models as qm
    client = client or _qdrant()
    names = [c.name for c in client.get_collections().collections]
    if QDRANT_COLLECTION not in names:
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=qm.VectorParams(size=EMBED_DIM, distance=qm.Distance.COSINE),
        )
        # Payload indices speed up filtered queries
        for field, schema in [
            ("root", qm.PayloadSchemaType.KEYWORD),
            ("rel_path", qm.PayloadSchemaType.KEYWORD),
            ("sha256", qm.PayloadSchemaType.KEYWORD),
            ("language", qm.PayloadSchemaType.KEYWORD),
            ("yymm", qm.PayloadSchemaType.KEYWORD),
            ("compound", qm.PayloadSchemaType.KEYWORD),
            ("file_id", qm.PayloadSchemaType.KEYWORD),
            # New: discriminate filename-only matches from real-content
            # matches in chat. Lets users filter to "extracted" only or
            # "synthetic" only via a query-time payload filter.
            ("text_source", qm.PayloadSchemaType.KEYWORD),
            ("extraction_status", qm.PayloadSchemaType.KEYWORD),
        ]:
            try:
                client.create_payload_index(
                    collection_name=QDRANT_COLLECTION,
                    field_name=field, field_schema=schema,
                )
            except Exception:
                pass


def _existing_files_for_root(client, root_name: str) -> dict[str, dict]:
    """
    Return {rel_path → {sha256, mtime, size_bytes}} currently indexed under
    this root. Metadata is read from each file's chunk-0 payload (authoritative).

    Used by the fast-path in index_file(): if current stat matches the stored
    mtime+size, skip sha256 entirely and treat the file as unchanged. This
    turns a "no-op reindex" over the SSHFS-mounted NAS from ~5 min per GB
    (hashing everything) into ~1 s per 1000 files (just stat).
    """
    from qdrant_client.http import models as qm
    out: dict[str, dict] = {}
    next_page = None
    flt = qm.Filter(must=[qm.FieldCondition(key="root",
                                            match=qm.MatchValue(value=root_name))])
    while True:
        resp, next_page = client.scroll(
            collection_name=QDRANT_COLLECTION,
            scroll_filter=flt,
            with_payload=["rel_path", "sha256", "mtime", "size_bytes", "chunk_id"],
            with_vectors=False,
            limit=512,
            offset=next_page,
        )
        for p in resp:
            payload = p.payload or {}
            if int(payload.get("chunk_id", 0)) != 0:
                continue  # authoritative metadata is on chunk 0 only
            rp = payload.get("rel_path")
            if not rp:
                continue
            out[rp] = {
                "sha256":     payload.get("sha256"),
                "mtime":      payload.get("mtime"),
                "size_bytes": payload.get("size_bytes"),
            }
        if next_page is None:
            break
    return out


# Keep the old name as a thin alias for backwards compatibility / tests
def _existing_shas_for_root(client, root_name: str) -> dict[str, str]:
    return {rp: m["sha256"] for rp, m in _existing_files_for_root(client, root_name).items()
            if m.get("sha256")}


def _delete_file_chunks(client, root_name: str, rel_path: str) -> None:
    from qdrant_client.http import models as qm
    flt = qm.Filter(must=[
        qm.FieldCondition(key="root", match=qm.MatchValue(value=root_name)),
        qm.FieldCondition(key="rel_path", match=qm.MatchValue(value=rel_path)),
    ])
    client.delete(collection_name=QDRANT_COLLECTION,
                  points_selector=qm.FilterSelector(filter=flt))


def _point_id(sha: str, chunk_id: int) -> str:
    return str(uuid.uuid5(_UUID_NS, f"{sha}:{chunk_id}"))


# Pretty type labels for known extensions (used by _synthetic_context_doc).
# Anything not in this map is described as "<ext> file".
_EXT_LABELS: dict[str, str] = {
    # archives
    ".zip": "ZIP archive", ".rar": "RAR archive", ".7z": "7-Zip archive",
    ".tar": "TAR archive", ".gz": "gzip-compressed file", ".bz2": "bzip2 file",
    # images
    ".jpg": "JPEG image", ".jpeg": "JPEG image", ".png": "PNG image",
    ".heic": "HEIC image", ".tiff": "TIFF image", ".tif": "TIFF image",
    ".webp": "WebP image", ".bmp": "BMP image", ".gif": "GIF image",
    ".svg": "SVG image",
    # video
    ".mp4": "MP4 video", ".mov": "QuickTime video", ".avi": "AVI video",
    ".mkv": "Matroska video", ".webm": "WebM video", ".m4v": "M4V video",
    # audio
    ".mp3": "MP3 audio", ".wav": "WAV audio", ".m4a": "M4A audio",
    ".flac": "FLAC audio", ".ogg": "OGG audio",
    # documents
    ".pdf": "PDF document", ".docx": "Word document", ".doc": "Word document",
    ".xlsx": "Excel spreadsheet", ".xls": "Excel spreadsheet",
    ".xlsm": "Excel macro spreadsheet", ".pptx": "PowerPoint presentation",
    ".ppt": "PowerPoint presentation", ".rtf": "Rich Text document",
    # disk / installer
    ".dmg": "macOS disk image", ".iso": "ISO disk image",
    ".pkg": "macOS installer package", ".exe": "Windows executable",
    ".msi": "Windows installer",
    # data
    ".json": "JSON file", ".xml": "XML file", ".yaml": "YAML file",
    ".yml": "YAML file", ".csv": "CSV file", ".tsv": "TSV file",
}

_STATUS_NOTES: dict[str, str] = {
    "unsupported": "no text extractor for this file type",
    "password":    "encrypted; requires password to extract text",
    "corrupt":     "file could not be parsed (corrupt or unreadable)",
    "too_large":   "file too large to extract (over the size limit)",
    "empty":       "file processed but contained no extractable text",
    "no_chunks":   "extracted text was too short to chunk",
    "unreadable":  "file unreadable (permissions or filesystem error)",
}


def _synthetic_context_doc(rel_path: Path, abs_path: Path, status: str,
                           conv: dict, size_bytes: int) -> str:
    """
    Build a small "document" for files where text extraction failed,
    using the filename, folder hierarchy, and any metadata derivable
    from the naming convention. The result embeds reasonably well in
    the same semantic space as real documents because the user's
    restructured tree already encodes meaning in paths
    (e.g. "G - Gesundheit Health / GH - Doctors / GHD 2401 MRI scan
    results.pdf" semantically embeds near "MRI", "scan", "results",
    "Health", "Doctors").

    Stored as the chunk's `text` payload in Qdrant alongside the
    `text_source: "synthetic"` marker so the chat UI can label results
    as filename / folder matches rather than content matches.
    """
    parts = list(rel_path.parts)
    folders = parts[:-1]
    filename = parts[-1] if parts else abs_path.name

    ext = abs_path.suffix.lower()
    type_label = _EXT_LABELS.get(ext, f"{(ext or 'binary').lstrip('.')} file")
    note = _STATUS_NOTES.get(status, status or "no text content")

    lines: list[str] = []
    lines.append(f"File: {filename}")
    lines.append(f"Type: {type_label}")
    lines.append(f"Note: {note}")
    if size_bytes:
        # Human-friendly size — keeps the embedding short
        if size_bytes >= 1024 * 1024:
            sz = f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:
            sz = f"{size_bytes / 1024:.1f} KB"
        else:
            sz = f"{size_bytes} bytes"
        lines.append(f"Size: {sz}")
    if folders:
        lines.append(f"Folder hierarchy: {' / '.join(folders)}")
    if conv.get("yymm"):
        yymm = conv["yymm"]
        if len(yymm) == 4 and yymm.isdigit():
            yy, mm = yymm[:2], yymm[2:4]
            year = f"20{yy}" if int(yy) < 90 else f"19{yy}"
            lines.append(f"Date (from filename): {year}-{mm}")
    if conv.get("descriptive"):
        lines.append(f"Description: {conv['descriptive']}")
    if conv.get("shortcut"):
        lines.append(f"Compound shortcut: {conv['shortcut']}")
    if conv.get("status_marker"):
        lines.append(f"Status marker: {conv['status_marker']}")
    return "\n".join(lines)


# --- Indexing one file -----------------------------------------------------


def index_file(client, root_name: str, root_path: Path, rel_path: Path,
               existing: dict | None = None) -> tuple[str, int]:
    """
    Index one file. Returns (status, n_chunks).
    status: "new", "updated", "unchanged", "skip:<reason>"

    `existing` is the previously-indexed metadata for this (root, rel_path)
    from Qdrant, or None for a first-time index. Shape:
        {"sha256": "...", "mtime": "ISO", "size_bytes": int}

    Fast-path decision ladder (cheap → expensive):
      1. stat the file (both mtime + size unchanged → unchanged, no hash)
      2. sha256 the file (matches stored → touched only, payload updated)
      3. extract + chunk + embed + upsert (content changed / new)
    """
    abs_path = root_path / rel_path
    try:
        stat = abs_path.stat()
    except Exception:
        return ("skip:unreadable", 0)

    current_mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    current_size  = stat.st_size

    # ---------- Fast path 1: stat match → unchanged, no I/O beyond stat ----
    if existing is not None:
        prev_mtime = existing.get("mtime")
        prev_size  = existing.get("size_bytes")
        if prev_mtime == current_mtime and prev_size is not None and int(prev_size) == current_size:
            return ("unchanged", 0)

    # ---------- Fast path 2: hash match → content unchanged, just touched -
    try:
        sha = sha256_file(abs_path)
    except Exception:
        return ("skip:unreadable", 0)

    if existing is not None and existing.get("sha256") == sha:
        # Content identical; mtime/size shifted (e.g. rsync without checksum,
        # chmod, atime tweak). Update stored metadata in-place on every chunk
        # so next scan hits the O(stat) fast path — no re-extract / re-embed.
        from qdrant_client.http import models as qm
        flt = qm.Filter(must=[
            qm.FieldCondition(key="root",     match=qm.MatchValue(value=root_name)),
            qm.FieldCondition(key="rel_path", match=qm.MatchValue(value=str(rel_path))),
        ])
        try:
            client.set_payload(
                collection_name=QDRANT_COLLECTION,
                payload={
                    "mtime":      current_mtime,
                    "size_bytes": current_size,
                    "indexed_at": datetime.now().isoformat(timespec="seconds"),
                },
                points=qm.FilterSelector(filter=flt),
            )
        except Exception:
            pass  # not fatal; worst case we re-hash again next time
        return ("unchanged", 0)

    # ---------- Slow path: content actually changed (or brand-new file) ----
    res = extract(abs_path)
    conv = parse_convention(abs_path.name)
    # If text extraction succeeded with real content, use it as-is.
    # Otherwise fall back to a synthetic context document built from the
    # filename + folder hierarchy + parsed convention metadata. This makes
    # ZIP archives, image-only PDFs (where OCR yielded nothing), raw
    # images, videos, and any other unindexable file type discoverable
    # via filename / folder semantics in chat search.
    if res.status == "ok" and res.text.strip():
        text = res.text
        text_source = "extracted"
    else:
        text = _synthetic_context_doc(rel_path, abs_path, res.status, conv,
                                      current_size)
        text_source = "synthetic"
        if not text.strip():
            # Truly nothing to embed (no filename, no path) — give up.
            _delete_file_chunks(client, root_name, str(rel_path))
            return (f"skip:{res.status or 'empty'}", 0)

    chunks = split_text(text)
    if not chunks:
        _delete_file_chunks(client, root_name, str(rel_path))
        return ("skip:no_chunks", 0)

    vecs = embed(chunks)
    lang, lang_conf = detect_language(text)
    # `stat` + `current_mtime` + `current_size` were computed at the top of
    # this function; reuse instead of stat()ing again (extra NAS round-trip).
    file_id_hex = hashlib.sha256(str(abs_path).encode("utf-8")).hexdigest()[:16]

    from qdrant_client.http import models as qm

    # Delete previous chunks for this file (if any) before upsert
    _delete_file_chunks(client, root_name, str(rel_path))

    points = []
    for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
        points.append(qm.PointStruct(
            id=_point_id(sha, i),
            vector=vec.tolist(),
            payload={
                "root": root_name,
                "rel_path": str(rel_path),
                "filename": abs_path.name,
                "sha256": sha,
                "size_bytes": current_size,
                "mtime": current_mtime,
                "language": lang,
                "language_confidence": lang_conf,
                "chunk_id": i,
                "n_chunks": len(chunks),
                "text": chunk,
                "ocr_used": res.ocr_used,
                "pages": res.pages,
                "compound": conv.get("shortcut"),
                "yymm": conv.get("yymm"),
                "descriptive": conv.get("descriptive"),
                "version": conv.get("version"),
                "status_marker": conv.get("status_marker"),
                "file_id": file_id_hex,
                "indexed_at": datetime.now().isoformat(timespec="seconds"),
                # NEW: how the chunk's text was sourced. "extracted" =
                # real document text; "synthetic" = built from filename +
                # folder + convention because the file has no extractable
                # text (binary, archive, OCR-empty image, etc.). Chat UI
                # uses this to label results as "filename match".
                "text_source": text_source,
                "extraction_status": res.status,
            },
        ))
    client.upsert(collection_name=QDRANT_COLLECTION, points=points, wait=False)

    # `existing` was passed in (a dict of stored metadata) iff this file had
    # a previous Qdrant record. We've already returned early above when its
    # sha matched (→ "unchanged"). Reaching here with `existing is not None`
    # therefore means "content changed" → "updated"; otherwise "new".
    return ("updated" if existing is not None else "new", len(chunks))


# --- Delta scan over a root -----------------------------------------------


def delta_scan(root_name: str, root_path: Path) -> dict:
    """
    Walk `root_path`; upsert new/changed files; delete chunks for missing
    files. Returns a summary dict.
    """
    if not root_path.exists() or not root_path.is_dir():
        return {"error": f"root not found: {root_path}"}

    client = _qdrant()
    ensure_collection(client)
    existing = _existing_files_for_root(client, root_name)
    seen: set[str] = set()
    counts = {"new": 0, "updated": 0, "unchanged": 0, "deleted": 0,
              "skip": 0, "chunks_added": 0}
    errors: list[str] = []
    # Per-file skip records: {"path": "<rel>", "reason": "<reason>"} entries.
    # Capped at MAX_SKIPPED_RECORDED to keep the per-root JSON small even on
    # roots with thousands of unsupported files (e.g. image dumps).
    MAX_SKIPPED_RECORDED = 1000
    skipped: list[dict] = []
    skipped_overflow = 0
    # Per-file deletion records (rel paths only — no ambiguity to capture).
    deleted_paths: list[str] = []

    all_files = [p for p in root_path.rglob("*") if p.is_file()]
    if EXTENSION_DENY:
        files = [p for p in all_files if p.suffix.lower() not in EXTENSION_DENY]
        n_denied = len(all_files) - len(files)
        if n_denied:
            print(f"  → ignoring {n_denied:,} of {len(all_files):,} file(s) "
                  f"by KB_EXTENSION_DENY ({len(EXTENSION_DENY)} suffixes)")
    else:
        files = all_files
        n_denied = 0

    # File-size filter — apply AFTER extension deny (no point stat'ing files
    # we already know we're skipping). Records up to MAX_TOO_BIG_RECORDED
    # entries in the summary so you can see which large files were dropped.
    too_big_records: list[dict] = []
    MAX_TOO_BIG_RECORDED = 1000
    if MAX_FILE_SIZE_BYTES > 0:
        files_under_cap = []
        n_too_big = 0
        for p in files:
            try:
                sz = p.stat().st_size
            except OSError:
                # If we can't stat the file, let downstream extract() decide.
                files_under_cap.append(p)
                continue
            if sz > MAX_FILE_SIZE_BYTES:
                n_too_big += 1
                if len(too_big_records) < MAX_TOO_BIG_RECORDED:
                    too_big_records.append({
                        "path": str(p.relative_to(root_path)),
                        "size_mb": round(sz / 1024 / 1024, 1),
                    })
            else:
                files_under_cap.append(p)
        files = files_under_cap
        if n_too_big:
            print(f"  → ignoring {n_too_big:,} file(s) larger than "
                  f"{MAX_FILE_SIZE_MB:g} MB (KB_MAX_FILE_SIZE_MB)")
    else:
        n_too_big = 0

    # Runtime extension stats — counts how many files of each extension
    # have timed out so far. When a count reaches AUTO_DENY_THRESHOLD,
    # that extension goes into runtime_deny and subsequent files with
    # the same suffix are skipped without attempting to process them.
    # With AUTO_DENY_THRESHOLD=0 (default) the runtime_deny set stays
    # empty regardless of how many files time out.
    runtime_deny: set[str] = set()
    timeout_count_by_ext: dict[str, int] = {}
    timeout_active = _HAS_SIGALRM and FILE_TIMEOUT_SECONDS > 0
    if timeout_active:
        signal.signal(signal.SIGALRM, _alarm_handler)
        if AUTO_DENY_THRESHOLD > 0:
            policy = (f"after {AUTO_DENY_THRESHOLD} timeouts of the same "
                      f"extension, deny that extension for the rest of the scan")
        else:
            policy = "skip the individual file only (no extension deny)"
        print(f"  → per-file timeout: {FILE_TIMEOUT_SECONDS}s — {policy}")
    elif FILE_TIMEOUT_SECONDS > 0:
        print(f"  ⚠ per-file timeout requested ({FILE_TIMEOUT_SECONDS}s) "
              f"but signal.SIGALRM is unavailable on this platform — disabled")

    for p in tqdm(files, unit="file", desc=f"index {root_name}"):
        suffix = p.suffix.lower()
        # Runtime deny check (an earlier file with this extension hung;
        # don't bother trying any more of them this scan).
        if suffix in runtime_deny:
            counts["skip"] += 1
            if len(skipped) < MAX_SKIPPED_RECORDED:
                skipped.append({"path": str(p.relative_to(root_path)),
                                "reason": f"runtime_deny:{suffix}"})
            else:
                skipped_overflow += 1
            continue
        rel = p.relative_to(root_path)
        seen.add(str(rel))

        # Arm timeout
        if timeout_active:
            signal.alarm(FILE_TIMEOUT_SECONDS)
        t0 = time.monotonic()
        try:
            status, nc = index_file(client, root_name, root_path, rel,
                                    existing.get(str(rel)))
        except _FileProcessingTimeout:
            elapsed = time.monotonic() - t0
            if timeout_active:
                signal.alarm(0)
            # Try to free GPU state that may be half-allocated from an
            # interrupted embed batch.
            try:
                from kb.chunk_embed import _release_gpu_cache
                _release_gpu_cache()
            except Exception:
                pass
            counts["skip"] += 1
            reason = f"timeout:{elapsed:.0f}s"
            if len(skipped) < MAX_SKIPPED_RECORDED:
                skipped.append({"path": str(rel), "reason": reason})
            else:
                skipped_overflow += 1
            print(f"\n  ⚠ TIMEOUT after {elapsed:.0f}s on {rel}", flush=True)
            # Track timeouts per extension. Only auto-deny when the count
            # reaches the (opt-in) threshold — by default the threshold
            # is 0, which means we never auto-deny based on extension and
            # each timeout only skips that one file.
            if suffix:
                timeout_count_by_ext[suffix] = (
                    timeout_count_by_ext.get(suffix, 0) + 1
                )
                if (AUTO_DENY_THRESHOLD > 0
                        and timeout_count_by_ext[suffix] >= AUTO_DENY_THRESHOLD
                        and suffix not in runtime_deny):
                    runtime_deny.add(suffix)
                    print(f"  ⚠ {timeout_count_by_ext[suffix]} '{suffix}' files "
                          f"timed out — adding '{suffix}' to runtime deny list; "
                          f"subsequent {suffix} files in this scan will be skipped",
                          flush=True)
            continue
        except Exception as e:
            if timeout_active:
                signal.alarm(0)
            errors.append(f"{rel}: {e}")
            continue
        else:
            if timeout_active:
                signal.alarm(0)

        if status.startswith("skip:"):
            counts["skip"] += 1
            reason = status.split(":", 1)[1] if ":" in status else "unknown"
            if len(skipped) < MAX_SKIPPED_RECORDED:
                skipped.append({"path": str(rel), "reason": reason})
            else:
                skipped_overflow += 1
        else:
            counts[status] = counts.get(status, 0) + 1
            counts["chunks_added"] += nc

    # Disarm any leftover alarm before the deletion sweep.
    if timeout_active:
        signal.alarm(0)

    # Delete chunks for files no longer present
    disappeared = set(existing) - seen
    for rp in disappeared:
        _delete_file_chunks(client, root_name, rp)
        counts["deleted"] += 1
        deleted_paths.append(rp)

    counts["errors"] = errors
    counts["skipped"] = skipped                  # NEW: per-file skip records
    counts["skipped_overflow"] = skipped_overflow  # # NOT recorded beyond cap
    counts["deleted_paths"] = deleted_paths        # NEW: which files vanished
    counts["root"] = root_name
    counts["root_path"] = str(root_path)
    counts["scanned_files"] = len(files)
    counts["ignored_by_deny"] = n_denied
    counts["deny_extensions"] = sorted(EXTENSION_DENY)
    counts["ignored_too_big"] = n_too_big
    counts["max_file_size_mb"] = MAX_FILE_SIZE_MB if MAX_FILE_SIZE_BYTES > 0 else 0
    counts["too_big_files"] = too_big_records
    counts["runtime_denied_extensions"] = sorted(runtime_deny)
    counts["timeout_count_by_extension"] = dict(sorted(timeout_count_by_ext.items()))
    counts["file_timeout_seconds"] = FILE_TIMEOUT_SECONDS if timeout_active else 0
    counts["auto_deny_threshold"] = AUTO_DENY_THRESHOLD
    counts["scanned_at"] = datetime.now().isoformat(timespec="seconds")
    return counts


def count_root_chunks(root_name: str) -> int:
    """Return the number of chunks currently indexed for `root_name` in the
    active Qdrant collection (under the active KB_VARIANT)."""
    from qdrant_client.http import models as qm
    client = _qdrant()
    ensure_collection(client)
    flt = qm.Filter(must=[
        qm.FieldCondition(key="root", match=qm.MatchValue(value=root_name)),
    ])
    res = client.count(collection_name=QDRANT_COLLECTION,
                       count_filter=flt, exact=True)
    return int(getattr(res, "count", 0))


def delete_root(root_name: str) -> int:
    """
    Delete EVERY chunk in the active collection whose `root` payload field
    equals `root_name`. Returns the number of chunks that were present
    before deletion (so the caller can report "deleted N chunks").

    Idempotent: calling twice on a now-empty root is a no-op (returns 0).
    Does NOT touch the source files on the NAS, the kb/data/<variant>/
    last_scan summary, or any pipeline state file — those are managed
    separately by the caller (kb.py / run.py).
    """
    from qdrant_client.http import models as qm
    client = _qdrant()
    ensure_collection(client)
    flt = qm.Filter(must=[
        qm.FieldCondition(key="root", match=qm.MatchValue(value=root_name)),
    ])
    before = count_root_chunks(root_name)
    if before == 0:
        return 0
    client.delete(collection_name=QDRANT_COLLECTION,
                  points_selector=qm.FilterSelector(filter=flt),
                  wait=True)
    return before


def collection_stats() -> dict:
    client = _qdrant()
    ensure_collection(client)
    info = client.get_collection(QDRANT_COLLECTION)
    # Qdrant >=1.15 moved some fields; stay tolerant.
    return {
        "points":  getattr(info, "points_count", None)
                   or getattr(info, "vectors_count", None)
                   or 0,
        "indexed": getattr(info, "indexed_vectors_count", 0),
        "status":  str(getattr(info, "status", "")),
    }
