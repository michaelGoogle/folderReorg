# Knowledge Base — RAG over the restructured archive

A local, private RAG (retrieval-augmented generation) layer on top of the
restructuring pipeline. Indexes your `Data_Michael_restructured/` tree on
`aizh`, stores chunks + embeddings in a local Qdrant, and lets you ask
natural-language questions via a Streamlit chat UI — reachable from
anywhere via Cloudflare Tunnel.

Companion docs:
- [`run-on-aizh.md`](./run-on-aizh.md) — pipeline runbook (how subsets
  get indexed in the first place)
- [`pipeline-overview.md`](./pipeline-overview.md) — pipeline architecture

---

## Two physically-separate KB stacks

Personal documents and 360F business documents are kept **completely
separate** — different Qdrant containers, different volumes, different
chat URLs, different systemd timers, different Cloudflare hostnames:

| Variant | Indexes | Qdrant | Chat (LAN) | Chat (public) | Systemd timer |
|---|---|---|---|---|---|
| **personal** | `Data_Michael_restructured/Personal/*` | `localhost:6333`, collection `folderreorg_personal` | http://192.168.1.10:8502 | https://private.vitalus.net | `folderreorg-kb-personal.timer` (~02:00) |
| **360f**     | `Data_Michael_restructured/360F/*`     | `localhost:6433`, collection `folderreorg_360f`     | http://192.168.1.10:8503 | https://360f.vitalus.net    | `folderreorg-kb-360f.timer`     (~02:15) |

The two stacks share only:
- The SSHFS mount of the NAS (read-only, at `/home/michael.gerber/nas`)
- The bge-m3 embedding model (cached in `~/.cache/huggingface`)
- The Ollama LLM endpoint (qwen2.5:14b on `localhost:11434`)

Use `--variant {personal,360f}` on every `kb.py` command to pick the stack.

```
              laptop / phone (browser)
                    │   https://private.vitalus.net   (or 360f.vitalus.net)
                    ▼
   ┌─────────────────────────────────────────────────────────────┐
   │   Cloudflare Edge   (Access policy: email PIN)              │
   └─────────────────────────────────────────────────────────────┘
                    │   cloudflared outbound tunnel
                    ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  aizh                                                        │
   │                                                              │
   │  Streamlit (127.0.0.1:8502  personal)    Ollama (:11434)     │
   │  Streamlit (127.0.0.1:8503  360f)            qwen2.5:14b     │
   │      │                                       bge-m3          │
   │      ▼                                       ▲               │
   │  kb.query ──retrieve──► Qdrant ◄──────────indexer            │
   │                          ├─ :6333  personal  (Docker)         │
   │                          └─ :6433  360f      (Docker)         │
   │                          │                                    │
   │                          ▼                                    │
   │                  qdrant_data/{personal,360f}/                 │
   │                  (per-variant Docker volumes, persistent)     │
   │                                                              │
   │  /home/michael.gerber/nas/Data_Michael_restructured/         │
   │      ← SSHFS read-only mount, indexer walks here              │
   └─────────────────────────────────────────────────────────────┘
```

The chat servers bind to `127.0.0.1` only — they're never directly exposed
to the LAN or the internet. `cloudflared` proxies traffic over an
outbound-only tunnel; the Cloudflare Access policy (email PIN) gates who
can reach them.

---

## Components

| File / path | Purpose |
|---|---|
| `kb/config.py` | Per-variant tunables (Qdrant URL, ports, paths, OCR langs, defaults) |
| `kb/extract.py` | Full-text + Tesseract OCR (PDF, DOCX, XLSX, images); tags MuPDF errors with filename |
| `kb/chunk_embed.py` | Sentence-aware chunker + bge-m3 embedder |
| `kb/indexer.py` | Qdrant upsert/delete + delta scan driver + `delete_root()` primitive |
| `kb/query.py` | Retrieval + RAG prompt + Ollama call |
| `kb/scheduled.py` | Entrypoint the systemd timer fires nightly |
| `chat_ui/chat_ui.py` | Streamlit chat UI (one process per variant) |
| `kb.py` | CLI: `setup` / `mount` / `umount` / `index` / `reindex` / `query` / `chat` / `status` / `remove` |
| `docker/qdrant/docker-compose.yml` | Both Qdrant containers (personal:6333, 360f:6433) |
| `systemd/folderreorg-kb-personal.{service,timer}` | Personal nightly delta scan |
| `systemd/folderreorg-kb-360f.{service,timer}` | 360F nightly delta scan |

Per-variant data lives at `kb/data/{personal,360f}/`. Each `last_scan_<root>.json`
file there records the most recent scan summary (file count, chunks added,
errors with full traceback).

---

## One-time setup (from scratch)

