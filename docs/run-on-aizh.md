# Runbook — Operating the Folder Reorg Pipeline on `aizh`

The operational manual. How to take any subset on the NAS, run it through the
11-stage pipeline, and end up with a restructured, indexed, searchable copy
on the NAS plus the chat UI — interactively, in batch overnight, or anywhere
in between.

Companion docs:
- [`pipeline-overview.md`](./pipeline-overview.md) — architecture (what each
  phase does and why)
- [`knowledge-base.md`](./knowledge-base.md) — RAG / chat UI specifics
- [`unattended-runs.md`](./unattended-runs.md) — running long batches in the
  background so they survive SSH disconnects (tmux / nohup / systemd-run)
- [`setup.md`](./setup.md) — first-time host setup from a fresh Ubuntu install

---

## Quick reference

| You want to… | Command |
|---|---|
| Walk one subset through every stage interactively | `./run.py` |
| Run a specific subset unattended (with auto-defaults) | `./run.py --subset C-Companies --nas-name "C - Companies" --auto-run --source-from-mount` |
| Run many subsets back-to-back overnight | `./run.py --batch 1,4,7,10,16-25 --source-from-mount` |
| Run every Personal subset (re-doing already-done ones) | `./run.py --batch personal --source-from-mount` |
| Run every 360F subset (re-doing already-done ones) | `./run.py --batch 360f --source-from-mount` |
| Run every subset, skipping already-restructured | `./run.py --batch all --skip-restructured --source-from-mount` |
| Run every subset, restructured ones get re-done | `./run.py --batch all --source-from-mount` |
| Resume a partial run | `./run.py --resume` |
| Mount the NAS (idempotent) | `./kb.py mount` |
| Inspect what's indexed | `.venv/bin/python kb.py --variant 360f status` |
| Force a KB reindex | `.venv/bin/python kb.py --variant 360f reindex` |
| Remove one subset from the KB | `.venv/bin/python kb.py --variant 360f remove --root F-Finance` |
| Open the chat UI | https://private.vitalus.net (Personal) / https://360f.vitalus.net (360F) |

> **Important — invoke via the venv.** All commands assume the venv Python.
> Either prefix with `.venv/bin/python …` or `source .venv/bin/activate` once
> per shell. Bare `./kb.py` uses the system Python (no `tqdm` etc.) and
> errors out with `ModuleNotFoundError`.

---

## Mental model in three paragraphs

1. **Two collections, fully isolated.** "Personal" and "360F" are physically
   separate from each other at every layer — separate NAS source roots,
   separate NAS destination subtrees, separate Qdrant containers, separate
   Streamlit chat ports, separate Cloudflare hostnames, separate systemd
   timers. Picking the right collection is the *only* decision you ever
   make about cross-cutting plumbing.

2. **The wizard runs one subset at a time, the batch runs many.** Both
   share the same 11-stage pipeline. The wizard prompts before each stage
   (or auto-defaults with `--auto-run`); the batch loops the wizard over
   a list of subsets unattended. Every run persists state to
   `data/runs/<collection>/<slug>.state.json`, so any interruption is
   recoverable with `--resume` or by re-invoking the same `--subset`.

3. **The source on the NAS is never touched.** Stages 1–7 build a clean
   parallel tree under `target_local/<collection>/<slug>/` on `aizh`,
   verify it byte-for-byte against the source, and only then rsync it to
   `Data_Michael_restructured/<collection>/<slug>/` on the NAS. Stage 11
   wakes up the KB indexer for the matching variant so the new content
   appears in chat search within minutes. The original
   `/volume1/Data_Michael/<...>` and `/volume1/360F-<...>` folders are
   read-only for the project's entire lifetime.

---

## Collections — picking the right one

| Collection | NAS source root | NAS destination | KB variant | Chat URL |
|---|---|---|---|---|
| **Personal** | `/volume1/Data_Michael/*` | `/volume1/Data_Michael_restructured/Personal/<slug>/` | `personal` (Qdrant :6333) | https://private.vitalus.net |
| **360F**     | `/volume1/360F-*` | `/volume1/Data_Michael_restructured/360F/<slug>/` | `360f` (Qdrant :6433) | https://360f.vitalus.net |

The wizard auto-detects the collection from the NAS folder name (`360F-*`
matches the 360F glob; everything else falls through to Personal). Override
with `--collection {Personal,360F}` if you ever need to be explicit.

The slug derivation rules:
- `F - Finance` → `F-Finance` (whitespace and `-` collapsed)
- `360F-A-Admin` → `A-Admin` (the `360F-` prefix is stripped)

