# Config, XDG Paths & Crash Recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a TOML config file for daemon tunables, move runtime files to an XDG-aware directory, and make the frontend auto-restart a dead daemon with a loop guard and banner.

**Architecture:** Two new focused modules — `paths.py` (single source of truth for runtime file locations, XDG_RUNTIME_DIR → /tmp) and `config.py` (TOML daemon tunables with safe fallbacks). `daemon.py` and `cli.py` import paths from `paths.py`; `daemon.py` reads tunables from `config.py`. The frontend's existing 5s refresh tick gains a recovery step that restarts a PID-dead daemon (capped at 3 restarts/60s) and surfaces a crash-loop banner.

**Tech Stack:** Python 3.10–3.12, stdlib `tomllib` (3.11+) with `tomli` fallback (3.10), Textual, `unittest`.

**Reference spec:** `docs/superpowers/specs/2026-06-05-config-crash-recovery-design.md`

---

## File Structure

- **Create** `src/autotmux/paths.py` — resolves runtime base dir + all file paths. Imported by `daemon.py` and `cli.py`.
- **Create** `src/autotmux/config.py` — loads daemon tunables from TOML. Imported by `daemon.py`.
- **Modify** `src/autotmux/daemon.py` — import paths + config; add legacy-daemon migration.
- **Modify** `src/autotmux/cli.py` — import paths; add crash-recovery logic.
- **Create** `tests/test_paths.py`, `tests/test_config.py`, `tests/test_recovery.py`.
- **Modify** `pyproject.toml` — add `tomli` dependency for 3.10.
- **Modify** `README.md` — document config file, XDG paths, upgrade note.

---

## Task 1: `paths.py` module

**Files:**
- Create: `src/autotmux/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_paths.py`:

```python
"""Tests for autotmux.paths — XDG-aware runtime dir resolution."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import paths


class PickBaseTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get('XDG_RUNTIME_DIR')

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('XDG_RUNTIME_DIR', None)
        else:
            os.environ['XDG_RUNTIME_DIR'] = self._saved

    def test_uses_xdg_when_set_and_writable(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ['XDG_RUNTIME_DIR'] = td
            base = paths._pick_base()
            self.assertEqual(base, os.path.join(td, 'autotmux'))
            self.assertTrue(os.path.isdir(base))

    def test_falls_back_to_tmp_when_xdg_unset(self):
        os.environ.pop('XDG_RUNTIME_DIR', None)
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_falls_back_to_tmp_when_xdg_not_writable(self):
        os.environ['XDG_RUNTIME_DIR'] = '/proc/nonexistent-not-writable'
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_module_constants_live_under_base(self):
        self.assertTrue(paths.PID_FILE.startswith(paths.BASE))
        self.assertEqual(os.path.basename(paths.PID_FILE), 'daemon.pid')
        self.assertEqual(os.path.basename(paths.STATE_FILE), 'daemon.json')
        self.assertTrue(paths.CTL_DIR.startswith(paths.BASE))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_paths -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotmux.paths'`

- [ ] **Step 3: Write minimal implementation**

Create `src/autotmux/paths.py`:

```python
"""Single source of truth for autotmux runtime file locations.

Prefers $XDG_RUNTIME_DIR (/run/user/<uid>, tmpfs, node-local, short path),
falling back to /tmp/autotmux_<uid> (the pre-XDG behavior). ~/.cache is
deliberately NOT used: it is NFS on HPC clusters, which breaks SSH
ControlMaster sockets — both because Unix-domain sockets misbehave on NFS
and because of the ~104-char sun_path length limit.
"""
import os

_UID = os.getuid()


def _pick_base() -> str:
    """Choose a writable, node-local runtime base dir and create it."""
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

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_paths -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autotmux/paths.py tests/test_paths.py
git commit -m "feat: add XDG-aware paths module"
```

---

## Task 2: Wire `cli.py` to use `paths`

