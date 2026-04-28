# Unattended runs — surviving SSH disconnects

How to start a long-running pipeline (`./run.py …`) in a way that survives
SSH disconnects, network drops, laptop sleep, or you closing the terminal.
Essential for any `--batch` run, since those typically take 8–50 hours.

## TL;DR

```bash
ssh aizh
tmux new -s reorg                              # creates a session named "reorg"
cd /home/michael.gerber/folderReorg
./run.py --batch all --source-from-mount       # or any other long-running invocation

# To detach and keep it running:  press  Ctrl-B  then  D
# Safe to log out, close laptop, lose Wi-Fi, etc.

# To re-attach later (from anywhere):
ssh aizh
tmux attach -t reorg
# Detach again with Ctrl-B D, or kill with Ctrl-C
```

That's it for 95 % of cases. The rest of this doc explains the alternatives
and the caveats.

---

## Why this matters

When you SSH into `aizh` and run `./run.py`, the wizard becomes a child of
your SSH session. **If the SSH connection drops, your shell receives
`SIGHUP`, the wizard receives `SIGHUP`, and everything dies.**

Causes of disconnect we've actually seen:
- Laptop went to sleep / closed lid
- Wi-Fi switched networks (home → mobile hotspot)
- Cloudflare WARP / VPN reconnected
- 30-minute idle timeout on a corporate firewall
- You closed the terminal "by accident"

A multi-hour batch left running naked over SSH **will not survive any of
these**. Use one of the three approaches below.

---

## Three approaches

### 1. `tmux` — best for long batches you might want to peek at

A "session manager" that runs your shell in a background process tree
detached from your SSH session. Commands keep running; you can re-attach
from any later SSH connection to see live output.

#### Setup (one-time)

```bash
sudo apt install -y tmux         # only if not already installed
```

#### Start a session and run the batch

```bash
ssh aizh
tmux new -s reorg                # session name "reorg" (any name works)
cd /home/michael.gerber/folderReorg
./run.py --batch all --source-from-mount
```

Now press **`Ctrl-B`** (release), then **`D`** (for "detach"). You'll see:

```
[detached (from session reorg)]
```

Your shell prompt comes back; the wizard keeps running in the background.
You can `exit` the SSH session safely.

#### Re-attach later

```bash
ssh aizh
tmux attach -t reorg             # see live output, scrollback, everything
```

#### Useful while attached

- `Ctrl-B  D` → detach (keep running)
- `Ctrl-B  [` → enter scroll mode (arrow keys / PgUp), `q` to exit
- `Ctrl-B  ?` → list every shortcut
- `Ctrl-C` → send SIGINT to the wizard (aborts the run, saves state)

#### Useful when not attached

```bash
ssh aizh 'tmux ls'                       # list all sessions
ssh aizh 'tmux kill-session -t reorg'    # nuke the session entirely
```

#### Pros / cons

| ✓ | ✗ |
|---|---|
| Live output, scrollback, can re-attach from anywhere | Needs `tmux` installed (one-time `apt install`) |
| Can run multiple commands per session | One extra step (`tmux new -s NAME`) |
| Survives SSH drops, laptop sleep, etc. |   |

---

### 2. `nohup &` — simplest for fire-and-forget

Standard Unix recipe. Logs to a file; no live output once you disconnect.

```bash
ssh aizh
cd /home/michael.gerber/folderReorg
nohup ./run.py --batch all --source-from-mount \
    > /tmp/batch-$(date +%Y%m%d-%H%M).log 2>&1 &
disown
exit                              # SSH connection drops, wizard keeps running
```

Breakdown:
- `nohup` — ignore SIGHUP when SSH disconnects
- `> /tmp/batch-…log 2>&1` — redirect both stdout + stderr to a timestamped log
- `&` — run in the background of the current shell
- `disown` — detach from the shell's job table so even shell exit doesn't kill it

#### Watch progress

```bash
ssh aizh 'tail -f /tmp/batch-*.log'

# or use the status tool:
ssh aizh '/home/michael.gerber/folderReorg/status.py'
```

#### Stop the run

```bash
ssh aizh 'pkill -f "run\.py.*--batch"'
```

(Or use `./status.py` to find the PID and `kill -INT <PID>` for a graceful
SIGINT — the wizard saves state on KeyboardInterrupt.)

#### Pros / cons

| ✓ | ✗ |
|---|---|
| No new tools to install | No live output once detached |
| One line | Log file lives in `/tmp` (cleared on reboot — move to persistent location for long batches) |
| Standard everywhere |   |

---

### 3. `systemd-run` — clean lifecycle, journald logs

Spins up an ad-hoc systemd user-service for the run. Cleaner than `nohup`
because logs go to journald (persistent, structured, queryable) and you
get a proper PID/status tracker.

```bash
ssh aizh
cd /home/michael.gerber/folderReorg
systemd-run --user \
    --unit=folderreorg-batch \
    --working-directory=$PWD \
    ./run.py --batch all --source-from-mount
```

The command returns immediately. The wizard runs as `folderreorg-batch.service`.

#### Monitor

```bash
ssh aizh 'systemctl --user status folderreorg-batch'
ssh aizh 'journalctl --user -u folderreorg-batch -f'   # live tail
ssh aizh 'journalctl --user -u folderreorg-batch --since today --no-pager'
```

#### Stop