The slug is the local-side identifier. Because Personal and 360F are kept in
separate `target_local/<collection>/`, `data/runs/<collection>/`,
`source_local/<collection>/`, and `logs/<collection>/` subtrees, slug
collisions across collections (e.g. Personal `F-Finance` vs 360F
`F-Finance`) are no longer a problem — they have independent state and
scratch space.

---

## Pre-flight (do once per session)

```bash
ssh aizh
cd /home/michael.gerber/folderReorg
source .venv/bin/activate

# Verify the prerequisites the wizard checks at Stage 0:
ollama list                       # expect qwen2.5:14b-instruct-q4_K_M + bge-m3
./kb.py mount                     # idempotent; mounts NAS at ~/nas if not already
```

If any model is missing:

```bash
ollama pull qwen2.5:14b-instruct-q4_K_M
ollama pull bge-m3
```

If the NAS mount won't come up, see *Troubleshooting* at the bottom.

---

## Running one subset (interactive wizard)

```bash
./run.py
```

The wizard:
1. Lists every subset on the NAS with menu numbers — restructured ones are
   marked `(restructured, at YYYY-MM-DD HH:MM)` so you can see at a glance
   what's already done.
2. Asks you to pick one by number (or type the exact NAS name).
3. Walks the 11 stages, asking before each one whether to **r**un, **s**kip,
   go **b**ack, **j**ump, **l**ist progress, or **q**uit and save.
4. Saves state after every successful stage, so you can quit and resume
   later with `./run.py --resume`.

If you already know what you want:

```bash
# Personal subset, named exactly as it appears on the NAS
./run.py --subset C-Companies --nas-name "C - Companies"

# 360F subset (collection auto-detected from the 360F-* prefix)
./run.py --subset A-Admin --nas-name "360F-A-Admin"

# Be explicit about the collection (overrides auto-detection)
./run.py --subset F-Finance --nas-name "360F-F-Finance" --collection 360F

# Resume the most recently modified state file across all collections
./run.py --resume
```

### `--source-from-mount` (recommended for big subsets)

By default Stage 1 rsyncs the source from NAS to `source_local/<collection>/<slug>/`
(~60 GB per subset). `--source-from-mount` skips that and reads source files
directly from the SSHFS-mounted NAS at `/home/michael.gerber/nas/`.

| Mode | Disk on aizh | Phase 5 read speed | When to use |
|---|---|---|---|
| Default (rsync-in) | full subset (~60 GB) | local SSD (~500 MB/s) | When you'll re-run Phase 5 several times |
| `--source-from-mount` | nothing | over SSHFS (~28 MB/s) | One-shot runs, batch mode, low-disk situations |

