# Setup — End-to-End Installation on a Fresh Host

Walks you from a bare Ubuntu 24.04 host with an NVIDIA GPU through to a
fully running solution: pipeline (`./run.py`), KB indexer (`./kb.py`),
two private chat UIs reachable from anywhere via Cloudflare Tunnel,
nightly delta scans, and the status tooling (`./status.py`).

If you already have a running install and just want operating
instructions, see [`run-on-aizh.md`](./run-on-aizh.md). If you want to
understand the *why*, see [`pipeline-overview.md`](./pipeline-overview.md)
and [`knowledge-base.md`](./knowledge-base.md). This doc is the **how
to install from scratch**.

---

## 0. Prerequisites

### Hardware
- **GPU**: NVIDIA, ≥10 GB VRAM (we run qwen2.5:14b ≈ 9 GB + bge-m3 ≈ 2 GB
  alternately). Tested on RTX 3090 24 GB.
- **CPU**: any modern x86_64. The bottleneck is GPU + NAS I/O.
- **RAM**: ≥16 GB
- **Disk**: SSD with ≥200 GB free for the venv, model caches, Qdrant
  vector data, plus per-subset scratch (~60 GB per subset if not using
  `--source-from-mount`).
- **Network**: gigabit Ethernet to the NAS recommended (we hit a 100 Mb
  bottleneck during initial testing — confirm with
  `cat /sys/class/net/<iface>/speed`).

### Network / accounts
- A **NAS** (Synology DSM in our case) reachable over SSH; you'll need
  SSH key auth set up. Hereafter referred to as `mgzh11`.
- A **Cloudflare account** with a domain (`vitalus.net` in our case) and
  Cloudflare Zero Trust (free tier is fine) for public chat access.
- A **GitHub account** to clone the repo.

### Conventions used below
- **Host name**: `aizh` (Ubuntu 24.04 + RTX 3090). All commands are run
  there unless noted.
- **NAS host**: `mgzh11`. SSH from `aizh` should work without a password.
- **Username**: `michael.gerber`. Replace with your own throughout.
- **Repo path**: `/home/michael.gerber/folderReorg`. All commands assume
  you've `cd`'d there.

---

## 1. Base OS + GPU stack

### 1.1 System packages

```bash
sudo apt update
sudo apt install -y \
    build-essential git curl wget jq unzip tree htop \
    ca-certificates gnupg lsb-release \
    sshfs fuse3 \
    tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
    antiword \
    python3-pip python3-venv
```

What each does:

| Package | Purpose |
|---|---|
| `sshfs`, `fuse3` | Mount the NAS read-only at `~/nas` (KB indexer reads from here) |
| `tesseract-ocr*` | OCR for image-only PDFs and standalone images |
| `antiword` | Legacy `.doc` text extraction |
| `python3-pip`, `python3-venv` | Bootstrap before `uv` is installed |

### 1.2 NVIDIA driver + CUDA

If `nvidia-smi` doesn't already work:

```bash
# Pick the latest "server" driver branch that supports your GPU
sudo apt install -y nvidia-driver-580   # match what's available; ours is 580.126.09
sudo reboot
```

After the reboot, verify:

```bash
nvidia-smi
# Expect: card name, driver version, memory free, no processes yet
```

### 1.3 Docker (for the Qdrant containers)

```bash
# Official Docker repo
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

# Add yourself to the docker group so `docker ps` works without sudo
sudo usermod -aG docker $USER
newgrp docker          # apply NOW for the current shell

docker version         # sanity check
```

### 1.4 UFW firewall

The chat UIs bind to `127.0.0.1` (Cloudflare Tunnel reaches them over
loopback), but if you also want LAN access from your laptop:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8502/tcp        # Personal chat UI (LAN)
sudo ufw allow 8503/tcp        # 360F chat UI (LAN)
sudo ufw enable
sudo ufw status numbered
```

### 1.5 Long-lived user services (`loginctl`)

The systemd timers that drive nightly KB scans run as user services.
Without `linger`, they stop firing when you log out:

```bash
sudo loginctl enable-linger $USER
loginctl show-user $USER | grep Linger    # expect Linger=yes
```

---

## 2. Ollama + LLM models

### 2.1 Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama --version
```