**Files:**
- Modify: `src/autotmux/cli.py:39-43`

- [ ] **Step 1: Replace the hardcoded path block**

In `src/autotmux/cli.py`, replace lines 39-43:

```python
_UID = os.getuid()
STATE_FILE = f'/tmp/autotmux_daemon_{_UID}.json'
CTL_DIR = f'/tmp/autotmux_ctl_{_UID}'
PID_FILE = f'/tmp/autotmux_daemon_{_UID}.pid'
SNAPSHOT_FILE = f'/tmp/autotmux_snapshots_{_UID}.json'
```

with:

```python
from autotmux import paths

STATE_FILE = paths.STATE_FILE
CTL_DIR = paths.CTL_DIR
PID_FILE = paths.PID_FILE
SNAPSHOT_FILE = paths.SNAPSHOT_FILE
```

(Keep the existing `from autotmux import __version__` line above it untouched. `_UID` was only used to build these paths; if a later reference to `_UID` exists, grep confirms — see Step 2.)

- [ ] **Step 2: Verify `_UID` is not referenced elsewhere in cli.py**

Run: `grep -n "_UID" src/autotmux/cli.py`
Expected: no matches (the only uses were the path lines just removed). If matches remain, add `_UID = os.getuid()` back above the `from autotmux import paths` line.

- [ ] **Step 3: Run the full test suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS — existing tests that set `autotmux.STATE_FILE` still work (they override the module attribute, which is unaffected by where it initially points).

- [ ] **Step 4: Smoke-test the import**

Run: `python -c "from autotmux import cli; print(cli.STATE_FILE)"`
Expected: prints a path ending in `daemon.json` (under XDG or /tmp).

- [ ] **Step 5: Commit**

```bash
git add src/autotmux/cli.py
git commit -m "refactor: cli reads runtime paths from paths module"
```

---

## Task 3: `config.py` module

**Files:**
- Create: `src/autotmux/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
"""Tests for autotmux.config — TOML daemon tunables with safe fallbacks."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import config


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved_path = config.CONFIG_PATH

    def tearDown(self):
        config.CONFIG_PATH = self._saved_path

    def _write(self, td, text):
        p = os.path.join(td, 'config.toml')
        with open(p, 'w') as f:
            f.write(text)
        config.CONFIG_PATH = p
        return p

    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            config.CONFIG_PATH = os.path.join(td, 'nope.toml')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_daemon_table_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = 60\n')
            cfg = config.load()
            self.assertEqual(cfg['squeue_interval'], 60)
            # untouched keys keep defaults
            self.assertEqual(cfg['health_interval'],
                             config.DEFAULTS['health_interval'])

    def test_flat_layout_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'connect_timeout = 15\n')
            self.assertEqual(config.load()['connect_timeout'], 15)

    def test_unknown_key_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nbogus = 5\n')
            cfg = config.load()
            self.assertNotIn('bogus', cfg)
            self.assertEqual(cfg, config.DEFAULTS)

    def test_bool_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = true\n')
            # bool must not override a numeric tunable
            self.assertEqual(config.load()['squeue_interval'],
                             config.DEFAULTS['squeue_interval'])

    def test_malformed_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'this is = not valid toml [[[')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_load_returns_a_copy(self):
        cfg = config.load()
        cfg['squeue_interval'] = 999
        self.assertNotEqual(config.DEFAULTS['squeue_interval'], 999)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_config -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotmux.config'`

- [ ] **Step 3: Write minimal implementation**

Create `src/autotmux/config.py`:

```python
"""Load daemon tunables from ~/.config/autotmux/config.toml.

Returns DEFAULTS merged with file overrides. Never raises: a missing,
malformed, or unparseable file falls back to defaults (with a logged
warning). The AUTOTMUX_CONFIG env var overrides the path (used by tests).
"""
import os
import logging

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
    section = data.get('daemon', data)  # accept [daemon] table or flat
    for k, v in section.items():
        if k in cfg and isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[k] = v
        else:
            log.warning(f'ignoring unknown/invalid config key: {k!r}')
    return cfg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_config -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autotmux/config.py tests/test_config.py
git commit -m "feat: add TOML config loader for daemon tunables"
```

