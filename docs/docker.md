# Docker — fully containerized stack (recommended)

The Docker setup brings up qdrant ×2, both Streamlit chat UIs, and the
Cloudflare Tunnel as long-lived services with `restart: unless-stopped`.
The pipeline (`run.py`) and KB CLI (`kb.py`) run as one-shot containers
via `docker compose run`.

**Ollama stays on the host** — it's shared with other applications on
your machine, so containerizing it would just add complexity. The
folder-reorg containers use `network_mode: host` to reach Ollama at
`http://127.0.0.1:11434` exactly the way the host already does.

If you prefer the manual `nohup`/systemd approach, see
[`run-on-aizh.md`](./run-on-aizh.md). Both are supported; pick one and
stick with it.

---

## Why Docker

| Friction today | Containerized |
|---|---|
| `./run.py` over SSH dies on disconnect | `docker compose run -d` is detached from the start; no `tmux` ritual |
| Streamlit `nohup &` rituals after every reboot | `restart: unless-stopped` auto-restarts |
| Logs in `/tmp/streamlit-*.log` (lost on reboot) | `docker compose logs chat-personal` (persistent, structured) |
| Per-host setup is 14 sections of `setup.md` | Install Docker + NAS mount + Cloudflare cert, `docker compose up -d` |
| New machine = redo every install step | Copy `docker-compose.yml`, `.env`, qdrant data dirs, `~/.cloudflared/`, `~/.ssh/id_ed25519`, done |
| Status / wizard concerns mixed with service concerns | Services in compose; pipeline + status remain on host where they belong |

---

## Prerequisites (on the host)

You still need these on the host — same as the manual setup:

- **Ubuntu 24.04** (or any recent Linux with Docker support)
- **Docker** + `docker compose` plugin (see [`setup.md`](./setup.md) §1.3)
- **NVIDIA driver** + **`nvidia-container-toolkit`** are NOT needed since
  Ollama stays on the host. Driver only required for Ollama itself.
- **Ollama** running on the host with the two models pulled
  (see [`setup.md`](./setup.md) §2):

  ```bash
  ollama list
  # qwen2.5:14b-instruct-q4_K_M    9.0 GB
  # bge-m3                         1.2 GB
  ```

- **NAS SSHFS mount** at `~/nas` (see [`setup.md`](./setup.md) §3 — same
  setup as before; the mount lives on the host and gets bind-mounted
  into containers as read-only):

  ```bash
  ./kb.py mount      # idempotent
  ls ~/nas/Data_Michael_restructured/   # sanity check
  ```

- **Cloudflare Tunnel** credentials at `~/.cloudflared/` if you want
  public chat access (see [`setup.md`](./setup.md) §8). Optional — without
  it, the chat UIs are still reachable on the LAN at
  `http://<aizh-ip>:8502 / :8503` (UFW permitting).

---

## First-time setup

```bash
# Clone (or already on aizh)
cd /home/michael.gerber/folderReorg

# Configure for your host
cp .env.example .env
$EDITOR .env
# At minimum, verify APP_UID / APP_GID match your host user (`id -u` / `id -g`).

# Build the application image (~3 min: installs tesseract, antiword,
# python deps including hdbscan + sentence-transformers + qdrant-client)
docker compose build

# Bring up the always-on services
docker compose up -d

# Verify
docker compose ps
# Expect: qdrant-personal, qdrant-360f, chat-personal, chat-360f, cloudflared
#         all "Up", restart "unless-stopped"

# Smoke test
curl -s http://127.0.0.1:6333/collections | jq        # personal qdrant
curl -s http://127.0.0.1:6433/collections | jq        # 360f qdrant
curl -s -I http://127.0.0.1:8502 | head -1            # chat personal
curl -s -I http://127.0.0.1:8503 | head -1            # chat 360f
```

---

## Day-to-day operations

### Pipeline runs

```bash
# Interactive wizard (with TTY for prompts and progress bars)
docker compose run -it --rm pipeline ./run.py

# Specific subset, attended
docker compose run -it --rm pipeline ./run.py \
    --subset M-Marketing --collection 360F --auto-run --source-from-mount

# Batch run — DETACHED FROM THE START. Survives SSH disconnect, Wi-Fi drop,
# laptop sleep — all the things tmux/nohup were ever for. No ritual needed.
docker compose run -d --name reorg-batch pipeline \
    ./run.py --batch all --source-from-mount

# Watch a detached batch
docker compose logs -f reorg-batch

# Stop a detached batch (graceful — sends SIGTERM, wizard saves state)
docker stop reorg-batch
docker rm   reorg-batch
```

> **Why no `tmux`?** Inside a detached container the process is no longer
> attached to your shell. It runs as a child of the Docker daemon, which
> survives reboots, SSH drops, etc. The whole point of tmux was to put the
> process under a different parent. Containers do that by definition.

### KB operations

```bash
# Status (collection size + last-scan summary)
docker compose run --rm pipeline .venv/bin/python kb.py --variant personal status
docker compose run --rm pipeline .venv/bin/python kb.py --variant 360f     status

# Manual delta scan
docker compose run --rm pipeline .venv/bin/python kb.py --variant personal reindex

# Remove a root from the KB
docker compose run --rm pipeline .venv/bin/python kb.py --variant 360f remove --root F-Finance
```

### Status snapshot

`./status.py` stays on the host — it queries host-level state (`pgrep`,
`nvidia-smi`, `ollama ps`, `docker ps`, `findmnt`) that wouldn't be
visible from inside a container. Same command as before:

```bash
./status.py             # one-shot
./status.py --watch 5   # live updating
```

The CHAT UI / SERVICES section now correctly shows the containers as
running (it greps for `qdrant-`-suffix names, which match
`folderreorg-qdrant-personal` etc).