```bash
ssh aizh
cd /home/michael.gerber/folderReorg

# --- Sudo bits ---
sudo apt install -y tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng
sudo apt install -y sshfs fuse3
sudo ufw allow 8502/tcp                        # Personal chat (LAN)
sudo ufw allow 8503/tcp                        # 360F chat (LAN)
sudo loginctl enable-linger michael.gerber     # so user-timers fire when you log out

# --- Python deps (in the venv) ---
export PATH="$HOME/.local/bin:$PATH"
source .venv/bin/activate
uv pip install qdrant-client pytesseract pillow streamlit tqdm

# --- Mount the NAS (shared by both KB stacks) ---
./kb.py mount

# --- Bring up BOTH Qdrant containers + create both collections ---
.venv/bin/python kb.py setup                   # ignores --variant; sets up both

# --- First-time full index per variant ---
.venv/bin/python kb.py --variant personal index
.venv/bin/python kb.py --variant 360f     index

# --- Install systemd timers (one per variant) ---
mkdir -p ~/.config/systemd/user
cp systemd/folderreorg-kb-personal.{service,timer} ~/.config/systemd/user/
cp systemd/folderreorg-kb-360f.{service,timer}     ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now folderreorg-kb-personal.timer
systemctl --user enable --now folderreorg-kb-360f.timer
systemctl --user list-timers 'folderreorg-kb-*.timer'

# --- Launch the chat UIs (one per variant) ---
nohup env KB_VARIANT=personal .venv/bin/streamlit run chat_ui/chat_ui.py \
    --server.address 127.0.0.1 --server.port 8502 --server.headless true \
    --browser.gatherUsageStats false > /tmp/streamlit-personal.log 2>&1 &
nohup env KB_VARIANT=360f .venv/bin/streamlit run chat_ui/chat_ui.py \
    --server.address 127.0.0.1 --server.port 8503 --server.headless true \
    --browser.gatherUsageStats false > /tmp/streamlit-360f.log 2>&1 &

# Personal → http://192.168.1.10:8502 + https://private.vitalus.net
# 360F     → http://192.168.1.10:8503 + https://360f.vitalus.net
```

> **Why bind to 127.0.0.1?** Cloudflare Tunnel reaches the chat servers
> over the loopback interface, so binding to `0.0.0.0` would expose them
> on the LAN as well. If you want LAN access in addition, change to
> `--server.address 0.0.0.0`.

### NAS mount details

`./kb.py mount` runs `kb/mount_nas.sh`, which is idempotent (no-op when
already mounted) and uses these SSHFS options:

| Option | Why |
|---|---|
| `ro` | Indexer never writes to the NAS — extra safety |
| `reconnect` | Survives transient network blips |
| `ServerAliveInterval=15`, `ServerAliveCountMax=3` | Fail fast if NAS is gone (≤45 s) |
| `Compression=no` | Avoids CPU bottleneck on already-compressed PDFs |
| `Ciphers=aes128-gcm@openssh.com` | ~30 % faster than default chacha on AES-NI CPUs |
| `cache_timeout=60` | Speeds up repeated directory listings during a scan |
| `IdentityFile=~/.ssh/id_ed25519` | Explicit so it works under systemd (no SSH agent) |

Both systemd services have `ExecStartPre=mount_nas.sh`, so the nightly
timers always ensure the mount before scanning.

To unmount manually: `./kb.py umount`.

---

## Day-to-day use

Every command takes `--variant {personal,360f}` (default `personal`):

```bash
# Status (collection size + per-root last-scan summary)
.venv/bin/python kb.py --variant personal status
.venv/bin/python kb.py --variant 360f     status

# Manual delta scan (what the systemd timer runs)
.venv/bin/python kb.py --variant personal reindex
.venv/bin/python kb.py --variant 360f     reindex

# Initial / full index (for a fresh variant — uses delta logic so it's safe to repeat)
.venv/bin/python kb.py --variant personal index
.venv/bin/python kb.py --variant 360f     index

# One-shot terminal query
.venv/bin/python kb.py --variant personal query "What was my UBS balance in Dec 2023?"
.venv/bin/python kb.py --variant 360f     query "Q4 vendor invoices" --yymm 2412 --language en

# Filters work across both variants
.venv/bin/python kb.py --variant personal query "rental payment" --yymm 2024 --language de
.venv/bin/python kb.py --variant personal query "UBS" --compound FBUBS --top-k 20

# Remove a specific subset from the variant's KB (chunks + last_scan summary)
.venv/bin/python kb.py --variant 360f remove --root F-Finance
.venv/bin/python kb.py --variant 360f remove --root F-Finance -y           # no prompt
.venv/bin/python kb.py --variant 360f remove --root F-Finance --keep-summary
```

> **Always invoke via `.venv/bin/python kb.py …`** — the shebang on
> `kb.py` resolves to the system Python, which lacks `tqdm` /
> `qdrant-client` / etc. and errors out immediately. Or `source
> .venv/bin/activate` once per shell.