The systemd unit `ollama.service` is installed by the installer and
binds to `127.0.0.1:11434` by default — do **not** expose it on `0.0.0.0`.

Optional: persist Ollama models across restarts and let them stay
loaded longer in VRAM (default is 5 min idle):

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
cat <<'EOF' | sudo tee /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

### 2.2 Pull the models

```bash
ollama pull qwen2.5:14b-instruct-q4_K_M   # ~9 GB; chat + cluster naming + per-file naming
ollama pull bge-m3                        # ~1.2 GB; embeddings (multilingual, 1024-dim)

ollama list
# Expect both listed
```

> Tip: If you want better Phase 3 / chat quality on rare languages,
> you can also pull `qwen2.5:32b-instruct-q4_K_M` (~19 GB) and switch
> later by setting `KB_LLM_MODEL=qwen2.5:32b-instruct-q4_K_M` in the
> environment. The 14B variant is the project default — fast and
> sufficient for DE / FR / IT / EN.

---

## 3. NAS access

### 3.1 SSH key to the NAS

```bash
# Generate a dedicated key (no passphrase — needed for unattended mounting)
ssh-keygen -t ed25519 -C "${USER}@aizh-to-nas" -f ~/.ssh/id_ed25519 -N ""

# Add it to the NAS user's authorized_keys (one-time password prompt)
ssh-copy-id -i ~/.ssh/id_ed25519.pub <nas-user>@<nas-ip>

# Verify passwordless login
ssh <nas-user>@<nas-ip> 'echo ok'
```

### 3.2 SSH config alias

So we can use `mgzh11` everywhere instead of memorising the IP:

```bash
cat >> ~/.ssh/config <<'EOF'

Host mgzh11
    HostName 192.168.1.20      # ← replace with your NAS IP/hostname
    User michael.gerber        # ← replace with your NAS username
    IdentityFile ~/.ssh/id_ed25519
    Compression no
    Ciphers aes128-gcm@openssh.com
EOF
chmod 600 ~/.ssh/config

ssh mgzh11 'ls /volume1/ | head'   # sanity check
```

The `Compression no` + `Ciphers aes128-gcm@openssh.com` combo is what
gave us peak SSHFS throughput on AES-NI CPUs. Skip them if you prefer
defaults — just expect ~30 % slower mount reads.

### 3.3 Mount point

The KB indexer reads from `/home/michael.gerber/nas/`. Create the
directory; the mount script will use it:

```bash
mkdir -p ~/nas
```

The actual mount happens via `./kb.py mount` once the repo is cloned
(next section). You can test it manually with:

```bash
sshfs -o ro,reconnect,Compression=no,Ciphers=aes128-gcm@openssh.com \
    mgzh11:. ~/nas
ls ~/nas/Data_Michael/ | head
fusermount3 -u ~/nas       # unmount when done testing
```

### 3.4 Expected NAS folder layout

The pipeline assumes this on the NAS:

```
/volume1/Data_Michael/                   ← Personal SOURCE (read-only)
    F - Finance/
    C - Companies/
    G - Gesundheit Health/
    ...

/volume1/360F-A-Admin/                   ← 360F SOURCE folders (read-only)
/volume1/360F-B-SCB/
/volume1/360F-...

/volume1/Data_Michael_restructured/      ← DESTINATION (created by Phase 7)
    Personal/
        F-Finance/
        C-Companies/
        ...
    360F/
        A-Admin/
        B-SCB/
        ...
```

Create `Data_Michael_restructured/` with read+write permission for your
NAS user, via DSM Control Panel → Shared Folder. The pipeline never
modifies the source roots; it only writes under
`Data_Michael_restructured/<collection>/<slug>/`.

