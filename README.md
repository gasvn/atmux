# AutoTmux

AutoTmux is a terminal dashboard for managing tmux sessions across Slurm
compute nodes. It automatically discovers your running jobs, keeps fast
SSH connections to each node open in the background, and lets you list,
preview, and attach to remote tmux sessions from a single TUI.

## Architecture (v0.7.0)

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

idle_hint = 300     # seconds of tmux quiet before a session is flagged
idle_stale = 3600   # seconds before the flag escalates to the red tier
mouse = "auto"      # "off" restores the terminal's own text selection
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

**Check the shared master's keepalive.** Keepalive options on a multiplexed
session are ignored — the master owns the TCP stream, so its `ServerAlive`
budget, not AutoTmux's, decides how long a stalled network blocks every channel
riding it. A master configured with `ServerAliveInterval 60` and
`ServerAliveCountMax 30` takes half an hour to notice a dead peer, and until it
does, previews, state refreshes, and attaches all hang. Give the master a bound
you can live with:

```
Host login1 login2 login3
  ServerAliveInterval 15
  ServerAliveCountMax 4      # give up after ~60s, not 30 minutes
```

`atmux --gateway-check` reports the effective budget when it is far longer than
AutoTmux's own. Existing masters keep the settings they started with, so
restart them after changing this.

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

### Agent-assisted local deployment runbook

This runbook is intended for a terminal/coding agent configuring AutoTmux on
behalf of a user. The goal is a local multi-login-node client without asking the
user to hand-edit AutoTmux or SSH configuration files.

#### 1. Establish the deployment boundary

- Perform the **local client** installation on the user's laptop/workstation,
  not inside a cluster login-node SSH session. Check the hostname and the
  `SSH_CONNECTION`, `SSH_CLIENT`, and `SSH_TTY` environment variables before
  claiming local deployment is complete.
- Existing login-node mode must remain usable. Do not replace or remove the
  login-side installation.
- Do not edit `~/.ssh/config`, terminate tmux servers/sessions, cancel Slurm
  jobs, or kill an active `atmux` frontend without explicit user permission.
- Preserve dirty Git worktrees. Inspect `git status` before pulling or
  installing from an existing checkout.

#### 2. Install the local client without changing shell configuration

Python 3.10 or newer and an OpenSSH client are required. A checkout-local
virtual environment keeps the installation isolated and lets the user invoke
AutoTmux by absolute path without modifying `PATH`:

```bash
git clone https://github.com/gasvn/atmux.git ~/autotmux
python3 -m venv ~/autotmux/.venv
~/autotmux/.venv/bin/python -m pip install --upgrade pip
~/autotmux/.venv/bin/python -m pip install -e ~/autotmux
~/autotmux/.venv/bin/atmux --version
```

For an existing clean checkout, update with Git rather than cloning over it:

```bash
git -C ~/autotmux status --short
git -C ~/autotmux pull --ff-only origin main
~/autotmux/.venv/bin/python -m pip install -e ~/autotmux
```

#### 3. Verify every existing SSH alias and remote agent

Use the aliases already defined on the **local machine**. For each selected
login node, verify passwordless/background access and the remote agent:

```bash
ssh -o BatchMode=yes login1 true
ssh -o BatchMode=yes login1 'atmux-agent --version'
```

Repeat for `login2`, `login3`, and so on. The local `atmux` version and every
remote `atmux-agent` version should match. On clusters with a shared home and
software environment, install the login-side package only once. Otherwise,
install the same version on each login node.

If `atmux-agent` works interactively but is not in the non-interactive SSH
`PATH`, determine its absolute login-side path. Do not modify shell startup
files just for AutoTmux; enter that absolute path in the TUI's **Remote agent
command (advanced)** field instead.

#### 4. Configure gateways through the TUI

Launch the connection manager explicitly:

```bash
~/autotmux/.venv/bin/atmux --connections
```

Then:

1. Select every desired login alias with Space.
2. Enter undiscovered aliases in **Additional SSH aliases**.
3. Keep `atmux-agent` as the remote command, or enter the absolute path found
   in the previous step.
