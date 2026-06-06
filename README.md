# AutoTmux

AutoTmux is a terminal dashboard for managing tmux sessions across Slurm
compute nodes. It automatically discovers your running jobs, keeps fast
SSH connections to each node open in the background, and lets you list,
preview, and attach to remote tmux sessions from a single TUI.

## Architecture (v0.4.0)

AutoTmux is split into two pieces:

- **`atd`** — a small background daemon (`src/autotmux/daemon.py`) that:
  - Polls `squeue -u $USER` every 30 s to discover allocated nodes.
  - Maintains a long-lived SSH ControlMaster connection per node so subsequent
    `ssh`/`tmux attach` calls return instantly without re-authenticating.
  - Probes each master with a real `ssh true` health check and tears down
    masters that have hung; uses exponential backoff (30 s → 600 s capped) for
    nodes that keep failing so it doesn't hammer them.
  - Lists tmux sessions on every alive node every 10 s.
  - Captures a `tmux capture-pane` snapshot for every (node, session) every
    60 s so the frontend has something to show *immediately* on startup.
  - Refreshes `squeue -l` and `squeue -l --start` raw text for the jobs
    panel.
  - Records `last_error` per node so the frontend can show *why* a node is
    offline.
  - Writes all of this atomically (tmp file + `os.replace`) to
    `/tmp/autotmux_daemon_<uid>.json` and `~/.autotmux_snapshots.json`.
  - Always includes `localhost` as a node so local tmux sessions show up.

- **`atmux`** — the foreground Textual TUI (`src/autotmux/cli.py`). It is **purely
  passive**: it reads the daemon's JSON state file (a few ms, no network)
  and renders it. The only network calls the frontend ever makes are
  `tmux capture-pane` for the live preview and the actual `ssh` you trigger
  by pressing Enter / `s` / `o`.

This split means the frontend never blocks on `squeue` or SSH, and tmux
session listings refresh in the background without any user-visible work.

## Installation

```bash
git clone https://github.com/gasvn/atmux.git
cd atmux
pip install .
```

This installs two console scripts (`atmux` and `atd`) and pulls in the
`textual` and `rich` runtime dependencies.

`atmux` will auto-start `atd` the first time it can't find a running
daemon, so day-to-day usage is just:

```bash
atmux
```

## Daemon control

```bash
atd start      # start daemon in background (idempotent)
atd stop       # graceful SIGTERM
atd restart    # stop, then start
atd status     # show current pid, last update, alive nodes
atd run        # run in foreground for debugging
```

State / log locations live under the runtime dir `<BASE>` — `$XDG_RUNTIME_DIR/autotmux/`
if available, otherwise `/tmp/autotmux_<uid>/` (see [Configuration](#configuration) below):

| Path | Purpose |
| :--- | :--- |
| `<BASE>/daemon.json` | Daemon state snapshot the frontend reads. Updated every ~10 s. |
| `<BASE>/daemon.log`  | Daemon log (rotated at 1 MB × 3 backups). |
| `<BASE>/daemon.pid`  | Daemon PID. |
| `<BASE>/ctl/cm_<node>` | One ControlMaster socket per node. |
| `<BASE>/snapshots.json` | Per-(node,session) tmux pane snapshots. |

## Configuration

Daemon timings can be tuned via `~/.config/autotmux/config.toml` (optional —
sane defaults apply if absent). Either a `[daemon]` table or flat keys work:

```toml
[daemon]
squeue_interval   = 30     # seconds between squeue polls
session_interval  = 15     # seconds between tmux list-sessions polls
snapshot_interval = 120    # seconds between pane-capture snapshots
health_interval   = 30     # seconds between ControlMaster health checks
connect_timeout   = 8      # ssh ConnectTimeout
backoff_base      = 30     # initial retry delay after a failed master start
backoff_cap       = 600    # max retry delay
```

Unknown keys, or keys whose value isn't a number, are ignored with a warning in the daemon log.
Restart the daemon to apply changes: `atd restart`.

### Runtime files & paths

Runtime state (pid, log, state JSON, snapshots, and ControlMaster sockets)
lives under `$XDG_RUNTIME_DIR/autotmux/` when available (e.g.
`/run/user/<uid>/autotmux/`), falling back to `/tmp/autotmux_<uid>/`.
`$XDG_RUNTIME_DIR` is preferred because it is node-local tmpfs with short
paths — required for SSH ControlMaster sockets to work reliably.

**Upgrading from a pre-XDG version:** run `atd restart` once after upgrading.
`atd start` automatically stops any old daemon still running under the legacy
`/tmp` pid file so you don't end up with two daemons.

## Dashboard layout

```
┌────────────────────┬────────────────────────┐
│ NODE / SESSION list│ live tmux capture-pane │
│ (incl. localhost)  │ preview of selected    │
├────────────────────┴────────────────────────┤
│ squeue jobs panel  (toggle with `j`)        │
└─────────────────────────────────────────────┘
```

| Key       | Action |
| :-------- | :----- |
| **↑ / ↓** | Navigate the session list. |
| **Enter** | Attach to the selected tmux session via SSH (or local tmux). |
| **s**     | Open a raw SSH shell on the selected node. |
| **t**     | Open / attach to a local tmux session (`autotmux_local`). |
| **o**     | Open the attach in a new tmux window of the surrounding tmux. |
| **j**     | Toggle the bottom jobs panel between `squeue -l` and `squeue --start`. |
| **r**     | Force-refresh the table from the daemon snapshot. |
| **q**     | Quit. |

The right-hand pane shows a `tmux capture-pane` preview of the
currently highlighted session. On row change the cached snapshot from
`/tmp/autotmux_snapshots_<uid>.json` is shown immediately; the live
preview catches up within ~0.5 s.

## Requirements

- Python 3.8+
- `tmux` installed both locally and on the remote nodes.
- `squeue` (Slurm) on the login host where you run `atmux`.
- SSH key-based access to the compute nodes (`BatchMode=yes` is used).

## Development

```bash
# Install editable
pip install -e .

# Run all unit tests (fast — ~5s)
python -m unittest tests.test_pure_functions tests.test_edge_cases \
    tests.test_warm_pool tests.test_frontend_pilot

# Run everything including the daemon integration tests (requires
# squeue/ssh/atd on PATH; ~80s on a real cluster)
python -m unittest discover -v
```

The `tests/` layout:

| File | What it covers |
| :--- | :--- |
| `test_pure_functions.py` | `read_state` / `read_snapshots` / `build_session_rows` / atomic writes / backoff state machine |
| `test_edge_cases.py` | Adversarial inputs — gone-node cleanup, shell quoting, `_write_status` concurrency, deep-probe failure streak |
| `test_warm_pool.py` | The pre-warm `WarmSlavePool` (uses a fake `ssh` script + real `pty.fork`) |
| `test_frontend_pilot.py` | Headless Textual Pilot tests of the TUI |
| `test_daemon_integration.py` | Real daemon lifecycle + ssh-leak monitoring (auto-skipped without cluster tools) |

CI runs the unit tests on every push (`.github/workflows/test.yml`).

## Notes on v0.4.0

This is a refactor of the previous monolithic curses app. A number of
v0.3.x features (notes, watch mode, Slack alerts, search, in-UI session
creation/kill) are not yet ported to the new architecture. The previous
curses implementation remains available in the git history (commits prior
to the v0.4.0 daemon split) for reference.