---

## 4. Clone the repo + Python environment

### 4.1 Optional: GitHub SSH key

If you'll push commits back, set up a GitHub SSH key + alias:

```bash
ssh-keygen -t ed25519 -C "${USER}@aizh-github" -f ~/.ssh/id_ed25519_github -N ""
cat ~/.ssh/id_ed25519_github.pub
# Paste into https://github.com/settings/keys

cat >> ~/.ssh/config <<'EOF'

Host github-aizh
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

ssh -T github-aizh    # expect "Hi <user>! You've successfully authenticated…"
```

### 4.2 Clone the repo

```bash
cd ~
git clone git@github-aizh:michaelGoogle/folderReorg.git folderReorg
# or HTTPS if you don't need to push:
# git clone https://github.com/michaelGoogle/folderReorg.git folderReorg
cd folderReorg
```

### 4.3 Install `uv` (fast pip / venv manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Add ~/.local/bin to PATH if not already
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

uv --version
```

### 4.4 Create the venv + install deps

```bash
cd ~/folderReorg
uv venv .venv --python python3.12
source .venv/bin/activate

# Project dependencies (pulls qdrant-client, sentence-transformers,
# pymupdf, openpyxl, python-docx, python-pptx, xlrd, hdbscan, etc.)
uv pip install -e .

# A few KB-side tools that aren't in pyproject.toml (yet)
uv pip install pytesseract pillow

# Verify
.venv/bin/python -c "
import qdrant_client, sentence_transformers, fitz, docx, openpyxl, pptx, xlrd, lingua, pytesseract
print('all extraction deps OK')
"
```

> **Why use `.venv/bin/python …` everywhere instead of `./kb.py`?**
> The shebang on `kb.py`, `run.py`, `status.py` is `#!/usr/bin/env
> python3` which resolves to the **system** Python (no `tqdm`,
> `qdrant-client`, etc.). Either always invoke via the venv binary
> (e.g. `.venv/bin/python kb.py status`) or `source .venv/bin/activate`
> once per shell.

---

## 5. Qdrant — bring up both vector stores

The repo ships a docker-compose with two Qdrant containers (one per
variant), each bound to `127.0.0.1` only:

```bash
cd ~/folderReorg
.venv/bin/python kb.py setup
```

What that does (idempotent — safe to re-run):

1. `docker compose up -d` for `docker/qdrant/docker-compose.yml`:
   - `folderreorg-qdrant-personal` on `127.0.0.1:6333`
   - `folderreorg-qdrant-360f`     on `127.0.0.1:6433`
   - Persistent volumes under `qdrant_data/{personal,360f}/`
2. Creates the two collections via the Qdrant client:
   - `folderreorg_personal` (1024-dim cosine, payload indices on
     root/rel_path/sha256/language/yymm/compound/file_id/text_source/
     extraction_status)
   - `folderreorg_360f` (same schema)

Verify:

```bash
docker ps | grep qdrant
# Expect both containers Up and listening on 127.0.0.1:6333 / :6433

.venv/bin/python kb.py --variant personal status
.venv/bin/python kb.py --variant 360f     status
# Expect: collection name + 0 points + green status
```

---

## 6. Mount the NAS + first index

```bash
cd ~/folderReorg
./kb.py mount

# Sanity check
ls ~/nas/Data_Michael_restructured/ 2>/dev/null
# Expect: empty if you haven't run Phase 7 yet,
# or "Personal/  360F/" if you have prior runs
```

If `Data_Michael_restructured/` doesn't exist on the NAS yet, create it
via DSM Control Panel → Shared Folder. The pipeline's Stage 7 will
create the per-collection subfolders on first use.

If you have prior restructured content, do an initial full index:

```bash
.venv/bin/python kb.py --variant personal index
.venv/bin/python kb.py --variant 360f     index
# These iterate every root under Data_Michael_restructured/<col>/, so they
# only do work if there's content. Safe to run on an empty tree.
```

---

## 7. Streamlit chat UIs

Each variant runs as its own Streamlit process bound to localhost.
Cloudflare Tunnel (next section) handles public access.

### 7.1 Launch the two instances

```bash
cd ~/folderReorg