4. Choose **Test** and require a `✓` plus latency for every intended gateway.
5. Choose **Save** or press `Ctrl+S`.

The TUI owns and writes `~/.config/autotmux/connections.json`; neither the user
nor the assisting agent needs to edit it manually. Press `g` in the dashboard
to change the pool later.

#### 5. Validate and hand off

```bash
~/autotmux/.venv/bin/atmux --gateway-check
~/autotmux/.venv/bin/atmux
```

Acceptance criteria:

- `--gateway-check` reports every intended alias healthy with a latency;
- the dashboard shows `login--HOST` and the expected compute-node sessions;
- ordinary login-node `atmux` still starts in native login mode;
- no SSH password prompt appears during background refreshes;
- after an upgrade, any already-running frontend is exited normally and
  relaunched before testing new interactive behavior.

Passwordless key users normally skip `--gateway-login`. For MFA or
keyboard-interactive accounts, save the pool first and then run:

```bash
~/autotmux/.venv/bin/atmux --gateway-login
```

For a non-persistent smoke test, the agent may use explicit gateways without
writing connection state:

```bash
~/autotmux/.venv/bin/atmux \
  --gateway login1 \
  --gateway login2 \
  --gateway login3
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

[notify]
# Reminders sent by the daemon on the login node, so they still arrive when
# the dashboard is closed. Off until a webhook is set.
enabled     = true         # master switch for every reminder route
desktop     = true         # notification on the machine running the TUI
webhook_url = ""           # Slack incoming webhook, or anything accepting
                           # {"text": "..."} (Discord, Teams, ntfy, relays)
lead_time   = 3600         # warn this long before a job hits its time limit
idle_notify = 300          # warn when a session stops producing output (0 = off)
idle_cooldown = 3600       # shortest gap between notices for one session
timeout     = 10           # seconds to wait for the webhook

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
│ squeue jobs panel  (switch view with `j`)   │
└─────────────────────────────────────────────┘
```

### More than one place

`[client].gateways` is **one cluster's interchangeable entry points**, not "my
servers". AutoTmux races them and keeps the first valid reply, because k6 and
k7 are two doors into the same machines. Put an unrelated host in that list and
it will sometimes win the race — and since it knows nothing about your compute
nodes, they all vanish from the table until the next fetch lands elsewhere.

Somewhere else goes in its own cluster. Each cluster is raced internally and
the results are merged into one table:

```
                 raced within                merged across
 ┌──────────────────────────────┐  ┌────────────────────────────────┐
 main:  k6  k7  k8  b8  kempner │  │  holygpu8a17504   4gpu         │
 lab:   my-workstation          │→ │  holygpu8a19301   sweepA       │
 other: ol1  ol2                │  │  login:my-workstation  notes   │
 └──────────────────────────────┘  │  ol-gpu02         eval         │
                                   └────────────────────────────────┘
```

A standalone machine with no Slurm is simply a cluster of one: it contributes
its own tmux sessions as a single row. Attach, preview, `x`, notes and the
idle column all work on it exactly as they do on a compute node.

Try it for one run:

```sh
atmux --cluster lab=my-workstation --cluster other=ol1,ol2
```

Keep it:

```toml
[client]
gateways = ["k6", "k7", "k8", "b8", "kempner"]   # the primary cluster

[client.clusters]
lab   = ["my-workstation"]
other = ["ol1", "ol2"]
```

A cluster that needs its own settings takes the table form instead:

```toml
[client.clusters.zgx]
gateways = ["zgx"]
# Where atmux-agent lives is a property of the machine. `ssh host <cmd>` runs
# non-interactively and gets a bare PATH -- on Ubuntu that excludes
# ~/.local/bin, so a venv install needs its absolute path here.
agent_command = ["/home/me/.local/venv/atmux/bin/atmux-agent"]
# "" means "manage your own SSH master". Set it when the global control_path
# points at an MFA helper's socket that this machine will never have.
control_path = ""
```

`gateways` stays the primary cluster rather than becoming one entry among
many, so a client that predates clusters still sees one coherent cluster
instead of a race between unrelated machines.

