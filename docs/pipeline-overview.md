# Folder Reorg Pipeline — How It Works

Operational companion to `Folder Reorganization Project Plan.md` (the design doc
in `C:\Users\micha\ClaudeAccess\`). The plan is the **what and why**; this doc
is the **how** — what actually runs, in what order, on which machine, and where
the files move at each step.

---

## Goal in one paragraph

Your NAS has ~61 GB / ~16,000 files under `/volume1/Data_Michael/` (personal)
plus a set of `/volume1/360F-*` business shares, most of them named by
whatever app created them (`IMG_1234.pdf`, `Kontoauszug.pdf`,
`scan_0037.jpg`). The pipeline reads the **content** of each file with a
local LLM, groups semantically similar files into folders
(`UBS Account Statements/`, `Singtel Bills/`, …), and renames them per your
convention (`UBS 2403 Account Statement V0-01.pdf`). The original files are
**never modified**. A second, clean tree is produced in parallel, and only
after you've reviewed and approved it does anything get copied back to the NAS.

## Collections

The pipeline organises work by "collection" — a group of source folders that
share a rsync-in root and a restructured-output subpath:

| Collection | NAS source                    | NAS destination                                  |
|---|---|---|
| **Personal** | `/volume1/Data_Michael/*`   | `/volume1/Data_Michael_restructured/Personal/…` |
| **360F**     | `/volume1/360F-*`           | `/volume1/Data_Michael_restructured/360F/…`     |

The wizard (`./run.py`) lists subsets from both collections in one menu and
auto-resolves the destination. The `360F-` prefix is stripped from the local
slug, so the NAS folder `360F-A-Admin` lands at `Data_Michael_restructured/360F/A-Admin/`.
Adding a new collection means editing the `COLLECTIONS` list in `run.py`.

---

## Topology — two machines

```
   ┌────────────────────────────────────┐          ┌──────────────────────────────┐
   │  aizh  (Ubuntu 24.04, RTX 3090)    │          │  mgzh11  (Synology NAS)       │
   │  ─────────────────────────────     │          │  ────────────────────         │
   │                                    │          │                               │
   │  Ollama (:11434) — LLM server      │          │  /volume1/Data_Michael/       │
   │    · qwen2.5:14b-instruct-q4_K_M   │          │      ← SOURCE (read-only)     │
   │    · bge-m3 (embeddings)           │          │                               │
   │                                    │          │                               │
   │  /home/michael.gerber/folderReorg/ │ ◄── rsync ──  /volume1/Data_Michael/     │
   │    · source_local/  (NAS mirror)   │          │                               │
   │    · target_local/  (new tree)     │ ── rsync ──►  /volume1/Data_Michael_     │
   │    · data/          (csvs, text)   │          │           restructured/       │
   │    · src/           (code)         │          │      ← TARGET (written at end)│
   │                                    │          │                               │
   └────────────────────────────────────┘          └──────────────────────────────┘
```

- **Source** (`/volume1/Data_Michael/` on `mgzh11`) is read-only for the entire
  duration of the project. Nothing writes to it. Ever.
- **Working copy** (`source_local/` on `aizh`) is an rsync'd mirror of the
  source, used so the pipeline reads from fast local SSD instead of the network.
- **Target** (`target_local/` on `aizh`, then `/volume1/Data_Michael_restructured/`
  on `mgzh11`) is the new, clean tree the pipeline builds.

**We deliberately use a "copy not rename" architecture.** The safety guarantee
is that the source tree is literally untouched — if anything goes wrong at any
phase, you can throw away everything we produced and you're exactly where you
started. See `Folder Reorganization Project Plan.md` §1.3 for the rationale.

---

## Pipeline phases — what runs, what it produces

All artifacts land in `/home/michael.gerber/folderReorg/data/` on `aizh`.
Each phase is a separate Python module under `src/`, and can be re-run
independently: later phases read only the CSVs / files written by earlier
phases, not process memory.

### Phase 0 — Freeze + manifest

Script: `src/phase0_manifest.py`

- Walks the working source mirror, computes SHA-256 for every file.
- Output: `data/source_manifest.csv` (columns: `rel_path, size_bytes, sha256`).
- Purpose: the manifest is the ground-truth fingerprint of the source, so
  Phase 6 can prove every file in the new tree is byte-identical to the
  file it was copied from.

### Phase 1 — Inventory + text extraction + language detection

Three scripts, usually run in sequence:

| Script | Purpose | Output |
|---|---|---|
| `phase1_inventory.py` | Walk source, one row per file | `data/inventory.csv` |
| `phase1_extract.py`   | Extract first ~4,000 chars of text per file (PDF, DOCX, XLSX, TXT) | `data/extraction_results.csv` + `data/extracted_text/<file_id>.txt` per OK file |
| `phase1_lang_detect.py` | Detect language per OK file (EN/DE/FR/IT/…) using `lingua` | `data/inventory_lang.csv` |

**Status values** written by `phase1_extract.py`:

- `ok` — text was extracted, goes through the LLM pipeline.
- `quarantine_image_only` — PDF has no extractable text (scanned document).
  Still renamed, parent-folder rule, `(image)` marker.
- `quarantine_password` — encrypted PDF. Still renamed, parent-folder rule,
  `(password)` marker. User unlocks manually later.
- `quarantine_no_extractor` — images, videos, archives, `.doc`, `.xls`
  (old binary formats), etc. Still renamed, parent-folder rule.
- `quarantine_corrupt` / `quarantine_too_large` — logged but **not renamed**;
  we can't verify content integrity, so we leave them alone.

### Phase 2 — Embeddings + clustering

| Script | Purpose | Output |
|---|---|---|
| `phase2_embed.py`   | Embed each OK text into a 1024-dim vector with `bge-m3` (multilingual) on the GPU | `data/embeddings.npy` + `data/embeddings_index.csv` |
| `phase2_cluster.py` | HDBSCAN on those vectors → semantic groups | `data/cluster_assignments.csv` (columns: `file_id, cluster_id`) |

`cluster_id = -1` means HDBSCAN couldn't confidently assign the file to any
cluster. Phase 3 handles these via the **preserved parent folder** fallback
(walk up the source path and use the nearest `[A-Z][A-Z0-9]* - Name` ancestor).

### Phase 3 — LLM classification (the expensive part)

Script: `src/phase3_classify.py`

**Pass A — cluster naming** (once per cluster, a few hundred LLM calls max):

> The LLM gets 8 sample texts from a cluster and is asked: "What kind of
> documents are these? Propose a folder name, a 1-4 letter shortcut, and a
> rationale." Produces `data/cluster_catalog.csv`.

**Pass B — per-file naming** (one LLM call per OK text file):

> The LLM gets the cluster's folder name and the file's extracted text, and is
> asked for a descriptive name (3-8 words, English, no year, no version) and
> a content date (for the YYMM cross-check rule).

**Non-text branch** (no LLM calls — just mechanical):

> Each non-text file (image, video, archive, image-only PDF, password PDF,
> unsupported binary) inherits the target folder name chosen for its text
> neighbours in the same source directory. If no text neighbours exist, we
> walk up to the nearest shortcut-prefixed ancestor folder and use that.

**Already-conforms skip** (§11.6 optimisation):

> Files whose existing filename matches the naming convention regex
> (`FD 2508 Spendenbescheinigung V0-01.docx`) bypass the LLM entirely — they
> pass through unchanged, preserving their original parent folder path.
> On F-Finance this saved ~91 LLM calls.

**Output:** `data/rename_plan.csv` — one row per file with:

| Column | Meaning |
|---|---|
| `current_path` | absolute path in the working source mirror |
| `proposed_parent` | target folder under `target_local/` (e.g. `PT - Payment Transactions`) |
| `proposed_name` | new filename (e.g. `PT 2508 International Payment V0-01.pdf`) |
| `yymm_source` | `mtime` (default) or `content` (if the LLM found a date in the text that differs from mtime by >6 months) |
| `cluster_id` | HDBSCAN cluster (or -1 for noise / non-text) |
| `confidence` | `high` / `medium` — used by the review UI to filter |
| `kind` | `text`, `non-text`, `text-pass-through`, `non-text-pass-through` |
| `source_lang` | detected language (`en`, `de`, `fr`, …) |
| `original_name`, `original_parent` | audit trail — nothing is lost to translation |
| `decision` | pre-filled `approve`; you change to `edit` / `skip` in Phase 4 |

### Phase 4 — Human review (your hands on the keyboard)

Two options, both read `data/rename_plan.csv`:

1. **Streamlit app** — `review_ui/review_ui.py`. Runs on your laptop against
   `rename_plan.csv` (mounted from `aizh` or copied locally). Filter by
   confidence, edit cells inline, set each row's `decision`, click
   **Save approved plan** → writes `rename_plan_approved.csv`.
2. **Excel** — open `rename_plan.csv` in Excel, add/edit the `decision` column,
   save as `rename_plan_approved.csv`. Identical effect, zero setup.

**What you're reviewing**:

- **All cluster names** — there are only a few hundred, one bad cluster name
  contaminates every file under it.
- **All `medium` confidence rows** — these are text files with <500 chars
  extracted or files that fell back to the preserved parent.
- **Any row where the LLM kept a German common noun** — the prompt is
  explicit but qwen2.5:14b occasionally slips.

The rest (high-confidence + pass-throughs) can be skim-reviewed.

### Phase 5 — Execute (copy, not rename)

Script: `src/phase5_execute.py`

For every row in `rename_plan_approved.csv` where `decision == "approve"`:

1. Create the target folder (`target_local/<proposed_parent>/`) if needed.
2. `shutil.copy2(source, target)` — preserves mtime and permissions.
3. Compute SHA-256 of both source and target; if they differ, abort that row
   and log the error.
4. If the target filename already exists in the target folder (RULE 4
   collision), bump the minor version (`V0-01` → `V0-02` → …).
5. Write the outcome to `data/execution_log.csv`.

**Still local to `aizh`.** Nothing has touched `mgzh11` yet other than the
initial rsync-in at Phase 1.

**Resumable**: if Phase 5 crashes or you kill it, re-running skips any row
already marked `ok` in the log. The executor is idempotent.

### Phase 6 — Verify

Script: `src/phase6_verify.py`

Four checks:

1. **Count check** — `approved_rows == ok_log_rows == files_in_target_tree`.
2. **Hash match** — every file in the new tree has the same SHA-256 as its
   source in `source_manifest.csv` (Phase 0).
3. **Source untouched** — re-hash a random 5 % sample of the source and
   confirm every hash still matches the Phase 0 manifest. The source tree
   MUST be byte-identical to its pre-Phase-0 state.
4. **Convention lint** — every filename in the new tree matches the
   naming-convention regex. Non-conforming names written to
   `data/non_conforming.csv`.

Only after all four pass is the target tree fit to ship back to the NAS.

### Phase 7 — Copy back to the NAS

**Not in the original plan.** The plan assumed the executor container ran on
the NAS itself, so "Phase 5" *was* the NAS-side write. We moved everything
onto `aizh` (no Docker on Synology DSM), so the copy-back is a separate step:

```bash
# On aizh, AFTER Phase 6 passes:
rsync -av --rsync-path=/usr/bin/rsync \
    /home/michael.gerber/folderReorg/target_local/F-Finance/ \
    mgzh11:/volume1/Data_Michael_restructured/F-Finance/
```

Properties:

- Target path on the NAS is **new** — we write to `Data_Michael_restructured/`,
  NOT to `Data_Michael/`. The original stays pristine until you explicitly
  decide to retire it.
- `rsync -av --checksum` (optional) can be used for the copy-back too, which
  re-verifies by hash at the destination.
- Disk headroom on the NAS: we have 1.6 TB free, full archive is 61 GB, so
  the two-tree coexistence period costs ~4 % of free space. Trivial.

**Order of finality on the NAS**:

1. After Phase 6 passes on a subset (e.g. `F-Finance/`), rsync that subset to
   `mgzh11:/volume1/Data_Michael_restructured/F-Finance/`.
2. Browse it in DSM / File Station, look around, confirm it's right.
3. Repeat for the next subset (e.g. `C - Companies/`, `G - Gesundheit Health/`, …).
4. Once every subset is present under `Data_Michael_restructured/` AND you're
   happy, *only then* decide what to do with the original `Data_Michael/` —
   archive it, delete it, or leave it indefinitely. That is a **manual,
   deliberate** action, not part of the pipeline.

---

## Milestones (M1–M6) — the order to run things the first time

These are checkpoints from the project plan §16. Each milestone runs a
progressively larger slice of the pipeline so you catch issues early and
cheaply, rather than discovering them 12 hours into a full run.

| Milestone | What it means | Scripts | Subset |
|---|---|---|---|
| **M1** | Freeze + baseline manifest | `phase0_manifest.py` | Chosen subset (e.g. `F-Finance/`) |
| **M2** | Inventory + text extraction + language detection | `phase1_inventory.py` → `phase1_extract.py` → `phase1_lang_detect.py` | Same subset |
| **M3** | Embeddings + clustering | `phase2_embed.py` → `phase2_cluster.py` | Same subset |
| **M4** | LLM cluster naming + per-file naming | `phase3_classify.py` | Same subset (optionally `--limit N` for a smoke test) |
| **M5** | Review + execute + verify, end-to-end on the subset | Phase 4 (review UI) → `phase5_execute.py` → `phase6_verify.py` → rsync back to NAS | Same subset |
| **M6** | Full run on all 16 k files of `Data_Michael/` | All phases | Entire archive |

**Golden rule**: do not attempt M6 until M5 has run cleanly on a representative
subset and you're happy with the quality. The subset is the rehearsal; the
full run is the performance.

### Where we are right now

- M1 ✅ passed on `F-Finance/` (1,960 files manifested, 2.6 s).
- M2 ✅ passed (1,141 text files + 812 non-text/quarantine files).
- M3 ✅ passed (73 clusters after tuning, 17 % noise).
- **M4 smoke test** (`--limit 50`) ✅ passed after four quality patches:
  tighter translation prompt, `already_conforms` skip, preserved-parent
  fallback for noise files, alphanumeric shortcut regex.
- **M4 full pass** — pending your green light. Estimated 20–25 min.
- M5 — pending M4.
- M6 — pending clean M5 on `F-Finance/`.

---

## File locations — single-pane-of-glass

On `aizh`:

```
/home/michael.gerber/folderReorg/
├── src/
│   ├── phase0_manifest.py        ← M1
│   ├── phase1_inventory.py       ← M2
│   ├── phase1_extract.py         ← M2
│   ├── phase1_lang_detect.py     ← M2
│   ├── phase2_embed.py           ← M3
│   ├── phase2_cluster.py         ← M3
│   ├── phase3_classify.py        ← M4
│   ├── phase5_execute.py         ← M5 part 2
│   ├── phase6_verify.py          ← M5 part 3
│   ├── llm.py                    ← Ollama wrapper
│   ├── naming.py                 ← convention regex, RULE 8 cleanups
│   ├── exclusions.py             ← skip Archive, _Archive, .git, caches, @eaDir
│   ├── depth_policy.py           ← Mode C preserve-vs-cluster
│   ├── shortcuts.py              ← 1-4 letter shortcut collision resolver
│   └── translate_name.py         ← cached LLM translation for non-English fragments
│
├── review_ui/review_ui.py        ← M5 part 1 (runs on your laptop)
├── data/                         ← all pipeline artifacts
│   ├── source_manifest.csv
│   ├── inventory.csv
│   ├── extraction_results.csv
│   ├── extracted_text/<file_id>.txt
│   ├── inventory_lang.csv
│   ├── embeddings.npy
│   ├── embeddings_index.csv
│   ├── cluster_assignments.csv
│   ├── cluster_catalog.csv
│   ├── rename_plan.csv
│   ├── rename_plan_approved.csv  ← produced by Phase 4
│   └── execution_log.csv
├── source_local/F-Finance/       ← rsync mirror of NAS (M1 input)
├── target_local/F-Finance/       ← new clean tree (M5 output, pre-NAS)
├── logs/                         ← rsync + model-pull logs
├── pyproject.toml
├── README.md
└── run.sh                        ← ./run.sh 0 | 1 | 2 | 3 | 5 | 6 | all-up-to-3 | all
```

On `mgzh11`:

```
/volume1/
├── Data_Michael/                          ← personal source (untouched)
├── 360F-A-Admin/                          ← 360F source shares (untouched)
├── 360F-B-SCB/
├── 360F-F-Finance/
├── … other 360F-* shared folders
└── Data_Michael_restructured/             ← written AFTER Phase 6 passes
    ├── F-Finance/                         ← legacy (pre-Collections run)
    ├── Personal/
    │   ├── F-Finance/                     ← future Personal runs
    │   ├── C-Companies/
    │   └── …
    └── 360F/
        ├── A-Admin/                       ← 360F runs (slug stripped of "360F-")
        ├── F-Finance/
        └── …
```

---

## When does a file actually move onto the NAS? (short answer)

Two moments — both **AFTER** you've approved the plan and verification has passed.

1. **NAS write only happens in Phase 7** (the post-Phase-6 rsync step).
   Before that, everything the pipeline produces lives on `aizh`.
2. You run Phase 7 **per subset**. After M5 passes on `F-Finance/`, rsync the
   `target_local/F-Finance/` up to `Data_Michael_restructured/F-Finance/`.
   Then move to the next subset (e.g. `P - Personal/`). Never all at once.
3. **The original `/volume1/Data_Michael/` is never written to.** The new tree
   lives at a different path. You keep both indefinitely until you're
   confident enough to retire the original — and retiring it is a deliberate
   manual action, never done by the pipeline.