---

## Task 4: Wire `daemon.py` to `paths` + `config`

**Files:**
- Modify: `src/autotmux/daemon.py:31-60` (paths + tunables block)
- Modify: `src/autotmux/daemon.py:487-488` (backoff constants)

- [ ] **Step 1: Replace the paths block**

In `src/autotmux/daemon.py`, replace lines 31-41:

```python
# ── paths (all in /tmp so they survive NFS issues) ──────────────────────────
_UID = os.getuid()
_USER = os.environ.get('USER', str(_UID))

from autotmux import __version__

CTL_DIR   = f'/tmp/autotmux_ctl_{_UID}'
PID_FILE  = f'/tmp/autotmux_daemon_{_UID}.pid'
LOG_FILE  = f'/tmp/autotmux_daemon_{_UID}.log'
STAT_FILE = f'/tmp/autotmux_daemon_{_UID}.json'  # status snapshot for `status` cmd
SNAPSHOT_FILE = f'/tmp/autotmux_snapshots_{_UID}.json'
```

with:

```python
# ── paths (XDG-aware, see autotmux.paths) ───────────────────────────────────
_UID = os.getuid()
_USER = os.environ.get('USER', str(_UID))

from autotmux import __version__, config, paths

CTL_DIR   = paths.CTL_DIR
PID_FILE  = paths.PID_FILE
LOG_FILE  = paths.LOG_FILE
STAT_FILE = paths.STATE_FILE  # status snapshot for `status` cmd
SNAPSHOT_FILE = paths.SNAPSHOT_FILE

# Legacy pre-XDG pid file — used by migration to stop an old daemon (Task 5).
LEGACY_PID_FILE = f'/tmp/autotmux_daemon_{_UID}.pid'

_cfg = config.load()
```

- [ ] **Step 2: Replace the tunables block with config-driven values**

In `src/autotmux/daemon.py`, replace the tunables block (lines 43-58, from `SQUEUE_INTERVAL = 30` through `GONE_NODE_THRESHOLD = 2`, keeping the `HOST_EXPR_RE` and `os.makedirs(CTL_DIR, ...)` lines below it) with:

```python
# ── tunables (overridable via ~/.config/autotmux/config.toml) ───────────────
SQUEUE_INTERVAL       = _cfg['squeue_interval']
HEALTH_INTERVAL       = _cfg['health_interval']
DEEP_PROBE_TIMEOUT    = _cfg['deep_probe_timeout']
CONNECT_TIMEOUT       = _cfg['connect_timeout']
CTL_PERSIST           = _cfg['ctl_persist']
SQUEUE_TIMEOUT        = _cfg['squeue_timeout']
SNAPSHOT_INTERVAL     = _cfg['snapshot_interval']
SESSION_INTERVAL      = _cfg['session_interval']
SERVER_ALIVE_INT      = _cfg['server_alive_int']
SERVER_ALIVE_MAX      = _cfg['server_alive_max']
GONE_NODE_THRESHOLD   = _cfg['gone_node_threshold']
SHALLOW_CHECK_TIMEOUT = _cfg['shallow_check_timeout']
```

Leave the `HOST_EXPR_RE = re.compile(...)` line and the `os.makedirs(CTL_DIR, mode=0o700, exist_ok=True)` line exactly as they are (they follow the tunables block).

- [ ] **Step 3: Replace the backoff constants**

In `src/autotmux/daemon.py`, replace lines 487-488:

```python
BACKOFF_BASE  = 30   # seconds — initial retry delay after a master start fails
BACKOFF_CAP   = 600  # seconds — max retry delay (10 min)
```

with:

```python
BACKOFF_BASE  = _cfg['backoff_base']  # initial retry delay after a master start fails
BACKOFF_CAP   = _cfg['backoff_cap']   # max retry delay
```

- [ ] **Step 4: Verify the daemon imports and reports config-driven values**

Run: `python -c "from autotmux import daemon as d; print(d.SQUEUE_INTERVAL, d.BACKOFF_CAP, d.STAT_FILE)"`
Expected: prints `30 600 <path ending in daemon.json>` (defaults, no config file present).

Run with an override:
```bash
mkdir -p /tmp/cfgtest && printf '[daemon]\nsqueue_interval = 77\n' > /tmp/cfgtest/c.toml
AUTOTMUX_CONFIG=/tmp/cfgtest/c.toml python -c "from autotmux import daemon as d; print(d.SQUEUE_INTERVAL)"
```
Expected: prints `77`.

- [ ] **Step 5: Run the full test suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS (existing tests unaffected; they override module attrs directly).

- [ ] **Step 6: Commit**

```bash
git add src/autotmux/daemon.py
git commit -m "feat: daemon reads paths and tunables from paths/config modules"
```

---

## Task 5: Legacy-daemon migration on `atd start`

**Files:**
- Modify: `src/autotmux/daemon.py` (add `_stop_legacy_daemon`, call it in `cmd_start`)
- Test: append to `tests/test_recovery.py` (created in Task 6) — **OR** create `tests/test_migration.py` now

> Note: this task creates `tests/test_migration.py` independently so task order is flexible.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration.py`:

```python
"""Tests for legacy-daemon migration on atd start."""
import os
import sys
import time
import subprocess
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import daemon as d


