# Dashboard — unified web UI for the folder-reorg stack

A single Streamlit multipage app that consolidates everything: status,
chat, KB operations, and the pipeline. One sidebar variant selector
applies across all pages — pick **Personal** or **360F** once and every
page acts on the right stack.

**LAN access only**, no Cloudflare. Bound to `0.0.0.0:8500` so it's
reachable at `http://<aizh-ip>:8500` (typically `http://192.168.1.10:8500`).

| Page | What it does |
|---|---|
| 📊 **Status** | Web port of `status.py` — what's running, GPU state, recent KB scans, services, NAS mount. Optional 5-second auto-refresh. |
| 💬 **Chat** | RAG chat over the active variant's KB. Same 2-column layout (chat left, sticky preview pane right) and source cards (Preview / Download / Expand) as the standalone chat UI. Per-variant history. |
| 🔍 **Knowledge Base** | Wraps `kb.py status / reindex / remove / cache-flush`. Long-running reindex spawns a detached subprocess; the page tails its log. |
| 🛠 **Pipeline** | Wraps `run.py`. Browse subsets discovered on the NAS (with restructured/partial/fresh status), launch single-subset or batch runs, view live log, send SIGINT to stop. |

---

## First-time launch

```bash
ssh aizh
cd /home/michael.gerber/folderReorg

# Open the LAN port (one-time)
sudo ufw allow 8500/tcp

# Foreground, for a quick verification:
./dashboard/launch.sh
# Visit http://<aizh-ip>:8500

# Once verified, run detached so it survives SSH disconnect:
tmux new -d -s dashboard ./dashboard/launch.sh
# Re-attach later: tmux attach -t dashboard
```

---

## Architecture notes

### Variant switching
Each page reads `st.session_state["variant"]` (set by the sidebar
selectbox) and constructs Qdrant clients explicitly via
`dashboard/_common.py::qdrant_client(variant)`. This bypasses
`kb.config.KB_VARIANT`, which is only read at module-import time and
can't easily be flipped mid-process.

`kb/query.py::search()` accepts optional `qdrant_url=` and
`collection=` kwargs (with sensible env-derived defaults). The chat
page passes the active variant's values per call.

### Long-running operations
Reindex / `run.py` batches / single-subset runs spawn detached
subprocesses with `start_new_session=True` (so they survive Streamlit
reruns AND SSH disconnects). Each spawn writes to a known log path
(`/tmp/folderreorg-dashboard-<op>-<timestamp>.log`); the page tails the
log on subsequent reruns. The PID is stored in `st.session_state` so
the page can show "running" / "stopped" state and offer a SIGINT button.

### What stays on the host (not in the dashboard)
- **Ollama** — runs as a host service, accessed at `localhost:11434`
- **NAS SSHFS mount** — managed by `./kb.py mount` outside the dashboard
- **Qdrant containers** — running via `docker/qdrant/docker-compose.yml`
- **NVIDIA driver** — kernel-level

The dashboard is a UI on top of the existing system; it doesn't replace
any of those services. Killing the dashboard doesn't affect anything
else (chat, indexer, pipeline) — those are detached.

---

## Migrating away from the old chat UIs

Once you've verified the dashboard works, you can decommission the
two pre-existing Streamlit instances on `:8052` and `:8053`:

```bash
ssh aizh
pkill -f "streamlit run chat_ui"

# Verify they're gone:
ssh aizh 'pgrep -af "streamlit run chat_ui"' || echo "(none)"

# (Optional) update Cloudflare Tunnel ingress to point at :8500 for
# both private.vitalus.net and 360f.vitalus.net:
#   See ~/.cloudflared/config.yml — replace 8052/8053 service entries
#   with a single 8500. Then `sudo systemctl restart cloudflared`.
```

The `Status` page's CHAT UI / SERVICES section flags these old
instances explicitly (`(legacy)`) so you can see at a glance whether
they're still running.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `streamlit: command not found` | Use the venv: `.venv/bin/python -m streamlit run …` (the launch script does this for you) |
| Dashboard reachable from aizh's localhost but not from LAN | `sudo ufw allow 8500/tcp` |
| Chat returns empty results | Ensure the variant's Qdrant container is up (`docker ps \| grep qdrant`) and that something has been indexed (Status page → recent KB scans) |
| Reindex / run.py button does nothing visible | Output goes to `/tmp/folderreorg-dashboard-*.log`. Pages tail those logs in expanders below the buttons. PID stored in `st.session_state`. |
| Subprocesses survive Streamlit restart | By design — they're in their own session group. To kill them: copy the PID from the log expander and `kill -INT <pid>`. |
| Stage subprocess shows in Status but not in Pipeline | The Pipeline page only lists subsets discovered on the NAS via `run._discover_all_entries()`; status uses `pgrep` directly. The two views can diverge briefly during NAS hiccups. |

---

## See also

- [`run-on-aizh.md`](./run-on-aizh.md) — operational runbook (the underlying CLI invocations the dashboard wraps)
- [`knowledge-base.md`](./knowledge-base.md) — KB / chat architecture
- [`unattended-runs.md`](./unattended-runs.md) — tmux / nohup patterns (useful for the dashboard itself if you don't want to run it under tmux)
- [`setup.md`](./setup.md) — first-time host setup
