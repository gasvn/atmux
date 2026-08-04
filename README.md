# AutoTmux

AutoTmux is a terminal dashboard for managing tmux sessions across Slurm
compute nodes. It automatically discovers your running jobs, keeps fast
SSH connections to each node open in the background, and lets you list,
preview, and attach to remote tmux sessions from a single TUI.

## Architecture (v0.6.1)

AutoTmux is split into two pieces:

- **`atmux-daemon`** (legacy alias: **`atd`**) — a small background daemon
  (`src/autotmux/daemon.py`) that:
  - Polls `squeue -u $USER` every 30 s to discover allocated nodes.
  - Maintains a long-lived SSH ControlMaster connection per node so subsequent
    background probes return instantly without re-authenticating. Interactive
    terminals use a separate short-lived master so preview/session payloads
    can never queue ahead of keystrokes.
  - Uses bounded `ssh -O check` probes plus SSH server keepalives to detect dead
    masters without mistaking an overloaded compute node for a broken one;
    confirmed failures use exponential backoff (30 s → 600 s capped).
  - Lists tmux sessions on every alive node every 15 s.
  - Captures a `tmux capture-pane` snapshot for every (node, session) every
    120 s so the frontend has something to show *immediately* on startup.
  - Refreshes `squeue -l` and `squeue -l --start` raw text for the jobs
    panel.
  - Records `last_error` per node so the frontend can show *why* a node is
    offline.
  - Writes state and snapshots atomically under the node-local runtime directory.
  - Caps SSH, Slurm, snapshot, keep-alive, and frontend I/O worker pools so an
    uninterruptible dependency degrades a slot instead of growing an unbounded
    backlog or preventing shutdown.
  - Always includes `localhost` as a node so local tmux sessions show up.

- **`atmux`** — the foreground Textual TUI (`src/autotmux/cli.py`). Its normal
  refresh path is passive: it reads the daemon's JSON state and renders it.
  Live pane previews and explicit actions (`Enter` / `s` / `o`) use SSH; the
  explicit keep-alive toggle (`k`) queries Slurm once with `scontrol`.

This split means the frontend never blocks on `squeue` or SSH, and tmux
session listings refresh in the background without any user-visible work.

AutoTmux 0.5+ includes an optional third piece without changing that native mode:

- **`atmux-agent`** — a bounded, one-request SSH-stdio bridge. A laptop can
  query the existing daemon on several login nodes, request previews, update
  keep-alive intent, and launch an interactive attach. It opens no listening
  port and receives no copied SSH key.
- **Local gateway pool** — the locally-running `atmux` races login nodes on
  first use, keeps a sticky low-latency route, reuses a local SSH
  ControlMaster, applies per-gateway circuit breaking, and automatically tries
  another login node after a transport loss. Last-good state and previews are
  cached locally so an outage does not blank the dashboard.
- **Connection Manager** (0.6+) — the TUI discovers existing OpenSSH aliases,
  tests latency, and persists the selected pool, so local deployment needs no
  hand-edited client config.

The original login-node deployment remains the default when no gateways are
configured. In `mode = "auto"`, an `atmux` launched through SSH also stays in
native login-node mode, so the same installation can support both workflows.

## Installation

```bash
git clone https://github.com/gasvn/atmux.git
cd atmux
pip install .
```

This installs `atmux`, `atmux-agent`, `atmux-daemon`, and the
backward-compatible `atd` alias, and pulls in Textual 8.x and Rich. Prefer
`atmux-daemon`: some Linux systems already use the generic name `atd` for
their at-job scheduler.

`atmux` will auto-start its daemon module the first time it can't find a running
daemon, so day-to-day usage is just:

```bash
atmux
```

## Local client with multiple login nodes

Install the same AutoTmux version on the local machine and on every login node.
On clusters with a shared home/software environment, one login-side install is
usually enough. Keep using `atmux` normally on a login node; local mode is an
additional deployment, not a replacement.

If the local machine already has passwordless SSH aliases such as `login1`,
`login2`, and `login3`, no config editing is needed. Run:

```bash
atmux
```

On first local use AutoTmux opens **Connections**, discovers literal `Host`
aliases from `~/.ssh/config` (including bounded `Include` files), and lets you:

- select multiple login nodes with Space;
- test every selected gateway and see its latency;
- add an alias which was not discovered;
- save with the button or `Ctrl+S` and connect without restarting the TUI.

Press `g` from the dashboard to reopen Connections at any time, or launch it
directly with `atmux --connections`. The choice is stored in the TUI-owned
`~/.config/autotmux/connections.json`; users do not need to edit it.

The old TOML form remains available for automation and advanced timing overrides:

