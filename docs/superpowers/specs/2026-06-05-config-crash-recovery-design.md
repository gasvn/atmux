# AutoTmux: Config, XDG Paths & Crash Recovery — Design

**Date:** 2026-06-05
**Status:** Approved (pending spec review)

## Goal

Make AutoTmux robust for real HPC cluster use by adding three capabilities:

1. **Persistent configuration** — restore the tunability lost in the v0.4.0 daemon
   split, via a TOML config file covering daemon timings.
2. **XDG-aware runtime paths** — prefer `$XDG_RUNTIME_DIR` over `/tmp`, removing the
   path duplication between `daemon.py` and `cli.py` along the way.
3. **Daemon crash recovery** — the frontend auto-restarts a dead daemon (with a loop
   guard) and surfaces a banner, instead of silently degrading to a static viewer.

## Motivation

- All runtime state lives in `/tmp/autotmux_*`, hardcoded **independently** in
  `daemon.py:37-41` and `cli.py:40-43`. Any path change must touch both in lockstep —
  a latent bug source.
- Every daemon tunable (`SQUEUE_INTERVAL`, timeouts, backoff) is hardcoded in
  `daemon.py:43-60`. The v0.3.x settings page is gone; users cannot tune for large
  clusters or slow login nodes.
- When the daemon dies while `atmux` is open, the frontend keeps rendering stale state
  with only a subtitle hint. `_launch_daemon()` runs once at startup and never again.

## Architecture: two new focused modules (Approach A)

Chosen over a single combined module (B) and minimal inline changes (C) because it
removes the existing path duplication and keeps each unit single-purpose and
independently testable, matching the codebase's existing daemon/frontend split.

### `src/autotmux/paths.py` — single source of truth for runtime file locations

```python
import os

_UID = os.getuid()

def _pick_base() -> str:
    """Choose a writable, node-local runtime base dir.

    Preference: $XDG_RUNTIME_DIR (/run/user/<uid>, tmpfs, node-local, short path),
    falling back to /tmp/autotmux_<uid> (current behavior). ~/.cache is deliberately
    NOT used: it is NFS on HPC clusters, which breaks SSH ControlMaster sockets — both
    because Unix-domain sockets misbehave on NFS and because of the ~104-char sun_path
    length limit.
    """
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK):
        base = os.path.join(xdg, 'autotmux')
    else:
        base = f'/tmp/autotmux_{_UID}'
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base

BASE          = _pick_base()
CTL_DIR       = os.path.join(BASE, 'ctl')
PID_FILE      = os.path.join(BASE, 'daemon.pid')
LOG_FILE      = os.path.join(BASE, 'daemon.log')
STATE_FILE    = os.path.join(BASE, 'daemon.json')
SNAPSHOT_FILE = os.path.join(BASE, 'snapshots.json')
```

`daemon.py` and `cli.py` drop their local path definitions and import from here.

**Design decision — fallback is `$XDG_RUNTIME_DIR` → `/tmp`, not `~/.cache`.** The
daemon requires its sockets on local, non-NFS fs with a short path. `$XDG_RUNTIME_DIR`
satisfies this strictly better than `/tmp`; `~/.cache` violates it. So XDG is the
preferred target and `/tmp` remains the safe existing fallback.

### `src/autotmux/config.py` — daemon tunables from TOML

```python
import os, logging

log = logging.getLogger('autotmux.config')

CONFIG_PATH = os.environ.get(
    'AUTOTMUX_CONFIG',
    os.path.expanduser('~/.config/autotmux/config.toml'),
)

# Defaults mirror the current daemon.py constants exactly.
DEFAULTS = {
    'squeue_interval': 30,
    'health_interval': 30,
    'session_interval': 15,
    'snapshot_interval': 120,
    'connect_timeout': 8,
    'deep_probe_timeout': 8,
    'shallow_check_timeout': 8,
    'squeue_timeout': 15,
    'ctl_persist': 3600,
    'server_alive_int': 30,
    'server_alive_max': 3,
    'gone_node_threshold': 2,
    'backoff_base': 30,
    'backoff_cap': 600,
}

def load() -> dict:
    """Return DEFAULTS merged with overrides from CONFIG_PATH. Never raises."""
    cfg = dict(DEFAULTS)
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            if os.path.exists(CONFIG_PATH):
                log.warning('config present but tomllib/tomli unavailable '
                            '(Python <3.11); using defaults')
            return cfg
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, 'rb') as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning(f'failed to parse {CONFIG_PATH}: {e}; using defaults')
        return cfg
    section = data.get('daemon', data)   # accept [daemon] table or flat top-level
    for k, v in section.items():
        if k in cfg and isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[k] = v
        else:
            log.warning(f'ignoring unknown/invalid config key: {k!r}')
    return cfg
```

Behavior:
- `tomllib` (3.11+) with a `tomli` fallback; on 3.10 with neither → warn + defaults,
  never crash.
- Unknown/wrong-type keys → warned and ignored.
- Malformed file → warned, defaults used.
- `AUTOTMUX_CONFIG` env override keeps tests hermetic.

Example `~/.config/autotmux/config.toml`:

```toml
[daemon]
squeue_interval = 60      # poll squeue less often on a large allocation
snapshot_interval = 300
connect_timeout = 15      # slow login node
```

## Daemon integration (`daemon.py`)

At module load, replace hardcoded constants with config- and paths-driven values:

```python
from autotmux import config, paths

_cfg = config.load()
SQUEUE_INTERVAL       = _cfg['squeue_interval']
HEALTH_INTERVAL       = _cfg['health_interval']
SESSION_INTERVAL      = _cfg['session_interval']
SNAPSHOT_INTERVAL     = _cfg['snapshot_interval']
CONNECT_TIMEOUT       = _cfg['connect_timeout']
DEEP_PROBE_TIMEOUT    = _cfg['deep_probe_timeout']
SHALLOW_CHECK_TIMEOUT = _cfg['shallow_check_timeout']
SQUEUE_TIMEOUT        = _cfg['squeue_timeout']
CTL_PERSIST           = _cfg['ctl_persist']
SERVER_ALIVE_INT      = _cfg['server_alive_int']
SERVER_ALIVE_MAX      = _cfg['server_alive_max']
GONE_NODE_THRESHOLD   = _cfg['gone_node_threshold']
BACKOFF_BASE          = _cfg['backoff_base']    # daemon.py:487
BACKOFF_CAP           = _cfg['backoff_cap']     # daemon.py:488

CTL_DIR, PID_FILE, LOG_FILE = paths.CTL_DIR, paths.PID_FILE, paths.LOG_FILE
STAT_FILE, SNAPSHOT_FILE    = paths.STATE_FILE, paths.SNAPSHOT_FILE
```

These constants are consumed only inside `daemon.py`, so the change is localized — the
rest of the daemon keeps reading the same names. Config is read once at startup
(also runs for `atd status`; cost is negligible).

## Crash recovery (`cli.py`)

Two distinct failure signals, handled differently:

| Signal           | Detection                              | Action                                  |
|------------------|----------------------------------------|-----------------------------------------|
| **Daemon dead**  | `_daemon_running()` → False (PID gone) | Auto-restart via `_launch_daemon()` + toast |
| **Daemon hung**  | PID alive but state age > 90s          | Banner only — never auto-kill           |

Auto-restart fires **only** when the PID is genuinely gone. Restarting a hung-but-alive
daemon risks killing one that is merely slow on a busy login node. The 90s threshold
gives margin over the 30s squeue interval.

Loop guard, extracted as a pure, testable function:

```python
def _should_restart(attempts: list, now: float, window: float = 60.0,
                    limit: int = 3) -> bool:
    recent = [t for t in attempts if now - t < window]
    return len(recent) < limit
```

≥3 restarts within 60s → stop auto-restarting and switch the subtitle to a persistent
`⚠ daemon crash-looping — run 'atd status'` banner.

Implementation notes:
- Wired into the existing 5s `_refresh_table` tick — no new timer.
- Recovery runs in a **Textual worker thread** because `_launch_daemon()` does a
  blocking `subprocess.run`; the UI never freezes.
- UI surfacing reuses existing mechanisms: a transient `self.notify("restarting
  daemon…")` toast per auto-restart, and the existing `self.sub_title` for persistent
  down / crash-loop state. No new widget or CSS.
- Restart-attempt timestamps stored on the app instance; `time.monotonic()` for `now`.

## Migration / back-compat

Changing the runtime dir orphans any daemon already running under the old
`/tmp/autotmux_*` paths: the new frontend would see no daemon at the new location and
launch a second one (duplicate SSH masters).

Mitigation, kept minimal:
- On `atd start`, before claiming the new PID file, check the legacy
  `/tmp/autotmux_daemon_<uid>.pid`. If a process is running there, SIGTERM it and log
  `migrated: stopped legacy daemon (pid=N)`. Upgrade becomes a clean `atd restart`.
- Legacy `/tmp/autotmux_ctl_<uid>/` sockets are left untouched (harmless; ControlPersist
  reaps them).
- README gets a one-line note: "After upgrading, run `atd restart`."

## Testing

New `unittest` modules following the existing fake-binary style:

- **`tests/test_paths.py`** — `XDG_RUNTIME_DIR` set & writable → base under it; unset →
  `/tmp/autotmux_<uid>`; XDG set but non-writable → falls back to `/tmp`. Monkeypatched
  env + temp dirs.
- **`tests/test_config.py`** — no file → exact `DEFAULTS`; valid TOML → overrides
  applied; unknown key → ignored + warned; malformed TOML → defaults + warned;
  `[daemon]` table and flat layout both work; bool rejected as non-numeric. Uses
  `AUTOTMUX_CONFIG` env override.
- **`tests/test_recovery.py`** — pure unit tests of `_should_restart()` (under limit →
  True; at/over limit in window → False; attempts outside window don't count). Plus a
  Pilot-style test mocking `_daemon_running`/`_launch_daemon` to assert: PID-dead
  triggers a restart attempt, hung-but-alive does not, crash-loop flips to the
  persistent banner.

Existing tests should be unaffected since paths/config resolve to the same effective
defaults. Any test hardcoding a `/tmp/autotmux_*` literal will be pointed at `paths.*`.

## Packaging / docs

- Add `tomli; python_version < "3.11"` to `pyproject.toml` dependencies.
- README: document the config file (location, keys, example) and the XDG path behavior;
  add the "run `atd restart` after upgrading" note.

## Out of scope

- Path overrides via config (config covers daemon timings only).
- Frontend preferences (refresh rates, panel sizes, keybindings).
- Re-porting other v0.3.x features (watch mode, Slack, notes, search).
- A standalone daemon supervisor independent of the frontend.

## Deliverables summary

1. `src/autotmux/paths.py` — XDG-aware runtime dir, removes path duplication.
2. `src/autotmux/config.py` — TOML daemon tunables with safe fallbacks.
3. `daemon.py` wired to both modules.
4. Frontend auto-restart (PID-dead only) + loop guard + banner.
5. Legacy-daemon migration on `atd start`.
6. `tests/test_paths.py`, `tests/test_config.py`, `tests/test_recovery.py`.
7. `pyproject.toml` + README updates.