### Restart / inspect / logs

```bash
# Service-level lifecycle
docker compose restart chat-personal    # one container
docker compose restart                   # all (except oneshot pipeline)
docker compose down                      # stop everything, keep data
docker compose down -v                   # stop + WIPE Qdrant data (DESTRUCTIVE)

# Logs
docker compose logs chat-personal         # entire history
docker compose logs -f chat-360f          # live tail
docker compose logs --since 1h cloudflared

# Open a shell in a running container for debugging
docker compose exec chat-personal bash
```

### Updating after a `git pull`

```bash
cd /home/michael.gerber/folderReorg
git pull
docker compose build         # rebuild image with new source
docker compose up -d         # recreate any service whose image changed
```

If you only edited a Streamlit file, the Streamlit server inside its
container will hot-reload automatically when the bind-mounted source
changes. **But** the source isn't bind-mounted by default in this
compose — to enable hot reload during development, add to the chat
services in a `docker-compose.override.yml`:

```yaml
services:
  chat-personal:
    volumes:
      - ./chat_ui:/app/chat_ui
```

---

## Migration from the manual setup

If you've been running the `nohup` / systemd-timer approach and want to
switch:

```bash
ssh aizh
cd /home/michael.gerber/folderReorg

# 1. Stop existing services
pkill -f "streamlit run chat_ui" || true
docker compose -f docker/qdrant/docker-compose.yml down
systemctl --user stop folderreorg-kb-personal.timer || true
systemctl --user stop folderreorg-kb-360f.timer || true

# 2. Pull the docker branch
git checkout main
git pull

# 3. Configure
cp .env.example .env
$EDITOR .env

# 4. Bring up the new stack
docker compose build
docker compose up -d

# 5. Verify the same data is there
docker compose run --rm pipeline .venv/bin/python kb.py --variant personal status
docker compose run --rm pipeline .venv/bin/python kb.py --variant 360f     status
# Expect: same "points" count as before
```

The Qdrant data path defaults match the old layout
(`./qdrant_data_personal` and `./qdrant_data_360f`), so existing indexes
are preserved automatically.

### Decommission the old systemd timers

Once you've confirmed the docker stack works, retire the user timers
and replace them with a host-side cron entry that triggers the
containerized indexer:

```bash
systemctl --user disable --now folderreorg-kb-personal.timer
systemctl --user disable --now folderreorg-kb-360f.timer

# Cron alternative (~/.config/cron or `crontab -e` if you prefer):
crontab -l > /tmp/cron.now
cat >> /tmp/cron.now <<'EOF'

# folder-reorg KB delta scans, post-Docker
2 2 * * *  cd /home/michael.gerber/folderReorg && docker compose run --rm -e KB_VARIANT=personal pipeline .venv/bin/python kb.py --variant personal reindex >> /tmp/folderreorg-kb-personal.log 2>&1
17 2 * * * cd /home/michael.gerber/folderReorg && docker compose run --rm -e KB_VARIANT=360f     pipeline .venv/bin/python kb.py --variant 360f     reindex >> /tmp/folderreorg-kb-360f.log     2>&1
EOF
crontab /tmp/cron.now
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose build` fails: `error: externally-managed-environment` | The Dockerfile uses `uv` for installs; if you're hitting this you're probably building on a system that has `pip` reset and no `uv`. Re-run; the apt-installed pip in slim images includes uv. |
| Chat container boots then exits with code 1 | Check `docker compose logs chat-personal`. Most common: `qdrant-personal` not yet ready when chat tried to connect. Add `restart: on-failure:5` and let compose retry. |
| Pipeline container can't reach Ollama | Verify Ollama is listening on the host: `curl http://127.0.0.1:11434`. If it only listens on a bridge interface (e.g. via OLLAMA_HOST), use `network_mode: host` (already set) AND check the host's actual binding |
| `KeyError: 'Personal'` on `kb.py status` | The `KB_VARIANT` env var isn't set. Always pass `-e KB_VARIANT=personal` (or `360f`) to `docker compose run`, OR set the default in `.env`. |
| Bind-mounted files written by container show as `root:root` on host | Set `APP_UID` and `APP_GID` in `.env` to your host user (`id -u` / `id -g`) and rebuild the image |
| Cloudflared container dies repeatedly with "no credentials" | `~/.cloudflared/<UUID>.json` doesn't exist or `CLOUDFLARED_CONFIG_HOST` in .env points elsewhere. Run `cloudflared tunnel login` on the host first |
| `docker compose run` hangs at the wizard's first prompt | You forgot the `-it` flag — interactive mode needs a TTY |
| Streamlit can't read PDF files via the chat preview | Make sure `NAS_MOUNT_HOST` in .env is set and the SSHFS mount on the host is alive (`./kb.py mount`) |

---

## What stays on the host (and why)

| Thing | Why on host |
|---|---|
| Ollama (LLM + embeddings server) | Shared with other apps on the machine |
| SSHFS mount of the NAS (`~/nas`) | FUSE in containers needs `--cap-add SYS_ADMIN --device /dev/fuse`, weakens isolation |
| `./status.py` | Introspects host PIDs, GPU, mounts, container states |
| `./kb.py mount` / `umount` | Calls host-level `sshfs` / `fusermount3` |
| NVIDIA driver | Kernel-level — only Ollama uses it |

---

## See also

- [`setup.md`](./setup.md) — first-time host setup (system pkgs, Ollama, NAS, Cloudflare)
- [`run-on-aizh.md`](./run-on-aizh.md) — operational runbook (wizard / batch / troubleshooting)
- [`unattended-runs.md`](./unattended-runs.md) — tmux / nohup / systemd-run for the manual non-Docker path
- [`knowledge-base.md`](./knowledge-base.md) — KB / chat UI architecture and operations
