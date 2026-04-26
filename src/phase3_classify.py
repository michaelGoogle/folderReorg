"""
Phase 3 — LLM classification with hierarchy-aware compound shortcuts.

Two passes:
  Pass A (once per cluster): sample N files, LLM proposes folder_name.
                             Cluster `shortcut` is ADVISORY ONLY now —
                             real shortcuts come from the file's path.
  Pass B (once per file):    LLM proposes descriptive_name + content_date.

Target path logic (per user decisions A/B/C):
  Case A — every ancestor folder of the file is shortcut-prefixed:
    preserve the path; upgrade each folder to full compound form; use the
    full compound as the filename prefix.

  Case B — some ancestor folder is NOT shortcut-prefixed:
    place the file in a new sub-folder of the deepest shortcut-prefixed
    ancestor. The new folder's compound = anchor + k new letters where
    k is 1 (≤10 sibling children) or 3 (>10 sibling children). Collisions
    bump the new letters only (FIGAR → FIGAS → FIGAT …).

  Case C — no shortcut-prefixed ancestor at all:
    fall back to root-level "X - Unsorted".
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from src.config import (
    CONTENT_DATE_DEVIATION_MONTHS,
    DATA_DIR,
    DEFAULT_VERSION_UNVERSIONED,
    LLM_MODEL,
    PER_FILE_LLM_MODEL,
)
from src.hierarchy import (
    AnchorInfo,
    AnchorStep,
    build_anchor_index,
    compound_chain,
    fully_anchored,
    new_compound,
    parse_folder_token,
    target_parent_path,
)
from src.llm import chat, extract_json
from src.naming import (
    already_conforms,
    assemble_new_name,
    extract_meaningful_token,
    safe_name,
)
from src.translate_name import to_english


# --- Prompts ---------------------------------------------------------------

TRANSLATION_RULES = """
LANGUAGE RULES (CRITICAL - your output MUST be in English):
- Always produce the output in ENGLISH, regardless of the source language.
- BEFORE RESPONDING, re-read your proposed name. If it contains ANY non-English
  common noun (like "Rechnung", "Kontoauszug", "Bestätigung", "Vertrag",
  "Versicherung", "Quittung", "Steuer", "Mietvertrag", "Spendenbescheinigung"),
  REPLACE IT with the English equivalent before responding.

- DO NOT translate proper nouns, brand names, organisation names, product names,
  or people's names. Examples:
    "UBS" -> "UBS"
    "Manulife" -> "Manulife"
    "Deutsche Bank" -> "Deutsche Bank"
    "Swisscard AECS" -> "Swisscard AECS"
    "St Georgen" -> "St Georgen"

- DO translate common nouns and descriptive words. Examples:
    "Rechnung" -> "Invoice"                (NOT "Rechnung")
    "Kontoauszug" -> "Account Statement"   (NOT "Kontoauszug")
    "Spendenbescheinigung" -> "Donation Receipt"  (NOT "Spendenbescheinigung")
    "Bestätigung" -> "Confirmation"        (NOT "Bestätigung")
    "Mietvertrag" -> "Rental Agreement"
    "Versicherungspolice" -> "Insurance Policy"
    "Quittung" -> "Receipt"
    "Steuerberechnung" -> "Tax Calculation"
    "Akontorechnung" -> "Preliminary Invoice"

  BAD:  "Swisscard AECS Rechnung"          <-- still has German
  GOOD: "Swisscard AECS Invoice"

  BAD:  "Spende Bestätigung St Georgen"    <-- still has German
  GOOD: "Donation Receipt St Georgen"