---

## Chat UI features

The Streamlit chat (one process per variant) lays out as:

```
┌──────────────────────────────┬──────────────────────────────┐
│  ◀ Sidebar (collapsed)       │                              │
│  · Variant label             │   Chat history               │
│  · Filters: root, language,  │   (scrolls)                  │
│    yymm, compound, top-k     │                              │
│                              │     ┌──────────────────────┐ │
│  Toggle with header chevron  │     │  📑 File preview     │ │
│                              │     │  (sticky, no scroll) │ │
│                              │     │                      │ │
│                              │     │  PDF / image / text  │ │
│                              │     │  inline render       │ │
│                              │     └──────────────────────┘ │
│                              │                              │
│                              │   "Sources (5)"              │
│                              │   filename · [Preview]       │
│                              │             [Download]       │
│                              │             [Expand toggle]  │
│                              ├──────────────────────────────┤
│                              │  Ask about your archive… ▶   │
└──────────────────────────────┴──────────────────────────────┘
```

Per-source row (under each assistant reply):
- **filename** in bold
- **📄 Preview** — opens the file in the right pane (no new tab). PDFs
  render inline via `st.pdf` (with `streamlit[pdf]` extra) or fall back
  to a base64 iframe for files ≤25 MB. Images render natively. Text /
  Markdown / JSON / XML / CSV render as formatted code. Office files
  surface a Download button instead.
- **⬇ Download** — direct browser download
- **Expand toggle** — reveals metadata (compound · yymm · language ·
  score), the rel_path, and an 800-char text excerpt of the matched chunk

Sidebar starts collapsed. Top-k defaults to 5 (5 most relevant *chunks*
returned, which usually ~3-5 distinct files because long PDFs split into
several chunks).

---

## Delta scan — what runs every night

`kb/scheduled.py` is the entrypoint for both timers. It walks every root
discovered under the variant's base (no manual `DEFAULT_ROOTS` list — see
*Auto-discovery* below). For each file:

1. **Fast path 1 — stat match**: if `(mtime, size)` are unchanged from
   what's stored in Qdrant, return `unchanged` with zero I/O beyond the
   stat call.
2. **Fast path 2 — sha256 match**: compute the file's sha256; if it
   matches what's stored, just refresh the stored mtime/size in Qdrant
   and return `unchanged`.
3. **Slow path**: extract text (with OCR if needed), chunk, embed, delete
   any old chunks for this file path, upsert new chunks.

After the per-file pass, files that have disappeared from the tree get
their orphan chunks deleted.

The job is idempotent — running it twice in a row does almost no work the
second time. Output goes to `kb/data/<variant>/last_scan_<root>.json`:

```json
{
  "root": "A-Admin",
  "root_path": "/home/michael.gerber/nas/Data_Michael_restructured/360F/A-Admin",
  "scanned_files": 777,
  "new": 720,
  "updated": 12,
  "unchanged": 0,
  "deleted": 0,
  "skip": 45,
  "chunks_added": 1862,
  "errors": [],
  "scanned_at": "2026-04-26T02:21:46"
}
```

The `errors` list contains one entry per file that couldn't be indexed,
with the underlying exception message — invaluable for post-mortems
(e.g. "JSON payload exceeded Ollama limit", "PDF failed to extract").

---

## Auto-discovery — no config edits when you restructure new subsets

`kb/config.py::discover_roots()` is called on every `kb.scheduled` run.
It scans the variant's base directory:

- `personal` → `/home/michael.gerber/nas/Data_Michael_restructured/Personal/*`
- `360f` → `/home/michael.gerber/nas/Data_Michael_restructured/360F/*`

…and returns every immediate subdirectory as `(slug, abs_path)`. No need
to edit `DEFAULT_ROOTS` when you add a subset — the next nightly run
picks it up automatically. (Stage 11 of the wizard fires the same
`kb.scheduled` after each successful Phase 7 rsync, so newly-restructured
subsets are searchable within minutes, not next morning.)

Skipped names: `@eaDir`, `#recycle`, `.DS_Store`, `lost+found`, anything
starting with `.`, `@`, or `#`.

---

## Removing a subset from the KB

The `kb.py remove` subcommand deletes every chunk for one root from the
active variant's collection, plus (by default) the per-root last-scan
summary file:

```bash
.venv/bin/python kb.py --variant 360f remove --root F-Finance
```

Output:
```
Will remove from folderreorg_360f (variant: 360f):
  · 1,234 chunks where root='F-Finance'
  · kb/data/360f/last_scan_F-Finance.json (last-scan summary file)

Proceed? [y/N]
```