class StopLegacyDaemonTests(unittest.TestCase):
    def setUp(self):
        self._saved_legacy = d.LEGACY_PID_FILE
        self._saved_pid = d.PID_FILE

    def tearDown(self):
        d.LEGACY_PID_FILE = self._saved_legacy
        d.PID_FILE = self._saved_pid

    def test_noop_when_legacy_equals_current(self):
        # When paths resolved to /tmp, legacy == current — must not self-kill.
        d.LEGACY_PID_FILE = '/tmp/same.pid'
        d.PID_FILE = '/tmp/same.pid'
        d._stop_legacy_daemon()  # should simply return, no exception

    def test_stops_running_legacy_process(self):
        proc = subprocess.Popen(['sleep', '30'])
        try:
            with tempfile.TemporaryDirectory() as td:
                legacy = os.path.join(td, 'legacy.pid')
                with open(legacy, 'w') as f:
                    f.write(str(proc.pid))
                d.LEGACY_PID_FILE = legacy
                d.PID_FILE = os.path.join(td, 'new.pid')
                d._stop_legacy_daemon()
                # poll up to 5s for SIGTERM to take effect
                for _ in range(50):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(proc.poll(),
                                     'legacy process was not stopped')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_missing_legacy_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            d.LEGACY_PID_FILE = os.path.join(td, 'absent.pid')
            d.PID_FILE = os.path.join(td, 'new.pid')
            d._stop_legacy_daemon()  # no file → return cleanly


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_migration -v`
Expected: FAIL — `AttributeError: module 'autotmux.daemon' has no attribute '_stop_legacy_daemon'`

- [ ] **Step 3: Write minimal implementation**

In `src/autotmux/daemon.py`, add this function just above `def cmd_start():`:

```python
def _stop_legacy_daemon() -> None:
    """Stop a pre-XDG daemon still running under the old /tmp pid file.

    Without this, after upgrading to XDG paths the new frontend wouldn't see
    the old daemon (different pid-file location) and would start a second one,
    leaving two daemons fighting over the same ControlMaster sockets.
    """
    if LEGACY_PID_FILE == PID_FILE:
        return  # paths resolved to /tmp anyway; nothing to migrate
    try:
        with open(LEGACY_PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    if pid == os.getpid() or not _pid_running(pid):
        return
    log.info(f'migrated: stopping legacy daemon (pid={pid})')
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
```

- [ ] **Step 4: Call it from `cmd_start`**

In `src/autotmux/daemon.py`, inside `cmd_start()`, add the migration call immediately after `_install_signal_handlers()` and before `_write_pid()`:

```python
    _install_signal_handlers()
    _stop_legacy_daemon()
    _write_pid()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_migration -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/autotmux/daemon.py tests/test_migration.py
git commit -m "feat: stop legacy /tmp daemon on atd start"
```

---

## Task 6: Crash-recovery loop-guard (pure function)

**Files:**
- Modify: `src/autotmux/cli.py` (add module-level `_should_restart`)
- Test: `tests/test_recovery.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_recovery.py`:

```python
"""Tests for crash-recovery logic in autotmux.cli."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli


class ShouldRestartTests(unittest.TestCase):
    def test_allows_when_no_prior_attempts(self):
        self.assertTrue(cli._should_restart([], now=100.0))

    def test_allows_under_limit(self):
        self.assertTrue(cli._should_restart([99.0, 98.0], now=100.0))

    def test_blocks_at_limit_within_window(self):
        self.assertFalse(cli._should_restart([99.0, 98.0, 97.0], now=100.0))

    def test_old_attempts_outside_window_do_not_count(self):
        # three attempts but all older than the 60s window
        self.assertTrue(cli._should_restart([10.0, 20.0, 30.0], now=200.0))

    def test_mixed_window_counts_only_recent(self):
        # two recent (within 60s of now=100), one old → under limit of 3
        self.assertTrue(cli._should_restart([10.0, 70.0, 80.0], now=100.0))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_recovery -v`
Expected: FAIL — `AttributeError: module 'autotmux.cli' has no attribute '_should_restart'`

- [ ] **Step 3: Write minimal implementation**

In `src/autotmux/cli.py`, add this module-level function next to `_daemon_running` (near line 810):

```python
def _should_restart(attempts, now: float, window: float = 60.0,
                    limit: int = 3) -> bool:
    """Loop guard: allow a daemon restart only if fewer than `limit`
    restarts happened in the last `window` seconds. `attempts` is a list of
    time.monotonic() timestamps of prior restarts."""
    recent = [t for t in attempts if now - t < window]
    return len(recent) < limit
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_recovery -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autotmux/cli.py tests/test_recovery.py
git commit -m "feat: add restart loop-guard for crash recovery"
```

---

## Task 7: Wire crash recovery into the frontend

**Files:**
- Modify: `src/autotmux/cli.py` — `AutotmuxApp.__init__`, `_refresh_table`, add recovery methods
- Test: append to `tests/test_recovery.py`

> The app's `__init__` sets up instance state. Find it in `AutotmuxApp` (the
> class with the `BINDINGS`/`on_mount` near line 425-475). If there is no
> explicit `__init__`, add one that calls `super().__init__()` first.

- [ ] **Step 1: Write the failing test (append to `tests/test_recovery.py`)**

Add to `tests/test_recovery.py` (above the `if __name__` block):

```python
import asyncio
from autotmux.cli import AutotmuxApp


class MaybeRecoverTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_no_recovery_when_daemon_alive(self):
        async def go():
            app = AutotmuxApp()
            async with app.run_test():
                calls = []
                app._dispatch_restart = lambda: calls.append(1)
                cli._daemon_running = lambda: True
                app._crash_looping = True  # should be cleared
                app._maybe_recover_daemon()
                self.assertEqual(calls, [])
                self.assertFalse(app._crash_looping)
        self._run(go())

    def test_restart_dispatched_when_dead(self):
        async def go():
            app = AutotmuxApp()
            async with app.run_test():
                calls = []
                app._dispatch_restart = lambda: calls.append(1)
                cli._daemon_running = lambda: False
                app._maybe_recover_daemon()
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(app._restart_attempts), 1)
        self._run(go())

    def test_stops_after_loop_guard_and_sets_banner(self):
        async def go():
            app = AutotmuxApp()
            async with app.run_test():
                calls = []
                app._dispatch_restart = lambda: calls.append(1)
                cli._daemon_running = lambda: False
                for _ in range(5):
                    app._maybe_recover_daemon()
                self.assertEqual(len(calls), 3)       # capped at limit
                self.assertTrue(app._crash_looping)
        self._run(go())
```

> The test reassigns `cli._daemon_running` directly; restore is not strictly
> needed since each test sets it, but if running in a shared process add a
> `tearDown` that restores it. (Keeping minimal here.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_recovery -v`
Expected: FAIL — `AttributeError: 'AutotmuxApp' object has no attribute '_maybe_recover_daemon'` (and `_restart_attempts`)

- [ ] **Step 3: Add instance state in `AutotmuxApp.__init__`**

In `src/autotmux/cli.py`, in `AutotmuxApp.__init__` (add one if absent), after `super().__init__(...)`:

```python
        self._restart_attempts = []   # time.monotonic() of recent restarts
        self._crash_looping = False
```

- [ ] **Step 4: Add the recovery methods to `AutotmuxApp`**

In `src/autotmux/cli.py`, add these methods to `AutotmuxApp` (place near `_daemon_age_seconds`):

```python
    def _maybe_recover_daemon(self) -> None:
        """Auto-restart a PID-dead daemon (with loop guard); banner-only for a
        hung-but-alive one (the existing stale subtitle covers that, and we
        never auto-kill a daemon that's merely slow)."""
        if _daemon_running():
            self._crash_looping = False
            return
        now = time.monotonic()
        if not _should_restart(self._restart_attempts, now):
            self._crash_looping = True
            return
        self._restart_attempts.append(now)
        self.notify('daemon down — restarting…', severity='warning', timeout=4)
        self._dispatch_restart()

    def _dispatch_restart(self) -> None:
        self.run_worker(self._restart_daemon_async(),
                        exclusive=True, group='recovery')

    async def _restart_daemon_async(self) -> None:
        await asyncio.to_thread(_launch_daemon)
```

- [ ] **Step 5: Consolidate the subtitle logic and call recovery from `_refresh_table`**

In `src/autotmux/cli.py`, add this helper method to `AutotmuxApp` (near `_refresh_table`):

```python
    def _status_subtitle(self, state, rows, updated) -> str:
        if self._crash_looping:
            return "⚠ daemon crash-looping — run `atd status`"
        if not state.get('nodes'):
            return "waiting for daemon… (run `atd status` to inspect)"
        stale = self._daemon_age_seconds(updated)
        if stale is not None and stale > 30:
            return f"⚠ daemon stale ({stale:.0f}s old) · run `atd status`"
        return f"{len(rows)} sessions · updated {updated}"
```

Then, at the very top of `_refresh_table` (before `state = read_state()`), add:

```python
        self._maybe_recover_daemon()
```

Finally, replace BOTH subtitle-setting branches in `_refresh_table` with the helper. In the hot path (the `if sig == self._last_rows_sig:` block), replace:

```python
            if not state.get('nodes'):
                self.sub_title = "waiting for daemon… (run `atd status` to inspect)"
            else:
                self.sub_title = f"{len(rows)} sessions · updated {updated}"
            return
```

with:

```python
            self.sub_title = self._status_subtitle(state, rows, updated)
            return
```

And in the cold path at the end of `_refresh_table`, replace:

```python
        if not state.get('nodes'):
            self.sub_title = "waiting for daemon… (run `atd status` to inspect)"
        else:
            stale = self._daemon_age_seconds(updated)
            if stale is not None and stale > 30:
                self.sub_title = f"⚠ daemon stale ({stale:.0f}s old) · run `atd status`"
            else:
                self.sub_title = f"{len(rows)} sessions · updated {updated}"
```

with:

```python
        self.sub_title = self._status_subtitle(state, rows, updated)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m unittest tests.test_recovery -v`
Expected: PASS (8 tests total in the file)

- [ ] **Step 7: Run the full suite + frontend pilot tests**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS — including the existing `test_frontend_pilot.py` (subtitle text for the waiting/stale cases is unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/autotmux/cli.py tests/test_recovery.py
git commit -m "feat: frontend auto-restarts dead daemon with loop guard and banner"
```

---

## Task 8: Packaging + docs

**Files:**
- Modify: `pyproject.toml:32-35` (dependencies)
- Modify: `README.md`

- [ ] **Step 1: Add the `tomli` dependency for Python 3.10**

In `pyproject.toml`, replace:

```toml
dependencies = [
    "textual>=0.40",
    "rich>=13.0",
]
```

with:

```toml
dependencies = [
    "textual>=0.40",
    "rich>=13.0",
    "tomli>=2.0; python_version < '3.11'",
]
```

- [ ] **Step 2: Verify the package still builds metadata**

Run: `python -c "import tomllib" 2>/dev/null && echo "3.11+ ok (tomllib stdlib)" || echo "3.10 — tomli needed"`
Expected: on this machine (3.12) prints `3.11+ ok`.

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS (full suite).

- [ ] **Step 3: Document config + XDG paths in README**

In `README.md`, add a `## Configuration` section (place it after the daemon-control section). Use this content:

````markdown
## Configuration

Daemon timings can be tuned via `~/.config/autotmux/config.toml` (optional —
sane defaults apply if absent). Either a `[daemon]` table or flat keys work:

```toml
[daemon]
squeue_interval   = 60     # seconds between squeue polls (default 30)
session_interval  = 15     # seconds between tmux list-sessions polls
snapshot_interval = 120    # seconds between pane-capture snapshots
health_interval   = 30     # seconds between ControlMaster health checks
connect_timeout   = 8      # ssh ConnectTimeout
backoff_base      = 30     # initial retry delay after a failed master start
backoff_cap       = 600    # max retry delay
```

Unknown or non-numeric keys are ignored with a warning in the daemon log.
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
````

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml README.md
git commit -m "docs: document config file and XDG paths; add tomli dep for 3.10"
```

---

## Final verification

- [ ] **Run the complete test suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS — all existing tests plus `test_paths`, `test_config`, `test_migration`, `test_recovery`.

- [ ] **Smoke-test the daemon lifecycle**

Run:
```bash
python -m autotmux.daemon start && sleep 2 && python -m autotmux.daemon status && python -m autotmux.daemon stop
```
Expected: start prints the log path, status shows `✓ Running` with a path under XDG/tmp, stop reports it stopped.

- [ ] **Confirm runtime files landed in the XDG dir (if set)**

Run: `ls -la "${XDG_RUNTIME_DIR:-/tmp/autotmux_$(id -u)}"/autotmux 2>/dev/null || ls -la /tmp/autotmux_$(id -u)`
Expected: `daemon.pid`, `daemon.log`, `daemon.json`, and a `ctl/` dir.

## Notes for the implementer

- **TDD discipline:** every task writes the test first, watches it fail, then implements. Do not skip the "verify it fails" step — it proves the test exercises the new code.
- **The existing tests override module attributes** (`autotmux.STATE_FILE = ...`). This pattern still works after the refactor because we assign module-level names from `paths.*` at import; tests reassign those names afterward.
- **Why `time.monotonic()` for the loop guard** but `datetime` for staleness: staleness compares against the daemon's wall-clock `updated` string; the loop guard only measures elapsed time between restarts, where monotonic is correct (immune to clock jumps).
- **Hung-but-alive daemon:** intentionally gets banner-only treatment via the existing stale subtitle. Recovery gates on `_daemon_running()` (PID liveness), so a slow daemon is never auto-killed.