Net effect on total runtime is small (~30 min more for a big subset's Phase 5)
because Phase 3 (LLM) is the long pole regardless.

### `--auto-run` (unattended)

Every prompt auto-defaults after 60 s (configurable via `--auto-run-timeout`),
so the wizard works through every stage by itself. **Phase 4 (Streamlit
review) is skipped** — Phase 5 uses the un-edited `rename_plan.csv` with
every row pre-set to `decision=approve`. Press Ctrl-C to abort, or any key
+ Enter at a prompt to override that one auto-default.

```bash
./run.py --subset C-Companies --nas-name "C - Companies" \
    --auto-run --source-from-mount
```

---

## Running many subsets back-to-back (`--batch`)

For overnight or long-running campaigns. Implies `--auto-run` automatically.
**Already-restructured subsets are RE-DONE by default** — the wizard picks
up where the prior run left off via the per-subset state file (completed
stages auto-skip, only changed or never-run stages execute again). Pass
`--skip-restructured` to leave already-restructured subsets alone.

> **⚠ Run batches detached from SSH.** A bare `./run.py --batch …` over
> SSH dies on disconnect (laptop sleep, Wi-Fi switch, idle timeout).
> Before kicking off any batch, see [`unattended-runs.md`](./unattended-runs.md).
> The short form: wrap in `tmux new -s reorg` first, then `Ctrl-B  D`
> to detach.

### `SPEC` syntax

| SPEC | Selects |
|---|---|
| `all` | every subset under Personal + 360F |
| `personal` | every subset under Personal |
| `360f` | every subset under 360F |
| `1,4,7,10` | menu numbers (1-based, exact match to interactive picker) |
| `16-25` | inclusive range |
| `1,4,7-10,16-20` | combos |

### Examples

```bash
# Every subset (Personal + 360F), restructured ones get re-done
./run.py --batch all --source-from-mount

# Same, but skip already-restructured subsets (only fresh ones run)
./run.py --batch all --skip-restructured --source-from-mount

# Specific menu numbers from the interactive picker
./run.py --batch 1,4,7,8,10 --source-from-mount

# Only the never-restructured 360F subsets
./run.py --batch 360f --skip-restructured --source-from-mount

# Skip the 10-second start countdown
./run.py --batch 16-20 --batch-countdown 0 --source-from-mount
```

### What batch mode does

1. Discovers every subset on the NAS, prints a numbered plan. By default
   already-restructured entries are tagged `(re-do; previously
   restructured at YYYY-MM-DD HH:MM)`. With `--skip-restructured` they
   are tagged `WILL SKIP` instead.
2. **10-second countdown** before starting (Ctrl-C to abort). Override
   with `--batch-countdown N` (0 to skip).
3. Runs each subset with the wizard's normal stage loop, but with no menu
   (always `r`) and per-stage failures abandoning *that* subset and
   moving to the next (instead of bringing down the whole batch).
4. Per-subset state is saved to `data/runs/<collection>/<slug>.state.json`
   as usual — any subset that crashed mid-way can be resumed individually
   with `./run.py --subset <slug> --collection <coll> --resume` later.
5. **Final summary table** showing per-subset status (`ok` / `fail` /
   `skipped-restructured` / `interrupted` / `context-error`) plus
   elapsed wall-clock time.

### Estimating runtime

| Subset size | Approximate wall-clock | Bottleneck |
|---|---|---|
| Small (<500 files) | 10–25 min | Phase 3 (LLM) |
| Medium (500–2000) | 30–60 min | Phase 3 + Phase 5 (copy) |
| Large (2000–10000) | 1–3 hr | Phase 3 + OCR for image-only PDFs |
| Huge (>10000) | 3–8 hr | Phase 3, OCR, plus Phase 5 sha256 verification |

A `--batch all` run across all 35 subsets is typically 20–50 hours wall
clock. **Test with 2–3 subsets first** before committing to a full run.

---

## Per-stage breakdown

The wizard prints these in order. Each stage is also a standalone Python
module under `src/` that you can re-run independently if needed.

| # | Stage | What it does | Typical time |
|---|---|---|---|
| 0 | Pre-flight | Verifies Ollama models, venv, NAS SSH, destination share | seconds |
| 1 | Mirror from NAS | rsync (or skip with `--source-from-mount`) | minutes (or 0) |
| 2 | Reset working data | Clears `data/*.csv`, `data/*.npy`, `data/extracted_text/` | seconds |
| 3 | Phase 0 — manifest | sha256 baseline of every source file | seconds–minutes |
| 4 | Phase 1 — inventory + extract + lang | Tika/PyMuPDF text extract, lingua language detect | 1–10 min |
| 5 | Phase 2 — embed + cluster | bge-m3 on GPU, then HDBSCAN | 1–5 min |
| 6 | Phase 3 — LLM classification | qwen2.5:14b cluster naming + per-file naming | 15–90 min |
| 7 | Phase 4 — Streamlit review | UI on `:8501`. **Skipped under `--auto-run`** | manual |
| 8 | Phase 5 — execute | Copy → `target_local/<col>/<slug>/` with sha256 verify | 1–20 min |
| 9 | Phase 6 — verify | 4 checks (counts, hashes, source untouched, naming lint) | seconds |
| 10 | Phase 7 — rsync to NAS | Push to `Data_Michael_restructured/<col>/<slug>/` | 1–10 min (network-bound) |
| 11 | Phase 8 — KB index | Auto-fires `kb.scheduled` for the matching variant | 1–60 min depending on size |

If Stage 9 (Phase 6 verify) fails, the wizard refuses to advance. **Do
not skip a failed verify** — it means something is wrong with the local
copy and pushing it to the NAS would propagate the corruption.

---

## File layout

Everything is namespaced by collection so Personal and 360F never collide.

```
folderReorg/
  source_local/                  # Phase 1 rsync target (only when NOT --source-from-mount)
    Personal/
      F-Finance/                 # ← from NAS /volume1/Data_Michael/F - Finance/
      C-Companies/
    360F/
      A-Admin/                   # ← from NAS /volume1/360F-A-Admin/
      B-SCB/

  target_local/                  # Phase 5 output: clean restructured tree
    Personal/
      F-Finance/                 # ← rsync'd to NAS /volume1/Data_Michael_restructured/Personal/F-Finance/
    360F/
      A-Admin/                   # ← rsync'd to NAS /volume1/Data_Michael_restructured/360F/A-Admin/

  data/
    runs/                        # Pipeline state (resumable per subset)
      Personal/F-Finance.state.json
      360F/F-Finance.state.json  # ← independent from Personal F-Finance!
    rename_plan.csv              # current subset's plan (overwritten each run)
    execution_log.csv
    source_manifest.csv
    cluster_assignments.csv
    extracted_text/<file_id>.txt # one per file with extracted text

  logs/
    Personal/
      phase3_F-Finance.log       # Phase 3 LLM output, per subset
    360F/
      phase3_A-Admin.log
    chat_personal.log            # Streamlit personal stdout
    chat_360f.log                # Streamlit 360F stdout

  kb/
    data/
      personal/                  # Per-variant KB scratch
        last_scan_F-Finance.json # one per indexed root
        last_scan_C-Companies.json
      360f/
        last_scan_A-Admin.json
        last_scan_B-SCB.json

  qdrant_data/                   # Mounted into both Qdrant containers (separate volumes)
    personal/
    360f/
```

Anything not under one of those collection-namespaced directories is shared
state (the venv, the source code, the embedding-model cache, …).

---

## Cleanup after a successful run

The wizard prints exact `rm -rf` commands at the end. Per subset you can
reclaim the disk used by `source_local/` (only if not `--source-from-mount`)
and `target_local/`:

```bash
# After F-Finance Personal is verified live on the NAS:
rm -rf source_local/Personal/F-Finance target_local/Personal/F-Finance

# Equivalent for the 360F variant:
rm -rf source_local/360F/F-Finance target_local/360F/F-Finance
```

State files (`data/runs/<col>/<slug>.state.json`) are tiny and worth
keeping — they document which stages completed when.

---

## Knowledge base operations

The KB lives in two parallel stacks (Personal + 360F). Stage 11 of the
pipeline auto-triggers a delta scan for the matching variant after each
subset is rsynced to the NAS, so newly-restructured content is searchable
within minutes.

### Status of what's indexed

```bash
.venv/bin/python kb.py --variant 360f status
.venv/bin/python kb.py --variant personal status
```

Returns the collection size, indexed-vector count, and a per-root summary
of the most recent scan (file count, chunks added, timestamp).

### Force an immediate reindex

```bash
.venv/bin/python kb.py --variant 360f reindex
.venv/bin/python kb.py --variant personal reindex
```

Equivalent to what the systemd timer does at 02:00 / 02:15 nightly. Use
this if you've manually edited files on the NAS outside the pipeline and
want them picked up before the next timer firing.

### Check whether a reindex is currently running

```bash
ssh aizh 'pgrep -af "kb\.scheduled" | grep -v grep || echo "(not running)"'
ssh aizh 'ollama ps'                                       # bge-m3 in VRAM = active embedding
ssh aizh 'journalctl --user -u folderreorg-kb-360f.service -f'   # live tail when run by timer
```

When `kb.scheduled` finishes, it writes `kb/data/<variant>/last_scan_<root>.json`.
The mtime of that file tells you when the last scan completed.

### Remove a subset from the KB

If you want the chat to forget a subset (e.g. you re-restructured it under
a different name and want the old chunks gone, or you've decided it
shouldn't be searchable):

```bash
.venv/bin/python kb.py --variant 360f remove --root F-Finance
# Prompts for confirmation, then deletes:
#   · all Qdrant chunks where root='F-Finance' from folderreorg_360f
#   · kb/data/360f/last_scan_F-Finance.json (the per-root summary file)
#
# Add  -y / --yes  to skip the confirmation
# Add  --keep-summary  to keep the last_scan_*.json file (audit trail)
```

What it does NOT touch:
- Source files on the NAS (`/volume1/Data_Michael_restructured/360F/F-Finance/`)
- Pipeline state (`data/runs/360F/F-Finance.state.json`)
- Local scratch (`target_local/360F/F-Finance/`, `source_local/360F/F-Finance/`)

To wipe one subset *everywhere*:

```bash
.venv/bin/python kb.py --variant 360f remove --root F-Finance -y
rm -f data/runs/360F/F-Finance.state.json
rm -rf target_local/360F/F-Finance source_local/360F/F-Finance
rm -f logs/360F/phase3_F-Finance.log
ssh mgzh11 'rm -rf /volume1/Data_Michael_restructured/360F/F-Finance/'
```

---

## Logs and where they live

| Output | Location | Captured beyond terminal? |
|---|---|---|
| Wizard stdout (banners, stage progress) | terminal only | no |
| Phase 3 LLM classification | `logs/<collection>/phase3_<slug>.log` | yes |
| Streamlit chat instances | `logs/chat_personal.log`, `logs/chat_360f.log` | yes |
| Stage 11 KB scan (interactive run) | terminal only | no |
| Stage 11 KB scan (systemd timer) | journald | `journalctl --user -u folderreorg-kb-{personal,360f}.service` |
| **Per-root indexer summary** (file count, errors, chunks added) | `kb/data/<variant>/last_scan_<root>.json` | **yes** — best post-mortem artifact |
| MuPDF errors during indexing | tagged with filename, written to terminal/journald | not separately logged |

The `last_scan_<root>.json` is the most valuable persistent record after a
KB scan — it includes the full list of files that errored along with the
exception message, machine-parseable.

---

## Slug collisions and the namespacing fix

The same slug can legitimately exist under both collections — the most
common case being `F-Finance` (Personal: `F - Finance`, 360F:
`360F-F-Finance`). Before the namespacing fix, both wrote to
`data/runs/F-Finance.state.json` and `target_local/F-Finance/`, with
nasty silent-overwrite consequences.

**Now resolved:** every scratch path is keyed by `(collection, slug)`:

| What | Old path | New path |
|---|---|---|
| State file | `data/runs/F-Finance.state.json` | `data/runs/{Personal,360F}/F-Finance.state.json` |
| target_local | `target_local/F-Finance/` | `target_local/{Personal,360F}/F-Finance/` |
| source_local | `source_local/F-Finance/` | `source_local/{Personal,360F}/F-Finance/` |
| Phase 3 log | `logs/phase3_F-Finance.log` | `logs/{Personal,360F}/phase3_F-Finance.log` |

`State.load()` falls back to legacy depth-1 files only when their
`collection` field matches the requested collection — so a leftover
`data/runs/F-Finance.state.json` with `"collection": "Personal"` will not
be loaded for a 360F run. The migration runs once at startup and is
idempotent.

If you ever see legacy files at depth-1 (`data/runs/*.state.json` with
no collection subdir), let `./run.py` migrate them on the next invocation
or run the migration manually:

```bash
.venv/bin/python -c "import run; run._migrate_legacy_state_files()"
```

---

## Public access via Cloudflare Tunnel

The chat UIs are reachable from anywhere via:

- https://private.vitalus.net (Personal chat)
- https://360f.vitalus.net (360F chat)

Authentication is handled by **Cloudflare Access** — an email-PIN flow
covering the configured domains (no VPN, no port-forwarding). The chat
processes themselves bind to `127.0.0.1:8502` / `:8503` on `aizh` and
are *not* directly exposed to the LAN or the internet — `cloudflared`
proxies traffic over an outbound tunnel.

LAN access still works for in-house testing:
- http://192.168.1.10:8502 (Personal — requires `sudo ufw allow 8502/tcp`)
- http://192.168.1.10:8503 (360F   — requires `sudo ufw allow 8503/tcp`)

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `./kb.py …` raises `ModuleNotFoundError: No module named 'tqdm'` | Using system Python instead of venv | `.venv/bin/python kb.py …` or `source .venv/bin/activate` first |
| `--batch` returns 0 entries | Picker can't reach NAS over SSH | `ssh mgzh11 ls /volume1/Data_Michael` to confirm SSH; `./kb.py mount` to verify mount |
| Wizard says "stage X failed" but the underlying tool succeeded | A subprocess returned non-zero on stderr noise | Check the per-stage log; rerun the stage |
| Slug clash between Personal & 360F | A legacy depth-1 state file | Run the migration once; future runs use namespaced paths automatically |
| Stage 11 reports OK but `kb status` shows 0 chunks added for that root | NameError / similar in `kb/indexer.py` | `tail kb/data/<variant>/last_scan_<root>.json` to see the error list; re-run after fixing |
| MuPDF spam during KB indexer with no filename | Old `kb/extract.py` (pre-tagging fix) | Update to current version; errors now print as `MuPDF [text] <filename>: <error>` |
| Chat says "no sources found" for content you know is indexed | Filters too narrow, or stale Streamlit cache | Loosen `--language` / `--yymm` filters; restart Streamlit |
| `kb.py status` shows `indexed: 0` but `points: 12000+` | Qdrant builds the HNSW index lazily | Searches still work; HNSW rebuild kicks in within ~5 min idle |
| Chat preview pane crashes on PDFs | Missing `streamlit-pdf` extra | Already handled — falls back to base64 iframe automatically |
| Two chat UIs but only one returns sources | Indexer ran for one variant, not the other | `.venv/bin/python kb.py --variant <other> reindex` |
| `rsync` to NAS exits "Permission denied" mid-stream | NAS user can't run remote rsync without absolute path | Always use `--rsync-path=/usr/bin/rsync` (the wizard does this for you) |
| `rsync` "cannot create directory" on NAS | Top-level shared folder doesn't exist | Create `Data_Michael_restructured` via DSM Control Panel → Shared Folder, once |
| Phase 3 progresses but cluster names look wrong | qwen2.5:14b producing lower-quality JSON | Try `KB_LLM_MODEL=qwen2.5:32b-instruct-q4_K_M ./run.py …` (slower) |
| Phase 6 reports many non-conforming names | Files without extensions, custom Postman collections, etc. | Inspect `data/non_conforming.csv`; usually accept-as-is for non-document content |
| Phase 6 "source untouched" sample fails | Something has changed the source mirror | Don't proceed; rsync again from NAS to refresh `source_local/`, re-run Phase 0 |
| GPU OOM during embedding | bge-m3 + qwen2.5:14b both resident | Wizard auto-pauses the KB indexer at Stage 5; `nvidia-smi` to verify nothing else is competing |
| Cloudflare URL returns "Access blocked" | Email not in the Access policy | Add to the Cloudflare Zero Trust Access policy (Cloudflare dashboard) |
| `loginctl enable-linger` not set | systemd timers stop firing when you log out | `sudo loginctl enable-linger michael.gerber` once |

---

## Parallel-terminal helpers

Useful in another SSH window while the wizard is running:

```bash
# Live Phase 3 progress
tail -f logs/<collection>/phase3_<subset>.log | tr '\r' '\n'

# GPU load (expect bge-m3 OR qwen2.5:14b resident, not both at once)
watch -n2 nvidia-smi

# Which model is in VRAM right now
ollama ps

# KB indexer running?
pgrep -af "kb\.scheduled" | grep -v grep || echo "(not running)"

# Wizard running?
pgrep -af "run\.py" | grep -v grep || echo "(not running)"

# Chat Streamlit instances
pgrep -af "streamlit run chat_ui" | grep -v grep
ss -lntp | grep -E ":8502|:8503"

# Plan size mid-Phase-3
wc -l data/rename_plan.csv

# Last-scan summary for one root (full error list)
.venv/bin/python -c "import json; d=json.load(open('kb/data/360f/last_scan_A-Admin.json')); print(json.dumps(d, indent=2))"
```

---

## What ships, what doesn't

The pipeline never:
- Modifies the source folders on the NAS
- Pushes anything to the NAS before Phase 6 verify passes
- Embeds or sends content outside `aizh` (LLM is local, embeddings are local, chunks are local)
- Auto-deletes `target_local/` or `source_local/` after a successful run (keep them around for inspection; delete manually)

The pipeline always:
- Saves resumable state after every successful stage
- Deletes-and-reinserts existing Qdrant chunks on re-index (no orphans)
- Frees the GPU after each Stage 11 (sets Ollama `keep_alive=0` so VRAM is available for the next subset's embedding)
- Tags MuPDF errors with the filename
- Keeps Personal and 360F isolated at every layer

---

## When something fails mid-batch

The batch loop catches per-subset failures and moves on. After the batch
ends, the summary table tells you which subsets need attention:

```
BATCH SUMMARY
  ✓ [Personal] G - Gesundheit Health        → G-Gesundheit-Health     [ok]
  ✗ [360F    ] 360F-A-Admin                 → A-Admin                 [fail]
  ✓ [360F    ] 360F-B-SCB                   → B-SCB                   [ok]
  ✗ [360F    ] 360F-F-Finance               → F-Finance               [fail]
2 ok · 2 not-ok   elapsed: 4:23:11
```

For each failed subset, look at `data/runs/<collection>/<slug>.state.json`
to see which stage it stopped at, then resume:

```bash
./run.py --subset A-Admin --collection 360F --resume --auto-run --source-from-mount
```

The wizard picks up at the first incomplete stage. If a stage is failing
deterministically (not transient), check the corresponding log
(`logs/<collection>/phase3_<slug>.log` for Phase 3, terminal scrollback
for others).