Each cluster needs `atmux-agent` reachable on its login nodes. AutoTmux is not
on PyPI — install it from the repo, and on a machine without root a venv keeps
it self-contained:

```sh
ssh my-workstation 'python3 -m venv ~/.local/venv/atmux &&
  ~/.local/venv/atmux/bin/pip install git+https://github.com/gasvn/atmux'
```

Then point that cluster's `agent_command` at
`~/.local/venv/atmux/bin/atmux-agent` as above. Nothing else is needed: the
daemon starts itself on first contact, and on a host with no `squeue` it says
so once and reports that machine's tmux sessions alone. Node names normally stay as they are; if two clusters both have a
`gpu1`, the second one shows as `gpu1--lab` — the first cluster to claim a name
keeps it, so adding a cluster never renames rows you already know. A cluster
with no reachable entry point gets one visible row carrying the error rather
than quietly disappearing, and the subtitle says `⚠ cluster unreachable: lab`.

#### Editing clusters from the dashboard

`g` manages all of them. The **Cluster** row at the top picks which one you are
editing; the alias list, the extra-aliases field and the agent command below it
all follow that choice, and switching away keeps your edits.

```
 Cluster  main            ▼   new cluster name        Add     Remove
 ┌──────────────────────────────────────────────────────────────────┐
 │ ▐X▌ k6      ▐X▌ k7     ▐X▌ k8     ▐X▌ b8    ▐ ▌ zgx             │
 └──────────────────────────────────────────────────────────────────┘
```

Type a name and press **Add** for a new cluster, then select its login nodes.
**Remove** deletes the one you are on — it is disabled for the primary cluster,
which is `gateways` and has no file shape that can express its absence.
Emptying a cluster's alias list also deletes it. **Test** probes the cluster you
are editing, using that cluster's own `agent_command` and `control_path`.

Settings the dialog does not show — `control_path`, and any `agent_command`
belonging to a cluster you did not open — are carried through a save untouched.

### Layout

`z` cycles which of those panes are on screen, and remembers the choice for
next time. The default spends 44% of the width on the preview and up to 14
lines on the queue, which is the wrong shape on a small terminal or whenever
the answer is in the table:

```
  split          wide           table          jobs
┌─────┬─────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│table│prev.│  │   table   │  │           │  │           │
├─────┴─────┤  ├───────────┤  │   table   │  │   jobs    │
│   jobs    │  │   jobs    │  │           │  │           │
└───────────┘  └───────────┘  └───────────┘  └───────────┘
```

Four presses always return to where you started, so there is no second key to
remember. A hidden preview also stops being captured — no SSH round trip per
tick for a pane nobody can see. In `jobs` the arrow keys scroll the queue,
since the table is not there to want them.

| Key | Does | Acts on |
| :-- | :--- | :------ |
| **Enter** / click | Attach to the tmux session | selected row |
| **s** | Open a plain SSH shell | selected row's node |
| **t** | Open / attach a local tmux session | this machine |
| **o** | Attach in a separate window, keeping this table open | selected row |
| **e** | Label the session with what it is for | selected row |
| **n** | Create a named tmux session (detached) | selected row's node |
| **x** | Kill the selected session — asks first | selected row |
| **v** | Read its output including scrollback, without attaching | selected row |
| **k** | Toggle Slurm auto-renew before the walltime ends | selected row's job |
| **j** | Switch the bottom panel: running / pending jobs | all jobs |
| **z** | Cycle the layout: split → wide → table → jobs | whole screen |
| **g** | Manage clusters and their login nodes | whole session |
| **r** | Refresh now (the table also refreshes on its own) | whole table |
| **↑ / ↓** | Move the selection | table |
| **?** | Show this list in the app | — |

`Enter` and `o` open the same thing and differ only in *where* it lands:
`Enter` takes over this terminal and returns you to the table on exit, while
`o` leaves the table up and opens the session beside it. Inside tmux that is
a new tmux window; on macOS outside tmux it is a new terminal window, opened
through the same `atmux://` handler the chat links use — so it also raises the
window a session is already showing rather than opening a second client on it.
Without that handler, or on other platforms, `o` says so and attaches in place.