```toml
[client]
mode = "auto"
gateways = ["login1", "login2", "login3"]  # SSH aliases or user@host

# Optional when a non-interactive SSH shell cannot find atmux-agent:
# agent_command = ["/home/me/.local/bin/atmux-agent"]

connect_timeout = 5
state_timeout = 10
hedge_delay = 0.35
sticky_ttl = 300
backoff_base = 2
backoff_cap = 60
probe_interval = 60
control_persist = 3600
server_alive_int = 15
server_alive_max = 3

# Reuse SSH masters owned by something else instead of opening our own.
# control_path = "~/.ssh/cm-2fa-%n"
```

### Reusing externally managed SSH masters

Some sites keep authenticated login masters alive with a separate helper —
common where every new connection costs an MFA prompt. AutoTmux normally opens
its own masters, which re-triggers that prompt on every background probe and
every attach; because background traffic uses `BatchMode=yes`, the prompt
cannot even appear and the connection simply fails with
`Permission denied (keyboard-interactive)`.

Point `control_path` at the helper's socket to ride it instead:

```toml
[client]
control_path = "~/.ssh/cm-2fa-%n"
```

OpenSSH expands its usual tokens, so `%n` becomes the gateway alias as written
in `gateways`. AutoTmux then uses that one master for every login-gateway
connection — state RPCs, previews, attaches, and the `ProxyCommand` hop to a
compute node alike — with `ControlMaster=no`, so it never creates or takes
ownership of a socket belonging to another tool. Masters to compute nodes are
still AutoTmux's own, since those terminate somewhere the login socket cannot
reach.

With this set, `atmux --gateway-login` reports which masters are missing rather
than trying to open them; renew them with whichever tool owns them.

If login requires MFA, a password, or keyboard-interactive authentication,
bootstrap the login ControlMasters explicitly:

```bash
atmux --gateway-login   # the only path which permits interactive SSH prompts
atmux --gateway-check   # concurrently verify every agent and show latency
atmux                   # local dashboard
```

Background refreshes always use `BatchMode=yes`; an expired MFA session
therefore becomes a visible, bounded gateway failure instead of an invisible
prompt that freezes the TUI. Run `atmux --gateway-login` again to renew it.
Key-only users can normally skip the login command. While the dashboard is
open, `probe_interval` sends a low-frequency ping to standby gateways so their
masters stay authenticated, their login-side daemons stay warm, and their
latency/error scores remain current.

Useful one-shot overrides:

```bash
atmux --gateway login1 --gateway login2  # use these gateways for this run
atmux --login-mode                       # force original native mode
atmux --gateway-mode                     # force configured local mode
```

Local mode displays the laptop as `localhost` and the selected login host as
`login--HOST`; compute-node names are unchanged. Interactive traffic follows:

```text
fast path: local atmux → SSH tunnel through login → compute tmux
fallback:  local atmux → selected login agent → compute tmux
```

The fast path uses one target PTY and a dedicated interactive SSH master, so
keystrokes never queue behind state/preview RPC payloads. AutoTmux establishes
that native OpenSSH path in the background for the most likely targets, but no
Python process relays terminal bytes. Interactive masters expire after five
idle minutes so a slow connection from an old network does not linger for an
hour. The path disables compression, problematic DSCP marking, and (on newer
OpenSSH clients) fixed-rate keystroke padding. It also applies the login account
name learned from the remote daemon, which matters when the laptop username
differs from the cluster username. If the cluster disallows end-to-end SSH
authentication to compute nodes, AutoTmux remembers that result for five
minutes and transparently uses the login agent instead.

Disabling optional keystroke-timing padding favors responsiveness on lossy
links. SSH payloads remain encrypted, but a passive observer may infer typing
timing; remove the `ObscureKeystrokeTiming=no` policy if that privacy tradeoff
is inappropriate for your environment.

If an outer login connection returns SSH's transport status, AutoTmux bypasses
the stale mux and then moves to another healthy login node. A live TCP/SSH
stream cannot migrate between gateways, but the tmux server remains alive on
the compute node and AutoTmux immediately reattaches through the next route.
Read-only state uses hedged requests; mutating keep-alive updates are idempotent
and protected by the existing shared lease, so failover cannot submit duplicate
replacement jobs.

## Daemon control

```bash
atmux-daemon start      # start and wait until the detached daemon is ready
atmux-daemon stop       # graceful SIGTERM
atmux-daemon restart    # stop, then start
atmux-daemon status     # show current pid, freshness, and alive nodes
atmux-daemon logs       # show the most recent 50 log lines
atmux-daemon logs -f    # follow the log
atmux-daemon run        # run in foreground for debugging
```

