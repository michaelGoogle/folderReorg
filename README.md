# Folder Reorg Pipeline + Knowledge Base

End-to-end content-aware reorganization of an ~60 GB Synology NAS archive,
plus a private RAG knowledge base over the restructured tree. Runs on a
single Ubuntu host with an RTX 3090; uses Ollama (qwen2.5:14b + bge-m3)
locally — nothing leaves the box.

## What it does

1. **Restructures** any subset of the NAS source tree into a clean,
   convention-named, semantically-grouped destination tree, with full
   safety guarantees (sha256-verified copies, source untouched, every
   stage resumable).
2. **Indexes** the restructured tree into a per-collection Qdrant
   vector store, with daily delta scans and full-text + Tesseract OCR
   for image-only PDFs.
3. **Serves** a Streamlit chat UI over the index, reachable via
   Cloudflare Tunnel from anywhere with email-PIN auth.

Two physically-separate stacks (Personal vs 360F business documents)
share the same code but have independent Qdrant containers, ports,
collections, systemd timers, and chat URLs.

## Topology

```
              laptop / phone (browser)
                    │   https://private.vitalus.net   (Personal)
                    │   https://360f.vitalus.net      (360F)
                    ▼
   ┌─────────────────────────────────────────────────────────────┐
   │   Cloudflare Edge   (Access policy: email PIN)              │
   └─────────────────────────────────────────────────────────────┘
                    │   cloudflared outbound tunnel
                    ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  aizh  (Ubuntu 24.04 · RTX 3090)                             │
   │                                                              │
   │  Streamlit (127.0.0.1:8502 personal, :8503 360f)             │
   │  Ollama (:11434)  · qwen2.5:14b  · bge-m3                    │
   │  Qdrant (127.0.0.1:6333 personal, :6433 360f)                │
   │  Pipeline + indexer + wizard / batch (run.py / kb.py)        │
   │                                                              │
   │     ▲ rsync     │ rsync                                      │
   │     │ ←from     │ to→                                        │
   └─────│───────────│────────────────────────────────────────────┘
         │           │
   ┌─────│───────────│────────────────────────────────────────────┐
   │  mgzh11  (Synology NAS)                                       │
   │     /volume1/Data_Michael/         ← SOURCE (read-only)       │
   │     /volume1/360F-*                ← SOURCE (read-only)       │
   │     /volume1/Data_Michael_restructured/{Personal,360F}/<slug>/│
   │                                    ← DESTINATION              │
   └───────────────────────────────────────────────────────────────┘
```

## Entrypoints

| Script | What it does |
|---|---|
| `./run.py` | Interactive wizard: walks one subset through 11 stages (manifest → restructure → KB index). Supports `--auto-run` (unattended), `--batch SPEC` (many subsets), `--resume`, `--source-from-mount`, `--skip-restructured`. |
| `./kb.py` | KB CLI: `setup`, `mount` / `umount`, `index` / `reindex`, `query`, `chat`, `status`, `remove --root <ROOT>`. Per-variant via `--variant {personal,360f}`. |
| `./status.py` | Standalone status snapshot: pipeline activity, GPU/Ollama, recent state files, recent KB scans, chat / Qdrant / NAS-mount health. Supports `--watch SEC` for live monitoring. |

## Documentation

| Doc | Audience |
|---|---|
| [`docs/run-on-aizh.md`](docs/run-on-aizh.md) | Operational runbook — how to run subsets, batch overnight, troubleshoot |
| [`docs/knowledge-base.md`](docs/knowledge-base.md) | KB / chat UI specifics — variants, indexing, removal, security |
| [`docs/pipeline-overview.md`](docs/pipeline-overview.md) | Pipeline architecture — what each phase does and why |

## Quick start

Once-only setup (see `docs/knowledge-base.md` for the full one-time setup):

```bash
ssh aizh
cd /home/michael.gerber/folderReorg
source .venv/bin/activate
./kb.py mount                     # mount NAS read-only at ~/nas
.venv/bin/python kb.py setup      # both Qdrant containers + collections
```

Day-to-day:

```bash
./run.py                                                    # walk one subset interactively
./run.py --batch all --source-from-mount                    # do everything overnight
./run.py --batch personal --skip-restructured --source-from-mount   # only fresh Personal subsets
./status.py                                                 # snapshot of what's happening right now
.venv/bin/python kb.py --variant 360f status                # KB collection size + last-scan rollup
.venv/bin/python kb.py --variant 360f remove --root F-Finance   # forget a subset from the KB
```

Public chat (after Cloudflare Tunnel + Access setup):

- https://private.vitalus.net — Personal
- https://360f.vitalus.net — 360F

## Models

- **LLM**: `qwen2.5:14b-instruct-q4_K_M` via Ollama — cluster + per-file
  naming (Phase 3) and chat answers. Loaded on demand,
  `OLLAMA_KEEP_ALIVE=5m` keeps it warm between requests.
- **Embeddings**: `BAAI/bge-m3` (multilingual, 1024-dim) via
  sentence-transformers — Phase 2 + KB indexer.
- **OCR**: Tesseract (`deu+eng`) for image-only PDFs and standalone
  images. Toggle with `KB_OCR_ENABLED=0`.

## Layout (after first run)

```
folderReorg/
  run.py              kb.py              status.py
  src/                pipeline modules (Phase 0-7)
  kb/                 KB modules (extract, indexer, query, scheduled, chat)
  chat_ui/            Streamlit chat UI (one process per variant)
  review_ui/          Streamlit Phase 4 review UI (skipped under --auto-run)
  docker/qdrant/      docker-compose for the two Qdrant containers
  systemd/            user-timer units (one per variant, fire ~02:00)
  docs/               operator + architecture documentation
  data/runs/<col>/    per-(collection,slug) wizard state (gitignored)
  source_local/<col>/ NAS source mirrors (gitignored, ~60 GB per subset)
  target_local/<col>/ restructured output mirrors (gitignored)
  logs/<col>/         per-collection Phase 3 logs (gitignored)
  qdrant_data/        Qdrant container volumes (gitignored)
  kb/data/<variant>/  per-variant scan summaries (gitignored)
```

## Safety guarantees

- The NAS source tree is **never modified** at any phase.
- Phase 6 verifies destination byte-for-byte (sha256) before Phase 7
  rsyncs to the NAS.
- Per-collection namespacing of all scratch dirs prevents
  cross-collection collisions (e.g. Personal `F-Finance` vs 360F
  `F-Finance` are fully independent).
- Every stage saves resumable state; Ctrl-C at any prompt is safe.
- Nothing leaves `aizh` — LLM is local, embeddings are local, Qdrant
  is local, Cloudflare Tunnel is outbound-only on the loopback
  interface.

## Status

Production. Restructured Personal subsets so far include
`C-Companies`, `F-Finance`, `P-Personal`. 360F restructured includes
`A-Admin`, `B-SCB`. KB stacks both running with daily delta scans;
chat UI live at the two Cloudflare-fronted hostnames.
