"""
Phase 1c — language detection (plan §12.3).

Reads extracted_text/<file_id>.txt, writes inventory_lang.csv with
(file_id, lang, lang_confidence).

Implementation note: the plan calls for fasttext lid.176.bin, but the
`fasttext-wheel` package is broken against NumPy 2.x (upstream uses
`np.array(..., copy=False)` which is illegal in NumPy 2). We use
`lingua-language-detector` instead — pure Python, no C deps, higher
accuracy on European languages than fasttext.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from src.config import DATA_DIR, EXTRACTED_TEXT_DIR

# Subset the detector to the languages we actually expect. Faster and more
# accurate than scanning all 75+ supported languages.
EXPECTED_LANGS = ["ENGLISH", "GERMAN", "FRENCH", "ITALIAN", "SPANISH", "DUTCH", "PORTUGUESE"]

# ISO-639-1 mapping for the same set (output format matches the plan §12.3).
_ISO = {
    "ENGLISH": "en", "GERMAN": "de", "FRENCH": "fr", "ITALIAN": "it",
    "SPANISH": "es", "DUTCH": "nl", "PORTUGUESE": "pt",
}


def _build_detector():
    from lingua import Language, LanguageDetectorBuilder
    langs = [getattr(Language, name) for name in EXPECTED_LANGS]
    return LanguageDetectorBuilder.from_languages(*langs).with_preloaded_language_models().build()


def detect(detector, text: str) -> tuple[str, float]:
    text = text.replace("\n", " ").strip()[:2000]
    if len(text) < 20:
        return ("und", 0.0)
    confs = detector.compute_language_confidence_values(text)
    if not confs:
        return ("und", 0.0)
    top = confs[0]  # already sorted descending
    return (_ISO.get(top.language.name, "und"), float(top.value))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extraction", type=Path, default=DATA_DIR / "extraction_results.csv")
    ap.add_argument("--out",        type=Path, default=DATA_DIR / "inventory_lang.csv")
    args = ap.parse_args()

    detector = _build_detector()

    ext_df = pd.read_csv(args.extraction)
    ok = ext_df[ext_df["status"] == "ok"].to_dict("records")

    rows: list[tuple[str, str, float]] = []
    for r in tqdm(ok, unit="file", desc="lang"):
        txt_path = Path(r["text_path"]) if r["text_path"] else EXTRACTED_TEXT_DIR / f"{r['file_id']}.txt"
        if not txt_path.exists():
            rows.append((r["file_id"], "und", 0.0))
            continue
        try:
            text = txt_path.read_text(encoding="utf-8")
        except Exception:
            rows.append((r["file_id"], "und", 0.0))
            continue
        lang, conf = detect(detector, text)
        rows.append((r["file_id"], lang, conf))

    with args.out.open("w", newline="", encoding="utf-8") as w:
        writer = csv.writer(w)
        writer.writerow(["file_id", "lang", "lang_confidence"])
        writer.writerows(rows)

    df = pd.DataFrame(rows, columns=["file_id", "lang", "lang_confidence"])
    print(df["lang"].value_counts().head(10).to_string())
    print(f"OK — wrote {len(rows):,} rows to {args.out}")


if __name__ == "__main__":
    main()