`s` differs on the other axis: it opens a plain login shell on the node rather
than a tmux session, so it is gone when you exit.

### Creating and killing sessions

`n` creates a detached session on the selected row's node — detached, so it
never steals the terminal you are in. Names are held to letters, digits and
`_ @ + -`: tmux addresses windows and panes with `:` and `.`, so a session
carrying either could never be targeted again.

`x` kills the selected session. It asks first, and the destructive answer is
not the default — `Esc` and `n` both decline, only `y` proceeds. These
sessions exist precisely because they outlive the connection to them, so what
is inside one is not recoverable.

A tmux error is not treated as a network failure: "session not found" leaves
the node's circuit breaker alone, where counting it as a broken link would
take previews and attaches down with it. A failed command is never retried
either — unlike a preview, which is a read that costs nothing to repeat, a
retry here could create a second session or kill one you had since recreated.
| **F12** | Restore the outer tmux after a killed client | surrounding tmux |
| **q** | Quit | AutoTmux |

Rarely-used keys (`r`, `t`) are kept out of the footer so the visible row stays
readable on a narrow terminal; `?` lists everything.

### Selecting text with the mouse

Mouse reporting is what lets a click attach to a session — and it is also what
stops the terminal doing its own selection, because the clicks go to AutoTmux
instead. Reporting is on locally and off over SSH by default.

To select and copy normally, either hold the terminal's bypass modifier while
dragging (Option in iTerm2 and Terminal.app, Shift in kitty/Alacritty/GNOME
Terminal), or give up click-to-attach and use `Enter`:

```toml
[client]
mouse = "off"       # "auto" (default) | "on" | "off"
```

`atmux --no-mouse` and `atmux --mouse` still override it for a single run.

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

### Idle session hints

tmux records when each session last saw activity, so the daemon reports it for
free alongside the session list. A session quiet for more than five minutes
gets a coloured dot and its age in the STATUS column — yellow up to an hour,
red beyond it:

```
IDLE   NODE              SESSION     LEFT   LOAD    STATUS
● 15m  holygpu8a11104    train ·2    1d23h  30.5/1
● 2h   holygpu8a11401    sweep       4h02   6.3/1
       holygpu8a17504    build       6h20   4.7/1   DEGRADED: connect timeout
       login:holylogin06 <shell>     -      14.6/1
```

`IDLE` leads because `STATUS` is the first column a narrow terminal truncates,
so a hint parked at the far right is invisible in exactly the crowded tables
where it helps. The other cells are written for width too: a login node drops
its cluster domain, `LEFT` keeps the magnitude of a walltime rather than the
second it ends on, and `LOAD` carries load and core count together because
neither number means much without the other. The window count rides on
SESSION as `·2`, shown only when a session has more than one -- a column
of its own held the constant `1` on every row. `STATUS` follows the same
rule: it stays empty while a row is healthy, so the eye lands on the row
that is not.

Idle time is measured against the *node's* clock, sampled in the same command
as the activity stamps, so it stays correct when the laptop and the cluster
disagree about the time. The dot is decoration only: the session name stays
exactly what `Enter` attaches to. Both thresholds are `[client]` settings —
`idle_hint` and `idle_stale`.

### Session notes

Session names are chosen for typing, not for reading: `tu_debug` and
`tu_improve` say nothing about which run matters right now. Press `e` on a row
to say what it is for. The note appears in **STATUS**, which is blank whenever
a row is healthy, so it costs no width — and a real warning always wins, since
the note must never be the reason a `DEGRADED` line went unseen.

Notes are keyed by **session name, not node**: a renewed batch job comes back
on whatever node Slurm had free, and a note tied to the old node would vanish
at exactly the moment the run it describes is still going. They live in
`~/.config/autotmux/notes.json`. The layout chosen with `z` is remembered
separately, in `~/.config/autotmux/layout.json` — kept out of `config.toml`
on purpose, since a keypress should not rewrite a hand-maintained file.

### Row order

Rows are grouped by how much they want a decision, then by node and session:

| | |
| :--- | :--- |
| offline / degraded | something is broken |
| **just went quiet** | a run that has probably finished or wedged — the decision worth making |
| working | |
| quiet for hours | not news any more; kept below live work |
| `<shell>` placeholders | not anybody's work |

The tiers are deliberately coarse, so a row changes place at most twice per
quiet spell. A table that re-sorts while the cursor is in it would be worse
than one merely ordered badly.

### Idle-session and job-expiry reminders

Two things are worth being told about without watching the dashboard.

**A session stopped producing output.** That is the observable end of a run:
the work finished, or it wedged. After `idle_notify` seconds of silence the
daemon says so:

```
AutoTmux: tmux session train on holygpu8a11104 (job sweep) has shown no
output for 15m — it has probably finished or stalled.
Last line: Epoch 40/40 done — checkpoint saved to runs/sweep/final.pt
```

The quoted line is what makes the notice actionable: `Epoch 40/40 done` and
`CUDA out of memory` are the same event to the idle check and completely
different to you. It is taken once per quiet spell, not per poll, and a node
too busy to answer the capture still gets its notice — just without the line.

It does mean one line of terminal output leaves the cluster, so it is a
separate switch from the notice itself:

```toml
[notify]
idle_tail = false          # notice only, no quoted output
```

What is quoted is the last line with words in it — rules, borders, spinners
and bare prompts are stepped over — stripped of colour, cursor and title
sequences and capped at 120 characters. A progress bar reports its final state
rather than its first, because the redraws are what the capture sees.

A full-screen program pins its furniture to the bottom of the screen, so the
literal last line is its status bar whatever it has been doing. A status block
sitting below the pane's last horizontal rule and built out of box glyphs
rather than words is skipped, which is what turns

```
⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
```

into

```
· Philosophising… (3m 33s · ↓ 6.3k tokens)
```

Both conditions are required: a block below a rule that reads like output —
the rows under a table header, say — is kept, because discarding a real last
line is worse than quoting a status bar.

Announced once per quiet spell, and re-armed as soon as the session produces
output again, so a long-lived job is not re-announced every poll while one
that wakes and stalls again is. Only sessions on compute nodes count: a shell
left open on a login node or the laptop is idle by design. `idle_notify = 0`
turns it off without disabling the webhook that expiry reminders share.

#### Clickable reminders (macOS)

With `attach_link = true`, each reminder carries an **Attach** link that opens
the session it is about:

```
… has probably finished or stalled.  <atmux://attach/holygpu8a11104/train|Attach>
```

Install the handler that resolves the scheme once per Mac:

```bash
contrib/install-url-handler-macos.sh          # or pass an explicit atmux path
```

It builds a small applet in `~/Applications`, points it at your `atmux`, and
registers `atmux://` with LaunchServices. The first click asks for permission
to control iTerm2 (or Terminal.app); that consent is what lets a link open a
window at all. Re-running the installer can ask again, because the applet is
signed ad-hoc and macOS treats each build as a new program.

Clicking a link for a session you are **already attached to** raises that
window instead of opening a second one — two tmux clients on one session share
a size, so a duplicate silently shrinks whichever window was larger. On iTerm2
this works for any attach, including ones started from the dashboard: atmux
tags its window with an `OSC 1337 SetUserVar` escape as it attaches and clears
it on the way out. Terminal.app has no equivalent, so there reuse only finds
windows the handler itself opened.

The link is untrusted input — anyone who can post to the channel can craft one
— so `atmux --open-url` validates the node and session against a conservative
character set and passes them on as argv. Nothing from a URL reaches a shell
before that check. Every click appends one line to
`/tmp/atmux-url-handler.log`, which is the only place a failure shows up: a
link that cannot open leaves the terminal in the foreground and nothing else.

Leave `attach_link = false` (the default) if you read reminders anywhere the
scheme is not installed; a dead link is worse than none.

### Reading a session without attaching

`v` opens the selected session's output with its scrollback, scrolled to the
end. The preview beside the table is one screen — that is all a poll should
ever pay for — but working out why something died usually means reading
further back, and attaching to look resizes the session to your terminal and
disturbs whatever is still running in it.