# Personal
nohup env KB_VARIANT=personal .venv/bin/streamlit run chat_ui/chat_ui.py \
    --server.address 127.0.0.1 --server.port 8502 \
    --server.headless true --browser.gatherUsageStats false \
    > /tmp/streamlit-personal.log 2>&1 &

# 360F
nohup env KB_VARIANT=360f .venv/bin/streamlit run chat_ui/chat_ui.py \
    --server.address 127.0.0.1 --server.port 8503 \
    --server.headless true --browser.gatherUsageStats false \
    > /tmp/streamlit-360f.log 2>&1 &

# Verify
ss -lntp | grep -E ':8502|:8503'
# Expect both listening on 127.0.0.1
```

### 7.2 LAN test (optional)

If you opened the UFW ports in §1.4, you can reach the chat from your
LAN at `http://<aizh-ip>:8502` and `…:8503` without Cloudflare. Useful
for in-house testing before the tunnel is set up.

To survive reboots, see §9 (systemd) for a user-service unit.

---

## 8. Cloudflare Tunnel + Access

This step makes the chat UIs reachable from anywhere on
`https://private.vitalus.net` and `https://360f.vitalus.net` with
email-PIN authentication, **without opening any ports** on aizh's
firewall or the LAN router.

### 8.1 Install `cloudflared`

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
    sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
    https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | \
    sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update
sudo apt install -y cloudflared
```

### 8.2 Authenticate + create the tunnel

```bash
cloudflared tunnel login
# Opens a browser. Pick the zone (e.g. vitalus.net). Saves cert to
# ~/.cloudflared/cert.pem

cloudflared tunnel create folderreorg
# Prints a tunnel UUID and writes ~/.cloudflared/<UUID>.json (credentials)
```

### 8.3 Route DNS records

```bash
cloudflared tunnel route dns folderreorg private.vitalus.net
cloudflared tunnel route dns folderreorg 360f.vitalus.net
```

These create CNAME records on Cloudflare pointing the hostnames at the
tunnel.

### 8.4 Tunnel config

```bash
mkdir -p ~/.cloudflared
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: folderreorg
credentials-file: /home/michael.gerber/.cloudflared/<UUID>.json   # replace <UUID>

ingress:
  - hostname: private.vitalus.net
    service: http://127.0.0.1:8502
  - hostname: 360f.vitalus.net
    service: http://127.0.0.1:8503
  - service: http_status:404
EOF
```

Run interactively first to make sure it connects:

```bash
cloudflared tunnel run folderreorg
# Expect: "INF Connection registered" lines
# Open https://private.vitalus.net in a browser → should reach the chat UI
# Ctrl-C to stop
```

### 8.5 Cloudflare Access policy

In the Cloudflare Zero Trust dashboard:

1. **Access → Applications → Add an application → Self-hosted**
2. Application domain: `private.vitalus.net` (and again for `360f.vitalus.net`)
3. **Add policy** → name "owner" → Include rule:
   - Selector: **Emails** → enter your email(s)
4. (Optional) Add a "trusted family" policy with their emails too
5. Save

From now on, every request to those URLs prompts for an email; users
get a 6-digit PIN by email and have a session for 24 hours.

### 8.6 Run as a system service

```bash
sudo cloudflared --config /home/michael.gerber/.cloudflared/config.yml \
    service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

---

## 9. Systemd timers — nightly KB scans

The repo ships per-variant timer + service unit files under `systemd/`.
They're user-level units (run as you, not root) so they have access to
your venv and SSH agent.