State / log locations live under the runtime dir `<BASE>` — `$XDG_RUNTIME_DIR/autotmux/`
if available, otherwise `/tmp/autotmux_<uid>/` (see [Configuration](#configuration) below):

| Path | Purpose |
| :--- | :--- |
| `<BASE>/daemon.json` | Daemon state snapshot the frontend reads. Updated every ~10 s. |
| `<BASE>/daemon.log`  | Daemon log (rotated at 1 MB × 3 backups). |
| `<BASE>/daemon.pid`  | Daemon PID. |
| `<BASE>/daemon.pid.lock` | Runtime singleton lock (held for the daemon lifetime). |
| `<BASE>/ctl/cm_<node>` | One ControlMaster socket per node. |
| `<BASE>/interactive-ctl/` | Short-lived, terminal-only ControlMaster sockets. |
| `<BASE>/snapshots.json` | Per-(node,session) tmux pane snapshots. |
| `<BASE>/preview.sock` | Private frontend/daemon preview and network-health IPC. |
| `<BASE>/warm/` | Ownership records for pre-warmed interactive SSH children. |
| `<BASE>/gateway-ctl/` | Local-mode ControlMaster sockets for login gateways. |
| `<BASE>/gateway-state.json` | Last-good local-mode dashboard state. |
| `<BASE>/gateway-snapshots.json` | Last-good local-mode pane previews. |
| `/tmp/autotmux_daemon_<uid>.guard` | Stable singleton guard plus active-runtime metadata. |

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
server_alive_int  = 30     # SSH keepalive interval for masters and attaches
server_alive_max  = 3      # missed keepalives before SSH declares failure
backoff_base      = 30     # initial retry delay after a failed master start
backoff_cap       = 600    # max retry delay
network_backoff_base = 2   # first shared node-network retry delay
network_backoff_cap  = 60  # maximum shared node-network retry delay
warm_orphan_interval = 30  # seconds between identity-safe warm-child sweeps

[keepalive]
enabled        = true      # master switch; existing opt-ins remain stored
lead_time      = 900       # renew this many seconds before expiry
cooldown       = 600       # suppress duplicate submissions after success
max_failures   = 3         # pause after this many consecutive submit failures
submit_timeout = 60        # maximum seconds to wait for sbatch
```

Unknown, non-finite, wrong-type, and out-of-range numeric values are ignored
with a warning in the daemon log.
Restart the daemon to apply changes: `atmux-daemon restart`.

The `[client]` table is read by the local frontend and does not require a
daemon restart. A selection saved through Connections overrides its mode,
gateway list, and agent command while retaining advanced numeric tunables from
TOML. Ordinary SSH/Mosh launches always keep native login-node behaviour
without waiting on local-client files; use `--gateway-mode` explicitly for the
unusual case of running a gateway client there. Outside SSH, `mode = "auto"`
enables configured gateways, `mode = "gateway"` forces them, and `mode =
"login"` keeps native behaviour. CLI flags override both saved sources for one
invocation.

### Runtime files & paths

Runtime state (pid, log, state JSON, snapshots, and ControlMaster sockets)
lives under `$XDG_RUNTIME_DIR/autotmux/` when available (e.g.
`/run/user/<uid>/autotmux/`), falling back to `/tmp/autotmux_<uid>/`.
`$XDG_RUNTIME_DIR` is preferred because it is node-local tmpfs with short
paths — required for SSH ControlMaster sockets to work reliably.
If a custom XDG path is too long for Unix sockets, AutoTmux falls back to the
short `/tmp` base; unusually long hostnames get deterministic hashed socket
names. If systemd removes or replaces the runtime directory beneath a detached
daemon, it releases the stable guard and exits so the frontend can recover with
a clean instance instead of waiting on an unreachable daemon forever.
The stable guard also advertises the active runtime path, so clients launched
from another SSH environment (for example, one without `XDG_RUNTIME_DIR`) still
read the running daemon's state and reuse its existing SSH masters.

**Upgrading from a pre-XDG version:** run `atmux-daemon restart` once after
upgrading. Startup automatically stops any old daemon still running under the
legacy `/tmp` pid file so you don't end up with two daemons.

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
| **k**     | Toggle auto-renew for the selected Slurm job (batch jobs only). |
| **j**     | Toggle the bottom jobs panel between `squeue -l` and `squeue --start`. |
| **r**     | Force-refresh the table from the daemon snapshot. |
| **q**     | Quit. |

When `atmux` itself runs inside tmux, it temporarily hands the outer client
directly to the SSH helper with `detach-client -E`; after detach it automatically
reattaches to the dashboard. Local gateway selections are carried into the
helper automatically. This removes the outer tmux renderer from the terminal
data path entirely. If client handoff is unavailable, AutoTmux falls back to
making the surrounding session transparent so inner prefixes and function keys
pass through. During that fallback the outer status line is hidden and
`escape-time` is leased at
`min(current, 10 ms)`. The last concurrent attach restores every original
setting exactly; press **F12** for emergency recovery after a killed client.

The right-hand pane shows a `tmux capture-pane` preview of the
currently highlighted session. On row change the cached snapshot from
`<BASE>/snapshots.json` is shown immediately; the live
preview probe starts after navigation settles. The frontend requests it over a
private Unix socket; only the daemon opens the background SSH channel. Session
polling, snapshots, and previews share one admission gate and one circuit
breaker per node, so a weak link cannot create a reconnect storm or exhaust the
ControlMaster's channel limit. If a pane stays unchanged, probes back off
gradually (up to 8 s); transport failures use jittered exponential backoff up
to `network_backoff_cap`. The STATUS column and subtitle show network recovery
and cached-preview age instead of leaving an indefinite “Loading” message.

Remote attaches now hand the real terminal directly to OpenSSH. AutoTmux only
pre-establishes a no-PTY master, removing the former Python/PTY relay whose
screen and input buffers could drift apart under backpressure. Reattaching uses
`tmux attach-session -d`, which removes a ghost client left by a stalled TCP
connection before that client's delayed input or stale geometry can affect the
new screen. If the low-latency master returns SSH's transport status, AutoTmux
retries exactly once with `ControlPath=none` and `ControlMaster=no`. During a
truly stuck interactive connection, press Enter and then type `~.` to disconnect
immediately.

The daemon lowers a live remote tmux server's `escape-time` to 10 ms when it is
higher, without editing `tmux.conf`; an already-lower value is preserved. This
removes the default 500 ms ambiguity delay that makes Vim/Alt/function-key
sequences feel stuck, especially when tmux is nested. The STATUS column still
shows `⚠ ESC Nms` if tuning was unavailable and the value remains above 50 ms.

### Keep-alive auto-renew

Press `k` on any remote Slurm row—including `<Start Shell>` or `<offline>`—to
opt that one batch job into renewal. AutoTmux records its batch script and
working directory, then submits a replacement with `sbatch` before the time
limit. Entries are tracked by JobID rather than job name, so same-named jobs are
independent. The STATUS column shows healthy, renewing, paused, disabled, or
stalled renewal state; press `k` twice after correcting a paused job to re-arm
it. Interactive `salloc`/`srun` jobs and `sbatch --wrap` jobs have no reusable
script and are rejected with a visible explanation.

The registry uses a short shared claim for each entry, so daemon instances on
different login hosts cannot submit the same replacement concurrently. An
ambiguous timeout or transport failure also starts the cooldown: this favors
avoiding a duplicate Slurm job when `sbatch` may have accepted the request but
its reply was lost.

## Requirements

- Python 3.10+
- `tmux` installed both locally and on the remote nodes.
- `squeue` (Slurm) on the login host where you run `atmux`.
- SSH key-based access to the compute nodes (`BatchMode=yes` is used).
- For local mode, SSH access to every configured login gateway and the same
  `atmux-agent` version available in its non-interactive command environment.

## Development

```bash
# Install editable
pip install -e .

# Run all non-cluster tests
python -m unittest discover -s tests -t . -v

# Run the opt-in daemon integration tests in an isolated runtime (requires
# squeue/ssh on PATH; use a unique guard path so a real daemon is untouched)
mkdir -p "/tmp/autotmux-integration-$UID"
chmod 700 "/tmp/autotmux-integration-$UID"
AUTOTMUX_RUN_INTEGRATION=1 \
XDG_RUNTIME_DIR=/tmp/autotmux-integration-$UID \
AUTOTMUX_GUARD_FILE=/tmp/autotmux-integration-$UID.guard \
python -m unittest tests.test_daemon_integration -v
```

The `tests/` layout:

| File | What it covers |
| :--- | :--- |
| `test_pure_functions.py` | `read_state` / `read_snapshots` / `build_session_rows` / atomic writes / backoff state machine |
| `test_edge_cases.py` | Adversarial inputs — gone-node cleanup, shell quoting, bounded workers, and master lifecycle races |
| `test_warm_pool.py` | The pre-warm `WarmSlavePool` (uses a fake `ssh` script + `Popen`/`openpty`) |
| `test_frontend_pilot.py` | Headless Textual Pilot tests of the TUI |
| `test_daemon_integration.py` | Real daemon lifecycle + ssh-leak monitoring (auto-skipped without cluster tools) |

CI runs the unit tests on every push (`.github/workflows/test.yml`).

## Notes on v0.4.0

This is a refactor of the previous monolithic curses app. A number of
v0.3.x features (notes, watch mode, Slack alerts, search, in-UI session
creation/kill) are not yet ported to the new architecture. The previous
curses implementation remains available in the git history (commits prior
to the v0.4.0 daemon split) for reference.