A full-screen program (an editor, another TUI) has no scrollback to show: it
draws on the alternate screen, so one screen is genuinely all there is.

### Job start notices

The daemon says when a job is nearly over and when a session has gone quiet;
`job_start` closes the third case — something you queued has finally got a
node:

```
AutoTmux: Slurm job train (4172318) is now running on holygpu8a11104.
```

The first complete poll after a daemon starts is *seeded* rather than
announced, or restarting would announce every job that happened to be running
— four times over, once per login node. A job that starts while every daemon
is down therefore goes unannounced, which is the right way round. Set
`job_start = false` to turn it off; it is separate from the others because it
fires on good news rather than on something wanting attention.

### Job expiry reminders

Point `[notify].webhook_url` at a Slack incoming webhook — or anything that
accepts `{"text": "..."}`, such as Discord, Teams, ntfy, or a relay into
WhatsApp/SMS — and the daemon sends one message per job as it enters its final
`lead_time` seconds:

```
AutoTmux: Slurm job train (4172318) on holygpu8a11104 ends in 58m.
```

It runs on the login node, so reminders arrive whether or not the dashboard is
open. Each job is announced once; a job whose remaining time Slurm cannot
report is skipped rather than guessed at, and a failed POST is retried on the
next poll instead of being silently dropped.

There are two independent routes, so no webhook is needed to be reminded:

| Route | Where it appears | Needs |
| :--- | :--- | :--- |
| `desktop` | Notification Centre on macOS, `notify-send` on Linux — on whichever machine runs the TUI | nothing |
| `webhook_url` | Slack/Discord/Teams/ntfy, sent by the login-node daemon | a URL |

`desktop` is on by default and covers the "I'm at my laptop" case; the webhook
covers "I'm away from it". The dashboard also shows its own banner whenever
reminders are enabled — `desktop = false` silences only the OS popup. Announced
JobIDs are remembered under the runtime dir, so restarting `atmux` does not
re-announce a job you have already been told about.

#### How duplicates are prevented

A daemon runs on every login node and they all watch the same `squeue`, so
they reach the same conclusion in the same second. Two fences stop that
becoming one message per login node:

1. **In memory, per daemon** — a job or session already announced is not
   announced again until it changes: a quiet session re-arms once it produces
   output, a job when it leaves the queue. Lost on restart, by design.
2. **On shared home** — `~/.config/autotmux/claims/`, one file per notice,
   created with `O_CREAT|O_EXCL`. Whichever daemon creates it first is the one
   that posts; the rest see it exists and stay quiet. Each file carries its own
   TTL: `idle_cooldown` for a quiet session, seven days for a job.

Claims are files rather than entries in one locked record because `flock` over
an NFSv3 home returns `ENOLCK` under contention — with four daemons racing it
failed every time, each fell through to "send anyway", and one quiet session
produced four identical messages in the same second. `O_EXCL` creation does not
involve the NFS lock manager; raced from four login nodes at once it yields
exactly one winner.

If the claim directory cannot be created at all, the notice is still sent —
a duplicate is a smaller harm than a silence, and the daemon log says so.

`idle_cooldown` is therefore the real volume knob. Raising it does not lose a
distinct event, only repeats about a session that keeps going quiet:

| `idle_cooldown` | messages, measured over one 37-hour stretch |
| :--- | :--- |
| 1h (default) | 51 |
| 2h | 37 |
| 4h | 29 |
| 8h | 24 |

#### Setting up a Slack webhook

Create the endpoint at <https://api.slack.com/apps>:

1. **Create New App** → **From scratch**, name it, pick the workspace.
2. **Incoming Webhooks** in the sidebar → turn **Activate Incoming Webhooks**
   on (it is off by default).
3. **Add New Webhook to Workspace** at the bottom → choose the channel → **Allow**.
4. Copy the generated `https://hooks.slack.com/services/…` URL.

None of the App Credentials (Client Secret, Signing Secret, Verification Token)
are needed. Those are for OAuth and for verifying requests Slack sends *you*;
a webhook is a plain POST to that one URL.