```bash
cd ~/folderReorg
mkdir -p ~/.config/systemd/user

cp systemd/folderreorg-kb-personal.{service,timer} ~/.config/systemd/user/
cp systemd/folderreorg-kb-360f.{service,timer}     ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now folderreorg-kb-personal.timer
systemctl --user enable --now folderreorg-kb-360f.timer

systemctl --user list-timers 'folderreorg-kb-*.timer'
# Expect both with NEXT firing in the early morning (≈02:00 / 02:15)
```

Verify a manual run works:

```bash
systemctl --user start folderreorg-kb-personal.service
journalctl --user -u folderreorg-kb-personal.service -f
# Ctrl-C once you see the per-root summary lines
```

---

## 10. End-to-end smoke test

### 10.1 Pre-flight

```bash
cd ~/folderReorg
./status.py --no-color
```

Expect:
- **CURRENTLY RUNNING**: idle (no wizard, no stage subprocess, no GPU model)
- **CHAT UI / SERVICES**: ✓ both Streamlit + ✓ both Qdrant containers
- **NAS MOUNT**: ✓ `/home/michael.gerber/nas` mounted, ≥0 entries reachable

### 10.2 First pipeline run (small subset)

Pick the smallest subset on your NAS to test end-to-end. Then:

```bash
./run.py --subset SmallSubset --collection Personal \
    --auto-run --source-from-mount
```

This will:
1. Pre-flight checks (Stage 0)
2. Skip rsync-in (Stage 1, no-op under `--source-from-mount`)
3. Reset working data (Stage 2; writes `data/.current_subset` sentinel)
4. Phase 0 manifest → Phase 7 rsync-out → Phase 8 KB index (Stages 3–11)

Monitor in another terminal:

```bash
./status.py --watch 5
```

You should see the wizard advance through stages, the GPU light up
during Phase 3 (qwen2.5:14b) and the KB scan (bge-m3), and finally the
"LAST COMPLETED" section update with the just-finished subset.

### 10.3 Verify chat works

Open `https://private.vitalus.net` (or `http://aizh:8502` over LAN).
Authenticate via email PIN. Ask a question relevant to the test subset.
Sources should appear, with **Preview**, **Download**, and **Expand**
options per source.

### 10.4 Verify the KB indexer ran

```bash
.venv/bin/python kb.py --variant personal status
# Expect: collection size > 0; last_scan summary for the test subset
```

### 10.5 Tear-down (if you want to reset and start over)

```bash
# Wipe ALL Qdrant data (keeps containers)
docker compose -f ~/folderReorg/docker/qdrant/docker-compose.yml down -v
.venv/bin/python kb.py setup    # recreate collections

# Wipe pipeline state (keeps source on NAS)
rm -rf ~/folderReorg/data/runs/*
rm -rf ~/folderReorg/data/extracted_text ~/folderReorg/data/*.csv ~/folderReorg/data/*.npy
rm -rf ~/folderReorg/source_local ~/folderReorg/target_local

# Wipe restructured data on the NAS (DESTRUCTIVE — be sure)
ssh mgzh11 'rm -rf /volume1/Data_Michael_restructured/*'
```

---

## 11. Routine operations (post-setup)

| You want to… | Use |
|---|---|
| Restructure one subset interactively | `./run.py` |
| Restructure many subsets overnight | `./run.py --batch all --source-from-mount` |
| See what's running right now | `./status.py` (or `./status.py --watch 5`) |
| Inspect KB collection state | `.venv/bin/python kb.py --variant <X> status` |
| Manual KB delta scan | `.venv/bin/python kb.py --variant <X> reindex` |
| Remove one root from KB | `.venv/bin/python kb.py --variant <X> remove --root <NAME>` |
| Drill into per-file errors | `./status.py --errors --root <NAME>` |
| Drill into per-file skip reasons | `./status.py --skipped --root <NAME>` |

For the full operational reference, see [`run-on-aizh.md`](./run-on-aizh.md).

---

## 12. Common gotchas