- Preserve numbers, dates, codes, and identifiers exactly as they appear.
- If unsure whether a word is a proper noun, keep the original.
"""

CLUSTER_SYS = (
    "You are a file organisation assistant. Given samples of document text "
    "from one cluster, propose:\n"
    "1. A short descriptive FOLDER NAME in ENGLISH (3-6 words, title case, "
    "no punctuation except spaces)\n"
    "2. A one-sentence rationale.\n\n"
    f"{TRANSLATION_RULES}\n"
    'Respond ONLY as JSON: {"folder_name": "...", "rationale": "..."}'
)

FILE_SYS = (
    "You are naming a single file that belongs to a known category.\n\n"
    "Respond ONLY as JSON with two fields:\n"
    "{\n"
    '  "descriptive_name": "<3-8 words, title case, no punctuation except spaces, '
    'no year, no version, no underscores>",\n'
    '  "content_date":     "<YYYY-MM or null>"\n'
    "}\n\n"
    "The content_date is the date the document is ABOUT (invoice date, statement period, "
    "letter date), NOT the date you are writing. null if no clear date appears.\n\n"
    f"{TRANSLATION_RULES}"
)


# --- Helpers ---------------------------------------------------------------


def _yymm_to_months(yymm: str) -> int:
    return int(yymm[:2]) * 12 + int(yymm[2:])


def reconcile_yymm(mtime_yymm: str, content_date: str | None) -> tuple[str, str]:
    """
    Returns (chosen_yymm, source) where source ∈ {'mtime', 'content'}.
    Uses content date only if it deviates from mtime by > threshold (§1.3).
    """
    if not content_date or content_date.lower() in ("null", "none", ""):
        return mtime_yymm, "mtime"
    m = re.match(r"^\s*(\d{4})-(\d{1,2})\s*$", str(content_date))
    if not m:
        return mtime_yymm, "mtime"
    cy, cm = int(m.group(1)), int(m.group(2))
    if not (1 <= cm <= 12):
        return mtime_yymm, "mtime"
    content_yymm = f"{cy % 100:02d}{cm:02d}"
    if abs(_yymm_to_months(mtime_yymm) - _yymm_to_months(content_yymm)) > CONTENT_DATE_DEVIATION_MONTHS:
        return content_yymm, "content"
    return mtime_yymm, "mtime"


# Fix D — context hints that a file contains credentials/login info.
# Matches folder and filename components; triggers the (password) marker
# on the proposed_name even when the file is plain text (not an encrypted PDF).
PASSWORD_CONTEXT_RE = re.compile(
    r"(?:pass[wv][oöa]rd?|password|credential|login|secret|keychain|\.kdbx)",
    re.I,
)


def _looks_like_password_context(rel_path: Path, filename: str) -> bool:
    for part in rel_path.parts:
        if PASSWORD_CONTEXT_RE.search(part):
            return True
    return bool(PASSWORD_CONTEXT_RE.search(filename))


NON_TEXT_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".raw", ".cr2", ".nef",
    ".gif", ".bmp", ".tiff", ".webp",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".exe", ".dll", ".iso", ".dbf", ".url", ".crdownload",
}


# --- Pass A: cluster naming (shortcut ignored; only folder_name used) -----


def name_cluster(
    cluster_id: int,
    samples: list[str],
    *,
    model: str = LLM_MODEL,
) -> dict:
    joined = "\n\n--- NEXT FILE ---\n\n".join(s[:800] for s in samples[:8])
    user = f"Here are {len(samples[:8])} sample documents from cluster {cluster_id}:\n\n{joined}"
    try:
        raw = chat(CLUSTER_SYS, user, model=model)
    except Exception as e:
        raw = ""
        print(f"  cluster {cluster_id}: LLM error {e}")
    parsed = extract_json(raw) or {}
    return {
        "folder_name": safe_name(parsed.get("folder_name") or f"Cluster {cluster_id}"),
        "rationale":   (parsed.get("rationale") or "").strip()[:200],
    }


def build_cluster_catalog(
    assignments: pd.DataFrame,
    extracted_dir: Path,
    *,
    model: str = LLM_MODEL,
    samples_per_cluster: int = 8,
) -> pd.DataFrame:
    rows = []
    for cid, grp in tqdm(
        list(assignments[assignments["cluster_id"] >= 0].groupby("cluster_id")),
        desc="cluster-name",
        unit="cluster",
    ):
        sample_ids = grp["file_id"].sample(
            min(samples_per_cluster, len(grp)), random_state=42,
        ).tolist()
        samples = []
        for fid in sample_ids:
            p = extracted_dir / f"{fid}.txt"
            if p.exists():
                samples.append(p.read_text(encoding="utf-8"))
        info = name_cluster(int(cid), samples, model=model) if samples else \
               {"folder_name": f"Cluster {cid}", "rationale": "no samples"}
        rows.append({
            "cluster_id":  int(cid),
            "folder_name": info["folder_name"],
            "rationale":   info["rationale"],
            "n_files":     int(len(grp)),
        })
    return pd.DataFrame(rows)


# --- Pass B: per-file naming ----------------------------------------------


def name_file(
    folder_name: str,
    text: str,
    *,
    filename: str | None = None,
    model: str = PER_FILE_LLM_MODEL,
) -> dict:
    """
    Ask the LLM for a descriptive name + content-date for a single file.

    `filename` is the original source filename (e.g. "gmx.txt", "PFCMIL 20170521 Invoice.pdf").
    For short-text files, it's often the strongest signal (service name, brand,
    date-in-name), so we pass it explicitly as a hint.
    """
    hint = ""
    if filename:
        stem, _dot, ext = filename.rpartition(".")
        stem = stem or filename
        hint = (
            f"Original filename: {filename!r}\n"
            f"(stem: {stem!r}; ext: .{ext if _dot else ''})\n"
            "If the stem contains a recognisable service / brand / person / category "
            "(e.g. 'gmx', 'gmail', 'wifi', 'Router', 'UBS'), prefer it as the core of "
            "the descriptive_name rather than generic folder-level text.\n\n"
        )
    user = (
        f"Folder: {folder_name}\n"
        f"{hint}"
        f"File content (truncated):\n{text[:1500]}\n\n"
        "Respond with the JSON object."
    )
    try:
        raw = chat(FILE_SYS, user, model=model)
    except Exception:
        raw = ""
    parsed = extract_json(raw) or {}
    desc = safe_name(parsed.get("descriptive_name") or "Unnamed Document")
    content_date = parsed.get("content_date")
    if isinstance(content_date, str) and content_date.strip().lower() in {"null", "none", ""}:
        content_date = None
    return {"descriptive_name": desc, "content_date": content_date}


# --- Target resolution (the new hierarchy-aware logic) --------------------


class TargetInfo:
    """Resolved target for one file."""
    __slots__ = ("proposed_parent_rel", "compound_shortcut", "case", "rationale")

    def __init__(self, parent: str, compound: str, case: str, rationale: str = ""):
        self.proposed_parent_rel = parent   # rel path under target root, e.g. "F - Finance/FI - Invoices/FICOM - Computer Jakob"
        self.compound_shortcut = compound   # filename prefix, e.g. "FICOM"
        self.case = case                    # "A", "B", or "C"
        self.rationale = rationale


def resolve_target(
    rel_path: Path,
    anchor_index: dict[str, AnchorInfo],
    *,
    cluster_folder_name: str | None = None,  # Case B only
    newly_registered: dict[str, str] | None = None,
    compounds_by_folder: dict[str, str] | None = None,
) -> TargetInfo:
    """
    Decide the target parent rel_path and filename compound shortcut for a file.

    `compounds_by_folder` is the authoritative compounds map produced by
    build_anchor_index — pass it in so Case A files use the SAME compounds
    the index assigned (with normalization applied).

    `newly_registered` is a per-run dict mapping (anchor_compound + proposal_key)
    → assigned compound, so that multiple files from the same messy folder all
    end up in the SAME new folder (and don't each spawn their own).
    """
    chain = compound_chain(rel_path, compounds_by_folder=compounds_by_folder)
    dir_parts = rel_path.parts[:-1]

    # --- Case A: fully anchored (every ancestor folder is shortcut-prefixed) -
    if chain and len(chain) == len(dir_parts):
        parent_rel = str(target_parent_path(chain))
        return TargetInfo(parent_rel, chain[-1].compound, "A",
                          f"fully anchored at {chain[-1].compound}")

    # --- Case B: some ancestor is messy, but at least one anchor exists ------
    if chain:
        anchor = chain[-1]
        # Upgraded path up to the anchor
        anchor_rel = target_parent_path(chain)
        anchor_info = anchor_index.get(anchor.compound)
        if anchor_info is None:
            # Anchor wasn't in the preflight index — synthesize a minimal one
            anchor_info = AnchorInfo(
                anchor_compound=anchor.compound,
                children_compounds=set(),
                children_count=0,
                additional_letter_mode=1,
            )

        # Fix A — always prefer the SOURCE folder name over any LLM cluster
        # proposal for Case B. You've already told the pipeline what the leaf
        # folder is ("Passwörter", "Julius Bär", "004 Urmein", …); a cluster
        # name derived from OTHER files in the same cluster shouldn't override
        # that signal. The cluster's folder_name still feeds Pass B per-file
        # naming via `leaf_folder` in the caller — for context, not location.
        messy_folder_name = dir_parts[len(chain)]
        folder_display_name = messy_folder_name or (cluster_folder_name or "Unsorted")

        # Deduplicate: same anchor + same source folder → same new target folder
        key = f"{anchor.compound}|{folder_display_name}"
        if newly_registered is not None and key in newly_registered:
            compound = newly_registered[key]
            new_folder_name = f"{compound} - {folder_display_name}"
        else:
            compound, _added = new_compound(anchor_info, folder_display_name)
            new_folder_name = f"{compound} - {folder_display_name}"
            # Register so subsequent calls see it as taken
            anchor_info.children_compounds.add(compound)
            if newly_registered is not None:
                newly_registered[key] = compound

        return TargetInfo(
            str(anchor_rel / new_folder_name),
            compound,
            "B",
            f"new subfolder under {anchor.compound} (source: '{folder_display_name}')",
        )

    # --- Case C: no shortcut-prefixed ancestor at all ------------------------
    return TargetInfo("X - Unsorted", "X", "C", "no anchor ancestor")


# --- Main ------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory",    type=Path, default=DATA_DIR / "inventory.csv")
    ap.add_argument("--extraction",   type=Path, default=DATA_DIR / "extraction_results.csv")
    ap.add_argument("--lang",         type=Path, default=DATA_DIR / "inventory_lang.csv")
    ap.add_argument("--clusters",     type=Path, default=DATA_DIR / "cluster_assignments.csv")
    ap.add_argument("--source",       type=Path, required=False, default=None,
                    help="Source root (used for building the anchor index)")
    ap.add_argument("--out-catalog",  type=Path, default=DATA_DIR / "cluster_catalog.csv")
    ap.add_argument("--out-plan",     type=Path, default=DATA_DIR / "rename_plan.csv")
    ap.add_argument("--extracted-dir", type=Path, default=DATA_DIR / "extracted_text")
    ap.add_argument("--cluster-model", type=str, default=LLM_MODEL)
    ap.add_argument("--file-model",    type=str, default=PER_FILE_LLM_MODEL)
    ap.add_argument("--limit",        type=int, default=None,
                    help="Only classify this many text files (for dev/debug)")
    args = ap.parse_args()

    inv = pd.read_csv(args.inventory)
    # Defend against NaN in string columns
    for col in ("ext", "filename", "parent_dir", "abs_path", "rel_path"):
        if col in inv.columns:
            inv[col] = inv[col].fillna("").astype(str)
    ext = pd.read_csv(args.extraction)
    lang = pd.read_csv(args.lang) if args.lang.exists() else pd.DataFrame(
        columns=["file_id", "lang", "lang_confidence"],
    )
    clu = pd.read_csv(args.clusters)

    # --- Preflight: build the anchor index from the source tree ----------
    # Source root = common prefix of all inv.rel_path, resolved via abs_path.
    if args.source is None:
        # Derive from abs_path - rel_path subtraction
        sample = inv.iloc[0]
        abs_p = Path(sample["abs_path"])
        rel_p = Path(sample["rel_path"])
        # Strip the rel_path tail from abs_path
        source_root = abs_p
        for _ in rel_p.parts:
            source_root = source_root.parent
    else:
        source_root = args.source
    print(f"Building anchor index from {source_root} …")
    anchor_index, compounds_by_folder = build_anchor_index(source_root)
    print(f"  anchors indexed: {len(anchor_index)}, "
          f"resolved compounds: {len(compounds_by_folder)}")

    # --- Pass A: cluster naming ---------------------------------------------
    print("== Pass A: naming clusters (shortcut ignored; folder_name used for Case B) ==")
    catalog = build_cluster_catalog(clu, args.extracted_dir, model=args.cluster_model)
    catalog.to_csv(args.out_catalog, index=False)
    print(f"  wrote {args.out_catalog} ({len(catalog)} clusters)")

    # --- Merge all inputs ---------------------------------------------------
    df = inv.merge(ext, on="file_id", how="left")
    df = df.merge(lang, on="file_id", how="left")
    df = df.merge(clu, on="file_id", how="left")
    df = df.merge(catalog[["cluster_id", "folder_name"]], on="cluster_id", how="left")
    df["version"] = DEFAULT_VERSION_UNVERSIONED

    rows: list[dict] = []
    newly_registered: dict[str, str] = {}

    # --- Text files (status == ok) ------------------------------------------
    text_df = df[df["status"] == "ok"].copy()
    if args.limit:
        text_df = text_df.head(args.limit)

    n_case_a = n_case_b = n_case_c = n_passthrough = 0
    print(f"== Pass B: naming {len(text_df):,} text files ==")
    for r in tqdm(text_df.to_dict("records"), unit="file", desc="file-name"):
        fid = r["file_id"]
        ext_s = str(r.get("ext") or "")
        rel = Path(r["rel_path"])

        # --- Already-conforming pass-through ---
        if already_conforms(str(r.get("filename") or "")):
            n_passthrough += 1
            # Still upgrade the parent path to compound form (using the
            # authoritative index so we get NORMALISED compounds).
            chain = compound_chain(rel, compounds_by_folder=compounds_by_folder)
            if chain and len(chain) == len(rel.parts) - 1:
                parent_rel = str(target_parent_path(chain))
            else:
                parent_rel = str(rel.parent)
            rows.append({
                "file_id":           fid,
                "current_path":      r["abs_path"],
                "original_name":     r["filename"],
                "original_parent":   r["parent_dir"],
                "source_lang":       r.get("lang"),
                "source_lang_confidence": r.get("lang_confidence"),
                "cluster_id":        int(r["cluster_id"]) if not pd.isna(r.get("cluster_id")) else -1,
                "proposed_parent":   parent_rel,
                "proposed_name":     r["filename"],
                "yymm_source":       "mtime",
                "content_date":      None,
                "confidence":        "high",
                "kind":              "text-pass-through",
                "case":              "A" if chain else "C",
                "reason":            "already matches naming convention (§11.6)",
            })
            continue

        # --- Resolve target location ---
        cluster_folder_name = r.get("folder_name")  # from cluster catalog merge
        if pd.isna(cluster_folder_name) or int(r.get("cluster_id", -1)) < 0:
            cluster_folder_name = None  # noise files use messy-folder name or preserved path
        target = resolve_target(
            rel,
            anchor_index,
            cluster_folder_name=cluster_folder_name if cluster_folder_name else None,
            newly_registered=newly_registered,
            compounds_by_folder=compounds_by_folder,
        )
        if target.case == "A": n_case_a += 1
        elif target.case == "B": n_case_b += 1
        else: n_case_c += 1

        # --- Case C preserve: no shortcut-prefixed ancestor → preserve path + name ---
        # Safer than forcing "X - Unsorted/<LLM-generated name>" which loses
        # the original folder structure and any information in the source
        # filename. Flagged low-confidence for human review.
        if target.case == "C":
            rel_parent = str(rel.parent) if str(rel.parent) != "." else ""
            rows.append({
                "file_id":           fid,
                "current_path":      r["abs_path"],
                "original_name":     r["filename"],
                "original_parent":   r["parent_dir"],
                "source_lang":       r.get("lang"),
                "source_lang_confidence": r.get("lang_confidence"),
                "cluster_id":        int(r["cluster_id"]) if not pd.isna(r.get("cluster_id")) else -1,
                "proposed_parent":   rel_parent,
                "proposed_name":     r["filename"],
                "yymm_source":       "mtime",
                "content_date":      None,
                "confidence":        "low",
                "kind":              "text-preserved",
                "case":              "C",
                "reason":            "no shortcut-prefixed ancestor — path and name preserved",
            })
            continue

        # --- Load extracted text (if any) ---
        text_path = r.get("text_path")
        text = ""
        if isinstance(text_path, str) and text_path:
            p = Path(text_path)
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="ignore")
        text_chars = int(r.get("text_chars") or 0)

        # Folder-name context for the LLM: use the LEAF upgraded folder name
        # (e.g. "FICOM - Computer Jakob") so the model knows what the file is about.
        leaf_folder = Path(target.proposed_parent_rel).name

        # --- Fix C — short-text bypass: skip LLM for tiny files; the filename is
        # a stronger signal than a hallucinated summary of ~30 chars of content.
        short_bypass = text_chars < 100
        if short_bypass:
            stem = Path(r["filename"]).stem
            # Clean the stem for use as a descriptive name
            desc_from_stem = safe_name(stem) or "File"
            out = {"descriptive_name": desc_from_stem, "content_date": None}
            reason = target.rationale + " [short-text bypass: used filename stem]"
        else:
            # --- Fix B — pass the original filename as a hint to the LLM.
            out = name_file(
                leaf_folder, text,
                filename=r["filename"],
                model=args.file_model,
            )
            reason = target.rationale

        yymm, yymm_src = reconcile_yymm(str(r["yymm"]).zfill(4), out["content_date"])

        # --- Fix D — password-context marker for plain-text credential files.
        pw_tag = _looks_like_password_context(rel, r["filename"])

        new_name = assemble_new_name(
            shortcut=target.compound_shortcut,
            yymm=yymm,
            descriptive=out["descriptive_name"],
            version=r["version"],
            ext=ext_s,
            password_tag=pw_tag,
        )

        # Confidence rule: a short-bypassed file is inherently lower-certainty
        # than an LLM-named one; demote to low so reviewer eyeballs it.
        if short_bypass:
            confidence = "low"
        elif text_chars > 500:
            confidence = "high"
        else:
            confidence = "medium"

        rows.append({
            "file_id":           fid,
            "current_path":      r["abs_path"],
            "original_name":     r["filename"],
            "original_parent":   r["parent_dir"],
            "source_lang":       r.get("lang"),
            "source_lang_confidence": r.get("lang_confidence"),
            "cluster_id":        int(r["cluster_id"]) if not pd.isna(r.get("cluster_id")) else -1,
            "proposed_parent":   target.proposed_parent_rel,
            "proposed_name":     new_name,
            "yymm_source":       yymm_src,
            "content_date":      out.get("content_date"),
            "confidence":        confidence,
            "kind":              "text",
            "case":              target.case,
            "reason":            reason + (" [password-context]" if pw_tag else ""),
        })

    print(f"  text: Case A={n_case_a}, Case B={n_case_b}, Case C={n_case_c}, pass-through={n_passthrough}")

    # --- Non-text files -----------------------------------------------------
    non_text_statuses = {
        "quarantine_no_extractor",
        "quarantine_image_only",
        "quarantine_password",
    }
    non_text_mask = df["status"].isin(non_text_statuses) | df["ext"].isin(NON_TEXT_EXTS)
    non_text_df = df[non_text_mask].copy()

    n_nt_a = n_nt_b = n_nt_c = n_nt_pass = 0
    print(f"== Non-text: {len(non_text_df):,} files ==")
    for r in tqdm(non_text_df.to_dict("records"), unit="file", desc="non-text"):
        ext_s = str(r.get("ext") or "")
        rel = Path(r["rel_path"])

        # Pass-through
        if already_conforms(str(r.get("filename") or "")):
            n_nt_pass += 1
            chain = compound_chain(rel, compounds_by_folder=compounds_by_folder)
            if chain and len(chain) == len(rel.parts) - 1:
                parent_rel = str(target_parent_path(chain))
            else:
                parent_rel = str(rel.parent)
            rows.append({
                "file_id":           r["file_id"],
                "current_path":      r["abs_path"],
                "original_name":     r["filename"],
                "original_parent":   r["parent_dir"],
                "source_lang":       r.get("lang"),
                "source_lang_confidence": r.get("lang_confidence"),
                "cluster_id":        -1,
                "proposed_parent":   parent_rel,
                "proposed_name":     r["filename"],
                "yymm_source":       "mtime",
                "content_date":      None,
                "confidence":        "high",
                "kind":              "non-text-pass-through",
                "case":              "A" if chain else "C",
                "reason":            "already matches naming convention (§11.6)",
            })
            continue

        target = resolve_target(
            rel,
            anchor_index,
            cluster_folder_name=None,
            newly_registered=newly_registered,
            compounds_by_folder=compounds_by_folder,
        )
        if target.case == "A": n_nt_a += 1
        elif target.case == "B": n_nt_b += 1
        else: n_nt_c += 1

        # Case C preserve — same safety net as in the text branch.
        if target.case == "C":
            rel_parent = str(rel.parent) if str(rel.parent) != "." else ""
            rows.append({
                "file_id":           r["file_id"],
                "current_path":      r["abs_path"],
                "original_name":     r["filename"],
                "original_parent":   r["parent_dir"],
                "source_lang":       r.get("lang"),
                "source_lang_confidence": r.get("lang_confidence"),
                "cluster_id":        -1,
                "proposed_parent":   rel_parent,
                "proposed_name":     r["filename"],
                "yymm_source":       "mtime",
                "content_date":      None,
                "confidence":        "low",
                "kind":              "non-text-preserved",
                "case":              "C",
                "reason":            "no shortcut-prefixed ancestor — path and name preserved",
            })
            continue

        leaf_folder = Path(target.proposed_parent_rel).name
        # Strip the compound prefix from the leaf folder to get the human part
        m = parse_folder_token(leaf_folder)
        folder_desc = m.human if m else leaf_folder
        lang_hint = r.get("lang")
        folder_desc_en = to_english(
            folder_desc,
            lang_hint=lang_hint if isinstance(lang_hint, str) else None,
        )
        stem = Path(r["filename"]).stem
        token = extract_meaningful_token(stem)
        desc = folder_desc_en + (f" {token}" if token else "")
        image_tag    = (r.get("status") == "quarantine_image_only")
        # Fix D — (password) marker applies to:
        #   · status == quarantine_password  (encrypted PDFs we tried to open)
        #   · filename/folder hints at credentials  (keychains, kdbx, Passwörter folders)
        password_tag = (
            r.get("status") == "quarantine_password"
            or _looks_like_password_context(rel, r["filename"])
        )
        new_name = assemble_new_name(
            shortcut=target.compound_shortcut,
            yymm=str(r["yymm"]).zfill(4),
            descriptive=desc,
            version=DEFAULT_VERSION_UNVERSIONED,
            ext=ext_s,
            image_tag=image_tag,
            password_tag=password_tag,
        )

        rows.append({
            "file_id":           r["file_id"],
            "current_path":      r["abs_path"],
            "original_name":     r["filename"],
            "original_parent":   r["parent_dir"],
            "source_lang":       r.get("lang"),
            "source_lang_confidence": r.get("lang_confidence"),
            "cluster_id":        -1,
            "proposed_parent":   target.proposed_parent_rel,
            "proposed_name":     new_name,
            "yymm_source":       "mtime",
            "content_date":      None,
            "confidence":        "medium",
            "kind":              "non-text",
            "case":              target.case,
            "reason":            target.rationale,
        })

    print(f"  non-text: Case A={n_nt_a}, Case B={n_nt_b}, Case C={n_nt_c}, pass-through={n_nt_pass}")

    plan = pd.DataFrame(rows)
    plan["decision"] = "approve"
    plan.to_csv(args.out_plan, index=False)
    print(f"OK — rename_plan: {len(plan):,} rows → {args.out_plan}")


if __name__ == "__main__":
    main()