**Put the config where the daemon runs, not where the TUI runs.** The two
routes read different machines:

| Route | Config file lives on | Read by |
| :--- | :--- | :--- |
| `desktop` | the machine you run `atmux` on | the TUI |
| `webhook_url` | the **login node** | that node's daemon |

Sending from the login node is what lets a reminder arrive after you close the
dashboard. On a cluster with a shared home, writing it once covers every login
node — but each node's daemon only picks it up when **that** daemon restarts:

```bash
# on a login node
install -d -m 700 ~/.config/autotmux
cat > ~/.config/autotmux/config.toml <<'EOF'
[notify]
enabled     = true
lead_time   = 3600
webhook_url = "https://hooks.slack.com/services/..."
EOF
chmod 600 ~/.config/autotmux/config.toml
atmux-daemon restart
```

The URL is a credential: anyone holding it can post to that channel. Keep the
file mode `600`, never commit it, and prefer passing it through an environment
variable over a command line on a shared login node — `/proc/<pid>/cmdline` is
world-readable there while `environ` is not.

To confirm delivery without waiting for a job to age:

```bash
python3 -c 'import os
from autotmux import notify, config
print(notify.post(config.load_notify()["webhook_url"], "AutoTmux test", 10))'
```

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

## From a phone or a tablet

`atmux-web` serves the dashboard to a browser. Not a second dashboard — the
real one: it runs `atmux` on a pseudo-terminal and streams that terminal to
the page, so every key, every layout mode and `attach` itself all work, and
anything added later arrives without touching this code.

```sh
atmux-web                       # binds 127.0.0.1:7681 only
tailscale serve --bg 7681       # HTTPS, reachable only inside your tailnet
```

Then open `https://<your-machine>.tail-scale.ts.net/` and, on iOS, *Add to Home
Screen* — it opens full-screen with its own icon. Nothing here authenticates a
caller, so **never bind it to `0.0.0.0`**: anything that can open the port gets
a shell. Loopback plus Tailscale is the whole security model. (If your tailnet
has no HTTPS certificates, `tailscale serve --bg --http=8080
http://127.0.0.1:7681` still works — the traffic is WireGuard-encrypted either
way, but a PWA install wants HTTPS.)

Dependencies: none. `pty`, `http.server` and a small WebSocket implementation
are all in the standard library, and xterm.js is vendored in the package
rather than fetched from a CDN, so the page works on a device with no route
off the tailnet.

### Touch

atmux is a table you steer with arrows and act on with single letters, so the
phone gets a keypad rather than a keyboard — xterm.js has no touch gesture
support at all ([#5377](https://github.com/xtermjs/xterm.js/issues/5377), open
and unassigned), and without this there is no way to send an arrow, `Esc` or
`Ctrl-C` from a touch screen:

```
  ↑     ↓     ⏎ attach     ←     →     esc    q
 ────────────────────────────────────────────────
  nav  atmux  tmux                    A−  A+  ⌨
```

- **nav** is the default: arrows repeat when held, so a long table is one
  press rather than twenty taps.
- **atmux** carries every bound key (`s o t v e n x k j z g r ?`). A test
  fails if a binding is ever added without a button, or a button added for a
  key nothing binds.
- **tmux** is for once you have attached — including `detach`, which is
  `Ctrl-B d` and is otherwise unreachable without a keyboard.
- **A− / A+** and pinch-to-zoom change the font size, and it is remembered.
  Pinch deliberately zooms the *font* and not the page: a zoomed viewport
  leaves you panning a grid that no longer fits.
- **⌨** raises the software keyboard, which is otherwise kept down. The
  terminal keeps focus either way (`inputmode="none"`), so an iPad with a
  hardware keyboard behaves like a desktop and never loses half its screen to
  a keyboard it does not need.

Safari on iPadOS reports `Ctrl-C` from a hardware keyboard as keyCode 13
(Enter). xterm.js fixed this in
[PR #5742](https://github.com/xtermjs/xterm.js/pull/5742), merged to master
for the 7.0.0 milestone, but no published build carries it — 6.0.0 is latest
and even the beta has no such special case — so the page handles it itself.

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