| Symptom | Fix |
|---|---|
| `./kb.py: ModuleNotFoundError` | Use `.venv/bin/python kb.py …` or `source .venv/bin/activate` first — the shebang resolves to the system python |
| `nvidia-smi` works but Ollama can't see GPU | `sudo systemctl restart ollama` after a driver install |
| Phase 3 crashes with `cluster_assignments.csv not found` | A previous run for a *different* subset wiped `data/`. The wizard now auto-detects and re-runs stages 2-9; if you see this on an old client, update `run.py` |
| Stage 1 fails with "source folder not reachable" | NAS mount lost; `./kb.py umount && ./kb.py mount` and retry |
| `.doc` files not extracted | `sudo apt install antiword` |
| `.ppt` files not extracted | No lightweight option; the synthetic-context fallback indexes them by filename. If full-text needed, install LibreOffice (heavy) |
| Streamlit unreachable from LAN | `sudo ufw allow 8502/tcp` (and 8503), or use SSH tunnel: `ssh -L 8502:localhost:8502 aizh` |
| Cloudflare URL returns "Access blocked" | Email not in the Zero Trust Access policy — add it in the Cloudflare dashboard |
| Systemd timers stop firing when you log out | `sudo loginctl enable-linger <user>` (one-time) |
| `./status.py` shows orphaned worker processes | They auto-reap by default; `--no-reap` to inspect first |
| Slug clash between Personal & 360F runs (e.g. F-Finance) | The wizard now namespaces all scratch by collection. If state files predate that, run `./run.py` once and the migration runs automatically |
| Qdrant collection lost after `docker compose down -v` | That `-v` flag deletes volumes. Re-run `kb.py setup` to recreate, then `kb.py reindex` to rebuild from source |
| HuggingFace download blocked by network | Models cache to `~/.cache/huggingface/hub`. Pre-download bge-m3 on a connected machine and rsync the cache |

---

## 13. What gets created on disk

After full setup, the layout is:

```
~/folderReorg/
  ├── .venv/                          ← Python venv (gitignored)
  ├── data/
  │   ├── runs/<collection>/<slug>.state.json   ← per-subset wizard state
  │   ├── extracted_text/             ← Phase 1 text per file (gitignored)
  │   ├── *.csv  *.npy                ← Phase 0–6 intermediate artifacts
  │   └── .current_subset             ← sentinel (which subset owns data/ now)
  ├── source_local/<collection>/<slug>/   ← rsync-in mirror (only when not --source-from-mount)
  ├── target_local/<collection>/<slug>/   ← Phase 5 output, then Phase 7 rsync-out source
  ├── logs/
  │   ├── <collection>/phase3_<slug>.log    ← Phase 3 LLM output, per subset
  │   └── (no other depth-1 files in the steady state)
  ├── kb/data/{personal,360f}/last_scan_<root>.json   ← per-root scan summaries
  └── qdrant_data/{personal,360f}/         ← Qdrant container volumes (persistent)

~/.cloudflared/
  ├── cert.pem
  ├── <tunnel-uuid>.json
  └── config.yml

~/.config/systemd/user/
  ├── folderreorg-kb-personal.service / .timer
  └── folderreorg-kb-360f.service / .timer

~/.cache/huggingface/hub/             ← bge-m3 (~1.2 GB after first index)
~/.ollama/models/                     ← qwen2.5:14b (~9 GB) + bge-m3 (mirror, ~1.2 GB)
~/nas → SSHFS mount of mgzh11:.       ← read-only NAS access (ephemeral)
```

Disk-usage estimate after a full Personal + 360F restructure indexed:
- venv + caches + Qdrant data: ≈ 30–50 GB
- per-subset scratch (gone after manual cleanup): ≈ 60 GB while in-flight
- restructured data lives on the NAS, not aizh

---

## 14. Where to go next

- [`run-on-aizh.md`](./run-on-aizh.md) — operational runbook (wizard, batch, troubleshooting)
- [`knowledge-base.md`](./knowledge-base.md) — KB architecture, chat UI features, removal, monitoring
- [`pipeline-overview.md`](./pipeline-overview.md) — phase-by-phase data flow design