```bash
ssh aizh 'systemctl --user stop folderreorg-batch'
```

The wizard receives SIGTERM and saves state cleanly on its way out.

#### Pros / cons

| ✓ | ✗ |
|---|---|
| Persistent journald logs (survive reboot) | No live output unless you `journalctl -f` |
| Real PID + status command | Slightly more typing |
| `systemctl stop` is graceful (SIGTERM with timeout) |   |
| Auto-cleans the unit when the run exits |   |

---

## Important caveats

### `./run.py --auto-run` alone PROMPTS for a subset

`--auto-run` only auto-defaults *prompts that have a default*. The
interactive subset picker (`Pick by number…`) has no default, so a bare
`./run.py --auto-run --source-from-mount` will sit at the picker forever
or loop on bad input. **For unattended runs you almost always need one
of:**

```bash
./run.py --batch all --source-from-mount               # batch driver
./run.py --batch personal --source-from-mount          # one collection
./run.py --batch 1,4,7,10 --source-from-mount          # explicit numbers
./run.py --subset X --collection Y --auto-run --source-from-mount
./run.py --resume --auto-run --source-from-mount       # most-recent partial
```

### Phase 4 (Streamlit review) is SKIPPED

Under `--auto-run`, the human-review step is bypassed entirely. Phase 5
uses the un-edited `rename_plan.csv` with every row pre-set to
`decision=approve`. The LLM's renaming choices land on the NAS without
review. This is what you want for unattended runs — but worth knowing.

### `--batch` implies `--auto-run`

You don't need to pass `--auto-run` explicitly when using `--batch`; it's
forced on. (Trying to do batch with prompts wouldn't work anyway.)

### `--source-from-mount` is strongly recommended for batches

Without it, Stage 1 rsyncs each subset's source from NAS to local SSD
(~60 GB per subset). For a `--batch all` (35 subsets) that's
multi-terabyte writes the local disk can't hold. With
`--source-from-mount`, the wizard reads from `~/nas` (SSHFS) directly
— zero local copy, ~30 min slower per big subset, totally fine for batches.

### Ctrl-C on the OUTSIDE shell does nothing once detached

Once you've detached (tmux's Ctrl-B D, or `nohup &`, or `systemd-run`),
the wizard is no longer attached to your terminal. Pressing Ctrl-C in
your local shell only affects your local shell — not the running batch.
To stop the batch, see the per-method "stop" sections above.

---

## Monitoring while a batch runs

Independent of which approach you used:

```bash
ssh aizh
cd /home/michael.gerber/folderReorg

# One-shot snapshot — what's currently running, what just finished
./status.py

# Live updating dashboard, refreshes every 5 s
./status.py --watch 5

# Dig into the most recent KB scan errors / skipped files
./status.py --errors --root <SUBSET>
./status.py --skipped --root <SUBSET>
```

The CURRENTLY RUNNING section shows the wizard PID, current stage,
which subset, GPU residency. The LAST COMPLETED section shows the
most recent fully-done subset and the most recent clean KB scan.

---

## Recommended pattern for an overnight batch

```bash
# 1. SSH in, start a tmux session, kick off the batch
ssh aizh
tmux new -s reorg
cd /home/michael.gerber/folderReorg
./run.py --batch all --source-from-mount      # or whatever scope you want

# 2. Detach and walk away
#    Press: Ctrl-B  D
#    Then:  exit  (close SSH)

# 3. Any time the next morning, check status
ssh aizh '/home/michael.gerber/folderReorg/status.py'

# 4. If you want to see live progress, re-attach
ssh aizh
tmux attach -t reorg

# 5. After it finishes, kill the tmux session
ssh aizh 'tmux kill-session -t reorg'
```

---

## Common gotchas

| Symptom | Fix |
|---|---|
| `tmux: command not found` | `sudo apt install -y tmux` |
| `tmux attach -t reorg` says "no sessions" | The wizard either finished cleanly or died — check `./status.py` and the journal. The session ends automatically when its only shell exits |
| `nohup` log file is in `/tmp` and got cleaned on reboot | Use `~/folderreorg-batch.log` instead of `/tmp/...` next time |
| `systemctl --user start` fails with "Unit not found" | You need `loginctl enable-linger <user>` once (see [`setup.md`](./setup.md) §1.5) |
| Batch was running, I closed SSH, now `./status.py` shows nothing | If you didn't use tmux/nohup/systemd-run, the batch died with SIGHUP. State files persist any completed stages — `./run.py --resume --auto-run --source-from-mount` to continue |
| Re-attached to tmux, see no scrollback | Press `Ctrl-B  [` to enter scroll mode, then arrow keys / PgUp. `q` to exit |
| Run is stuck and `./status.py` shows GPU is idle | Wizard might be waiting on a prompt that wasn't auto-defaultable. Re-attach via tmux to see, or check `journalctl --user -u folderreorg-batch` |
| Multiple tmux sessions with the same name | Use unique names: `tmux new -s reorg-personal`, `tmux new -s reorg-360f` |

---

## See also

- [`setup.md`](./setup.md) — first-time host setup (includes `loginctl enable-linger`)
- [`run-on-aizh.md`](./run-on-aizh.md) — full operational runbook (wizard / batch flags reference)
- [`knowledge-base.md`](./knowledge-base.md) — KB indexer / chat UI specifics