What it does NOT touch (deliberately):
- Source files on the NAS (`/volume1/Data_Michael_restructured/360F/F-Finance/`)
- Pipeline state (`data/runs/360F/F-Finance.state.json`)
- Local scratch (`target_local/360F/F-Finance/`, `source_local/360F/F-Finance/`)
- Phase 3 log (`logs/360F/phase3_F-Finance.log`)

Use cases:
- **Forget a renamed/retired subset**: just the `kb.py remove` is enough.
- **Force a fresh re-index** (e.g. after schema change): `remove` then
  `reindex` rebuilds from source.
- **Wipe everywhere**: chain the `remove` with manual `rm -rf` of the
  scratch dirs and (optionally) the NAS destination — see
  `run-on-aizh.md` for the full sequence.

---

## Monitoring a running reindex

```bash
# Is anything indexing right now?
ssh aizh 'pgrep -af "kb\.scheduled" | grep -v grep || echo "(not running)"'

# Which model is in VRAM (bge-m3 = active embedding, qwen2.5:14b = chat)
ssh aizh 'ollama ps'

# Live tail when fired by systemd timer
ssh aizh 'journalctl --user -u folderreorg-kb-personal.service -f'
ssh aizh 'journalctl --user -u folderreorg-kb-360f.service -f'

# Most-recent per-root summary mtime tells you when each root last finished
ssh aizh 'ls -lt /home/michael.gerber/folderReorg/kb/data/{personal,360f}/last_scan_*.json'

# Built-in status command (collection size + per-root last-scan rollup)
ssh aizh 'cd /home/michael.gerber/folderReorg && .venv/bin/python kb.py --variant 360f status'
```

When the timer-driven scan completes, the per-root JSON file is rewritten
and its mtime jumps. `points` in `kb status` reflects the current
in-collection chunk count (after deletes-and-reinserts of changed files).

---

## Security notes

- **Qdrant binds to `127.0.0.1` only** in `docker-compose.yml`. Not LAN
  reachable. Both containers (personal:6333, 360f:6433) are loopback-only.
- **Streamlit chat UIs bind to `127.0.0.1`**. Reached via Cloudflare
  Tunnel (`cloudflared`) over the loopback interface, OR via SSH tunnel
  for stricter setups: `ssh -L 8502:localhost:8502 aizh`.
- **Cloudflare Access** gates every request to `private.vitalus.net` /
  `360f.vitalus.net` — email PIN flow per session, configurable in the
  Cloudflare Zero Trust dashboard.
- **Ollama** stays loopback-only on aizh.
- **Nothing leaves aizh + mgzh11** ever — LLM is local (qwen2.5:14b),
  embeddings are local (bge-m3), vector store is local (Qdrant),
  retrieval and RAG happen on the loopback interface.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `./kb.py …` raises `ModuleNotFoundError` (tqdm, qdrant_client, …) | Use `.venv/bin/python kb.py …` or `source .venv/bin/activate` first |
| `./kb.py setup` fails with "Cannot connect to Docker daemon" | `sudo systemctl start docker` and add user to `docker` group |
| Index run very slow on PDFs | OCR is the bottleneck; `KB_OCR_ENABLED=0 .venv/bin/python kb.py reindex` to skip image-PDFs |
| Chat UI unreachable from laptop (LAN) | `sudo ufw status` → ensure 8502/8503 allowed; or use SSH tunnel |
| Chat UI unreachable from internet | Check `cloudflared` is running (`systemctl status cloudflared`) and the Access policy includes your email |
| Qdrant collection lost after container restart | Confirm `qdrant_data/{personal,360f}/` directories persisted |
| `pytesseract.TesseractNotFoundError` | `sudo apt install tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng` |
| Timer not firing when you're logged out | `sudo loginctl enable-linger michael.gerber` |
| `kb.py status` shows `chunks_added: 0` for every root | Indexer hit an exception per file; inspect `kb/data/<variant>/last_scan_<root>.json` `errors` field |
| MuPDF spam during scan with no filename | Old `kb/extract.py`; update — errors now print as `MuPDF [text\|ocr\|open-fail] <filename>: <error>` |
| Chat says "no sources found" for content you indexed | Filters too narrow OR Streamlit cached an empty result; loosen filters and click "rerun" |
| F-Finance chunks appear under wrong variant | Pre-namespacing slug collision — see *Slug collisions* in `run-on-aizh.md` |
| `kb.py status` shows `indexed: 0` but `points` > 12k | Qdrant builds the HNSW index lazily; searches still work, index rebuilds within ~5 min idle |
| Chat preview crashes on PDFs | Fix already in place — falls back to base64 iframe when `streamlit[pdf]` extra missing |
| Want to wipe and reindex from zero | `docker compose -f docker/qdrant/docker-compose.yml down -v` then `kb.py setup && kb.py --variant <X> index` |
| Want to wipe just one root from a variant | `.venv/bin/python kb.py --variant <X> remove --root <ROOT> -y` then `kb.py --variant <X> reindex` |
