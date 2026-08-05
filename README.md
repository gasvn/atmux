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
│ squeue jobs panel  (toggle with `j`)        │
└─────────────────────────────────────────────┘
```

| Key | Does | Acts on |
| :-- | :--- | :------ |
| **Enter** / click | Attach to the tmux session | selected row |
| **s** | Open a plain SSH shell | selected row's node |
| **t** | Open / attach a local tmux session | this machine |
| **o** | Attach in a new window of the surrounding tmux | selected row |
| **k** | Toggle Slurm auto-renew before the walltime ends | selected row's job |
| **j** | Switch the bottom panel: running / pending jobs | all jobs |
| **g** | Choose which login nodes to route through | whole session |
| **r** | Refresh now (the table also refreshes on its own) | whole table |
| **↑ / ↓** | Move the selection | table |
| **?** | Show this list in the app | — |
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

This reads a screen, not a log, so it answers well for the batch jobs the
notice exists for and poorly for a full-screen program: a session running an
editor or another TUI quotes that program's status bar, because for a TUI the
last line genuinely is the status bar. Click through to the pane for those.

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
