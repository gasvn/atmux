#!/usr/bin/env python3
"""
autotmux_daemon.py - SSH ControlMaster keep-alive daemon
=========================================================
Runs in the background and maintains persistent SSH ControlMaster connections
for all nodes returned by `squeue`. This allows autotmux (and plain ssh) to
attach to remote tmux sessions instantly, without re-authenticating.

Sockets and state use the XDG-aware node-local runtime directory described in
``autotmux.paths`` (never NFS).

Usage:
    python3 autotmux_daemon.py start    # start daemon in background
    python3 autotmux_daemon.py stop     # stop daemon
    python3 autotmux_daemon.py restart  # restart daemon
    python3 autotmux_daemon.py status   # show running nodes & masters
    python3 autotmux_daemon.py run      # run in foreground (for debugging)
"""

import os
import sys
import time
import signal
import shlex
import subprocess
import threading
import json
import re
import glob
import fcntl
import uuid
import logging
import math
import pwd
import select
import socket
import stat
import struct
import weakref
from logging.handlers import RotatingFileHandler

from autotmux import (
    __version__, config, ipc, keepalive, lifecycle, network, notify, paths,
    warm_registry,
)

# ── paths (XDG-aware, see autotmux.paths) ───────────────────────────────────
_UID = os.getuid()
# Never consult NSS during module import: LDAP/NSS can block indefinitely, and
# control commands such as `atd stop` do not need a username at all.  The daemon
# child resolves it through the bounded runtime loader below; numeric UID is the
# safe fallback accepted by Slurm when directory service is unavailable.
_USER = str(_UID)

CTL_DIR   = paths.CTL_DIR
PID_FILE  = paths.PID_FILE
LOG_FILE  = paths.LOG_FILE
STAT_FILE = paths.STATE_FILE  # status snapshot for `status` cmd
SNAPSHOT_FILE = paths.SNAPSHOT_FILE
PREVIEW_SOCKET = paths.PREVIEW_SOCKET
WARM_DIR = paths.WARM_DIR
# Exclusive singleton lock — held for the daemon's whole lifetime so two
# concurrent `atd start` invocations can't both spawn a daemon.
LOCK_FILE = PID_FILE + '.lock'
GUARD_FILE = paths.GUARD_FILE

# Legacy pre-XDG pid file — used by migration to stop an old daemon (Task 5).
LEGACY_PID_FILE = f'/tmp/autotmux_daemon_{_UID}.pid'

# ── logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger('autotmux_daemon')
log.setLevel(logging.INFO)
log.propagate = False


def _prepare_log_files(path: str, backup_count: int = 3) -> None:
    """Create/tighten owned regular log files without following symlinks.

    Older releases created the log before setting a private umask, leaving it
    mode 0644.  The runtime directory is itself 0700, but correcting every
    existing generation to 0600 preserves that boundary even if the directory
    is later copied or its permissions are changed.  ``O_NONBLOCK`` also makes
    an accidentally planted FIFO fail cleanly instead of hanging startup.
    """
    for index in range(backup_count + 1):
        candidate = path if index == 0 else f'{path}.{index}'
        flags = (os.O_WRONLY | os.O_APPEND
                 | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
        if index == 0:
            flags |= os.O_CREAT
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != _UID:
                raise OSError(f'unsafe daemon log file {candidate!r}')
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def _configure_logging() -> None:
    """Open the runtime log only in the actual daemon, never control clients."""
    if any(isinstance(handler, RotatingFileHandler) for handler in log.handlers):
        return
    try:
        _prepare_log_files(LOG_FILE)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    except Exception as error:
        # State publication is more important than logging. A full/read-only
        # runtime filesystem must not turn a recoverable observability failure
        # into a daemon crash loop.
        if not log.handlers:
            log.addHandler(logging.NullHandler())
        try:
            sys.stderr.write(f'autotmux: could not open daemon log: {error}\n')
        except Exception:
            pass
        return
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    log.addHandler(handler)

_cfg = dict(config.DEFAULTS)

# Keep-alive auto-renew: reads the TUI-written registry, resubmits `占坑`
# batch scripts before their allocation expires. Driven from _squeue_loop.
_keepalive_mgr = keepalive.KeepAliveManager(config.KEEPALIVE_PATH,
                                            dict(config.KEEPALIVE_DEFAULTS))

# Reminders for jobs nearing their time limit.  The daemon outlives the TUI, so
# this keeps working after the dashboard is closed -- which is when a warning
# is actually worth sending.
_notify_cfg = dict(config.NOTIFY_DEFAULTS)
_notified_jobs: set[str] = set()
# Jobs already seen running, so a start is announced once. Seeded on the first
# complete poll rather than compared, or a restart announces everything that
# happened to be running at the time.
_started_jobs: set[str] = set()
_started_seeded = False
# node -> sessions already announced as quiet, cleared when they move again.
_idle_announced: dict[str, set] = {}
_notify_lock = threading.Lock()

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
NETWORK_BACKOFF_BASE  = _cfg['network_backoff_base']
NETWORK_BACKOFF_CAP   = _cfg['network_backoff_cap']
WARM_ORPHAN_INTERVAL  = _cfg['warm_orphan_interval']
# Expanded hosts must be plain ssh destinations, never option-looking strings.
# Brackets/commas are accepted only by the nodelist-expansion branch below.
HOST_RE           = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
HOST_EXPR_RE      = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._\-\[\],]*$')
_SESSION_SECTION = '\x00AUTOTMUX_SESSIONS\x00'
_NODEINFO_SECTION = '\x00AUTOTMUX_NODEINFO\x00'
_TMUXINFO_SECTION = '\x00AUTOTMUX_TMUXINFO\x00'
_STATE_FILE_LIMIT = 8 * 1024 * 1024
_SNAPSHOT_FILE_LIMIT = 64 * 1024 * 1024
_LOG_TAIL_LIMIT = 1024 * 1024
_DAEMON_READY_TIMEOUT = 10.0
_CLOCK_ID = lifecycle.monotonic_clock_id()
_PREVIEW_CONTENT_LIMIT = 1024 * 1024


def _network_backoff_delays(base: float, cap: float) -> tuple[float, ...]:
    values = []
    value = max(0.1, float(base))
    cap = max(value, float(cap))
    while value < cap and len(values) < 31:
        values.append(value)
        value = min(cap, value * 2)
    values.append(cap)
    return tuple(values)


_network_coordinator = network.NodeNetworkCoordinator(
    _network_backoff_delays(NETWORK_BACKOFF_BASE, NETWORK_BACKOFF_CAP))


class _CommandCapacityExhausted(RuntimeError):
    """No subprocess helper slot was available, so no probe was attempted."""

paths.ensure_runtime_dirs()


# ── helpers ──────────────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data) -> None:
    """Write JSON atomically — write to a unique tmp file, fsync, then
    os.replace.

    Without this, a frontend polling the state file can read a half-written
    document and bail with a JSON error. The tmp name must be unique per
    call (not just per-pid): three daemon loop threads write the same state
    file, and a shared tmp path would let them truncate each other's
    in-flight write. The fsync makes the data durable before the rename so a
    crash can't leave a zero-length file behind the atomic swap.
    """
    tmp = f'{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}'
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json_dict(path: str, max_bytes: int) -> dict:
    """Read a small, owned regular JSON file or raise a safe read error."""
    raw = lifecycle.read_owned_regular_file(path, max_bytes)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f'expected a JSON object in {path!r}')
    return value


def _sweep_stale_tmp() -> None:
    """Remove leftover `*.tmp.*` files from a previously-crashed daemon so
    they don't accumulate in the runtime dir across restarts."""
    for base in (PID_FILE, STAT_FILE, SNAPSHOT_FILE):
        for stale in glob.glob(f'{base}.tmp.*'):
            try:
                os.unlink(stale)
            except OSError:
                pass


# ── ControlMaster helpers ────────────────────────────────────────────────────

def _ctl_path(node: str) -> str:
    return paths.control_path(node, CTL_DIR)


def _master_alive(node: str, deep: bool = False) -> bool:
    """Check whether the ControlMaster socket is usable.

    `ssh -O check` only verifies the local mux socket. With deep=True we also
    run a trivial command through the master to detect connections that look
    alive locally but hang on real I/O (network blip, remote sshd gone, etc.).
    Localhost has no SSH master and is always considered alive.
    """
    if node == 'localhost':
        return True
    ctl = _ctl_path(node)
    if not os.path.exists(ctl):
        return False
    try:
        r = _hard_run(
            ['ssh', '-o', f'ControlPath={ctl}', '-O', 'check', node],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=SHALLOW_CHECK_TIMEOUT,
        )
        if r.returncode != 0:
            return False
    except _CommandCapacityExhausted:
        # Local helper saturation says nothing about the SSH master. Treat an
        # existing socket conservatively as alive so five skipped probes can
        # never accumulate into a destructive restart that yanks the user.
        return True
    except Exception:
        return False
    if not deep:
        return True
    try:
        r = _hard_run(
            ['ssh', '-o', f'ControlPath={ctl}', '-o', 'BatchMode=yes', node, 'true'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=DEEP_PROBE_TIMEOUT,
        )
        return r.returncode == 0
    except _CommandCapacityExhausted:
        return True
    except Exception:
        return False


def _kill_master(node: str) -> None:
    """Tear down a (possibly hung) ControlMaster and remove its socket.
    Serialized per-node so it can't race with _start_master."""
    with _node_master_lock(node):
        _kill_master_unsafe(node)


def _master_pid(node: str) -> int | None:
    """Ask the master itself for its PID via `ssh -O check`. Output looks
    like `Master running (pid=12345)`. Returns None if not running."""
    ctl = _ctl_path(node)
    if not os.path.exists(ctl):
        return None
    try:
        out = _hard_check_output(
            ['ssh', '-o', f'ControlPath={ctl}', '-O', 'check', node],
            universal_newlines=True, stderr=subprocess.STDOUT, timeout=3,
        )
        m = re.search(r'pid=(\d+)', out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _kill_master_unsafe(node: str) -> None:
    ctl = _ctl_path(node)
    # Snag the master's PID before we tell it to exit — if it doesn't
    # respond gracefully we'll fall back to SIGKILL on the PID directly.
    master_pid = _master_pid(node)
    master_token = lifecycle.process_token(master_pid) if master_pid else None
    if os.path.exists(ctl):
        try:
            _hard_run(
                ['ssh', '-o', f'ControlPath={ctl}', '-O', 'exit', node],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            )
        except Exception:
            pass
    try:
        os.unlink(ctl)
    except OSError:
        pass
    # `ssh -O exit` only exits cleanly when the master is responsive. If
    # it was hung, kill via tracked Popen, then pgrep as a backstop, then
    # the directly-known master PID.
    _kill_orphan_master_proc(node)
    _kill_ssh_with_ctl_path(node)
    if master_pid:
        # Give it a brief moment to wind down; SIGKILL if still alive.
        for _ in range(10):
            if not lifecycle.same_process(master_pid, master_token):
                return  # gone
            time.sleep(0.05)
        lifecycle.signal_same_process(master_pid, master_token, signal.SIGKILL)


def _ssh_master_matches(pid: int, node: str) -> bool:
    """Whether one PID is an ssh master for exactly this ControlPath."""
    ctl_arg = os.fsencode(f'ControlPath={_ctl_path(node)}')
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            argv = [arg for arg in f.read().split(b'\x00') if arg]
    except OSError:
        return False
    if not argv or os.path.basename(os.fsdecode(argv[0])) != 'ssh':
        return False
    return (ctl_arg in argv and b'-N' in argv
            and b'ControlMaster=yes' in argv)


def _ssh_master_pids(node: str) -> list[int]:
    try:
        pids = [int(name) for name in os.listdir('/proc') if name.isdigit()]
    except OSError:
        return []
    return [pid for pid in pids
            if pid != os.getpid() and _ssh_master_matches(pid, node)]


def _kill_ssh_with_ctl_path(node: str) -> int:
    """Terminate orphan *master* ssh processes for this ControlPath.

    The old ``pgrep -f ControlPath=...`` matched every slave too: previews,
    warm shells, and the user's interactive attach all carry the same option.
    It also interpreted dots in paths as regex wildcards.  Exact argv matching
    plus ``-N``/``ControlMaster=yes`` limits teardown to the process we mean.
    """
    targets = [(pid, lifecycle.process_token(pid))
               for pid in _ssh_master_pids(node)]
    killed = 0
    for pid, token in targets:
        if (not lifecycle.same_process(pid, token)
                or not _ssh_master_matches(pid, node)):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    deadline = time.monotonic() + 0.5
    while (time.monotonic() < deadline
           and any(lifecycle.same_process(pid, token)
                   and _ssh_master_matches(pid, node)
                   for pid, token in targets)):
        time.sleep(0.05)
    # Revalidate argv immediately before SIGKILL so PID reuse cannot redirect
    # the signal to an unrelated process.
    for pid, token in targets:
        if (lifecycle.same_process(pid, token)
                and _ssh_master_matches(pid, node)):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return killed


def _cleanup_orphan_sockets() -> None:
    """At startup, decide what to do with each leftover ControlMaster socket.

    Healthy masters are *adopted* (left alone) so a user attached to a tmux
    session via the previous daemon doesn't get yanked when they `atd
    restart`. A single failed check of a still-owned socket is deferred to the
    normal health failure streak; only ownerless stale socket files are removed
    immediately.
    """
    if not os.path.isdir(CTL_DIR):
        return
    entries = [entry for entry in os.listdir(CTL_DIR) if entry.startswith('cm_')]
    # Long hostnames use a hashed socket filename. Recover those names from the
    # last state snapshot when possible. If the runtime state was also lost,
    # defer the unknown socket: a current node will be adopted/restarted by the
    # normal discovery+health loops, while an ended master's ControlPersist
    # will expire naturally. Guessing a hostname here would kill healthy users.
    reverse = {}
    try:
        old_state = _read_json_dict(STAT_FILE, _STATE_FILE_LIMIT)
        old_nodes = old_state.get('nodes', {}) if isinstance(old_state, dict) else {}
        if isinstance(old_nodes, dict):
            for old_node in old_nodes:
                if isinstance(old_node, str) and HOST_RE.fullmatch(old_node):
                    reverse[os.path.basename(_ctl_path(old_node))] = old_node
    except Exception:
        pass
    nodes = []
    for entry in entries:
        raw_node = entry[3:]
        if re.fullmatch(r'h-[0-9a-f]{32}', raw_node):
            node = reverse.get(entry)
            if node is None:
                log.info(f'Deferring unmapped hashed orphan socket {entry}')
                continue
        else:
            node = raw_node
        if not HOST_RE.fullmatch(node):
            # Never pass a crafted socket filename through to ssh as a host or
            # option. The private ctl directory should contain only our files.
            try:
                os.unlink(os.path.join(CTL_DIR, entry))
            except OSError:
                pass
            log.warning(f'Removed invalid ControlMaster socket name {entry!r}')
            continue
        nodes.append(node)

    def clean(node):
        # A shallow mux check is deliberately enough.  A real remote command
        # can time out on a busy compute node even while an interactive tmux
        # channel is healthy; killing that master during `atd restart` would
        # yank the user's session.  ServerAlive on the master owns TCP death
        # detection, and the health loop requires a failure streak.
        if _master_alive(node):
            log.info(f'Adopting healthy orphan master for {node}')
            return
        if _ssh_master_pids(node):
            log.info(f'Deferring unresponsive orphan master for {node} to health streak')
            return
        try:
            os.unlink(_ctl_path(node))
        except OSError:
            pass
        log.info(f'Cleaned ownerless stale master socket for {node}')

    # Fixed-size waves: no one-thread-per-socket explosion, while unlike the
    # periodic fair batches this one-shot startup sweep eventually visits every
    # socket whose earlier wave completed normally.
    for i in range(0, len(nodes), 6):
        if _stop_event.is_set():
            return
        threads = [t for node in nodes[i:i + 6]
                   if (t := _bounded_daemon_thread(
                       clean, (node,), _cleanup_semaphore,
                       f'cleanup-{node}')) is not None]
        _join_threads_until(
            threads, SHALLOW_CHECK_TIMEOUT + CONNECT_TIMEOUT + 15)


def _kill_orphan_master_proc(node: str) -> None:
    """Terminate the previous Popen for this node, if any. Prevents a
    pile-up of `ssh -N ... ControlMaster=yes` processes that didn't
    manage to bind the socket within the deadline."""
    prev = _master_procs.pop(node, None)
    if prev is None:
        return
    if prev.poll() is not None:
        return  # already exited
    try:
        prev.terminate()
        try:
            prev.wait(timeout=2)
        except subprocess.TimeoutExpired:
            prev.kill()
            try:
                prev.wait(timeout=2)
            except subprocess.TimeoutExpired:
                lifecycle.defer_popen_reap(prev)
    except Exception:
        pass


def _start_master(node: str) -> bool:
    """Start a ControlMaster for node. Serialized per-node so concurrent
    calls (e.g., _squeue_loop and _health_loop racing on the same dead
    master) don't both fork a new ssh and leak one. Returns True on
    success."""
    with _node_master_lock(node):
        # The liveness decision made by the caller may already be stale: the
        # health and discovery loops race by design.  Recheck *inside* the
        # per-node lock so the loser adopts the winner instead of killing and
        # replacing the master it just created.
        if _master_alive(node):
            return True
        # Never unlink an extant socket from this one-shot start path.  A
        # shallow check can time out transiently; only the health streak's
        # explicit restart path is allowed to tear such a master down.
        if os.path.exists(_ctl_path(node)):
            return False
        return _start_master_unsafe(node)


def _restart_master(node: str) -> bool:
    """Atomically tear down and recreate one master's socket/process.

    Keeping kill+start under one per-node lock closes the gap where another
    loop could create a good master and then have it immediately removed by
    the original restarter.
    """
    with _node_master_lock(node):
        with _lock:
            if node not in _known_nodes_info:
                return False
        _kill_master_unsafe(node)
        if _stop_event.is_set():
            return False
        with _lock:
            if node not in _known_nodes_info:
                return False
        return _start_master_unsafe(node)


def _start_master_unsafe(node: str) -> bool:
    """Tracks the Popen handle so the next call (or `_kill_master`) can
    actually reap it. Also records a short `last_error` message in
    _known_nodes_info[node] so the frontend can surface it.

    NOTE: we deliberately do NOT pgrep-kill ssh processes here. Caller is
    responsible for ensuring the previous master is dead (typically via
    `_kill_master` or because shallow `_master_alive` returned False).
    Killing-by-argv from inside _start_master is a footgun: a transient
    shallow-check timeout on a busy login node can make `_ensure_master`
    decide a healthy master is dead, and pgrep-killing yanks an
    interactive ssh slave that the user is sitting in.
    """
    _kill_orphan_master_proc(node)
    ctl = _ctl_path(node)
    try:
        os.unlink(ctl)
    except OSError:
        pass
    try:
        proc = subprocess.Popen(
            ['ssh', '-N',
             '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new',
             '-o', f'ConnectTimeout={CONNECT_TIMEOUT}',
             '-o', 'ControlMaster=yes',
             '-o', f'ControlPath={ctl}',
             '-o', f'ControlPersist={CTL_PERSIST}',
             # Keepalive prevents NAT/firewalls from silently dropping
             # an idle TCP connection — which otherwise yanks every
             # interactive ssh slave (including the user's bash).
             '-o', f'ServerAliveInterval={SERVER_ALIVE_INT}',
             '-o', f'ServerAliveCountMax={SERVER_ALIVE_MAX}',
             node],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _master_procs[node] = proc
        # Give SSH a moment to bind the socket
        for _ in range(int(CONNECT_TIMEOUT * 10)):
            if _stop_event.wait(timeout=0.1):
                _kill_orphan_master_proc(node)
                return False
            if os.path.exists(ctl):
                _record_error(node, None)
                return True
            if proc.poll() is not None:
                _master_procs.pop(node, None)
                _record_error(node, f'ssh exited before binding socket (rc={proc.returncode})')
                return False
        # Failed to bind in time — clean up the spawn we just made so
        # it doesn't sit around as a zombie.
        _kill_orphan_master_proc(node)
        _record_error(node, f'master did not bind socket within {CONNECT_TIMEOUT}s')
        return False
    except Exception as e:
        msg = f'spawn failed: {e}'
        log.warning(f'{node}: {msg}')
        _record_error(node, msg)
        return False


_ERROR_PRIORITY = ('master', 'discovery', 'session', 'snapshot', 'preview')


def _set_info_error_locked(info: dict, source: str,
                           err: str | None) -> None:
    """Update one subsystem error and derive the compatibility last_error."""
    errors = info.get('errors')
    if not isinstance(errors, dict):
        errors = {}
    else:
        errors = dict(errors)
    if err is None:
        errors.pop(source, None)
    else:
        errors[source] = ' '.join(str(err).split())[:200]
    if errors:
        info['errors'] = errors
        selected = next(
            (errors[key] for key in _ERROR_PRIORITY if errors.get(key)),
            next(iter(errors.values())),
        )
        info['last_error'] = selected
    else:
        info.pop('errors', None)
        info.pop('last_error', None)


def _record_error(node: str, err: str | None,
                  source: str = 'master') -> None:
    """Set or clear one subsystem error without erasing another's failure."""
    with _lock:
        info = _known_nodes_info.get(node)
        if info is None:
            return
        _set_info_error_locked(info, source, err)


def _clear_error_prefix(node: str, prefix: str) -> None:
    """Clear a stale error only when it belongs to the recovered subsystem."""
    with _lock:
        info = _known_nodes_info.get(node)
        if info is None:
            return
        errors = info.get('errors')
        if isinstance(errors, dict):
            for source, value in list(errors.items()):
                if str(value).startswith(prefix):
                    _set_info_error_locked(info, source, None)
                    return
        if str(info.get('last_error', '')).startswith(prefix):
            info.pop('last_error', None)


# Guards _master_backoff and _master_failure_streak — both are read-modify-
# written from the squeue ensure-threads and the health thread concurrently.
_backoff_lock = threading.Lock()


def _backoff_should_skip(node: str) -> bool:
    with _backoff_lock:
        bk = _master_backoff.get(node)
        return bool(bk and time.monotonic() < bk['next_try'])


def _backoff_record_failure(node: str) -> float:
    with _backoff_lock:
        bk = _master_backoff.get(node, {'next_try': 0.0, 'fails': 0})
        bk['fails'] += 1
        # Once capped, do not keep constructing exponentially huge integers on
        # a daemon that has been running against a dead host for months.
        previous = bk.get('delay', 0.0)
        delay = min(BACKOFF_BASE if previous <= 0 else previous * 2, BACKOFF_CAP)
        bk['delay'] = delay
        bk['next_try'] = time.monotonic() + delay
        _master_backoff[node] = bk
        return delay


def _backoff_clear(node: str) -> None:
    with _backoff_lock:
        _master_backoff.pop(node, None)


def _streak_bump(node: str) -> int:
    """Increment and return the consecutive-failure streak for `node`."""
    with _backoff_lock:
        n = _master_failure_streak.get(node, 0) + 1
        _master_failure_streak[node] = n
        return n


def _streak_clear(node: str) -> None:
    with _backoff_lock:
        _master_failure_streak.pop(node, None)


def _cleanup_gone_node(node: str) -> None:
    """Drop all per-node bookkeeping when a node leaves squeue.

    Without this, master Popen handles, backoff entries, the
    ControlMaster socket, and the health-check failure streak all
    persist for hours after the slurm job ended.
    """
    if node == 'localhost':
        return
    with _node_master_lock(node):
        # The host may have been reallocated while this cleanup waited behind a
        # health/start operation.  In that case its new master is now live data,
        # not an orphan to kill.
        with _lock:
            if node in _known_nodes_info:
                return
            _session_generation.pop(node, None)
        _backoff_clear(node)
        _streak_clear(node)
        _network_coordinator.drop(node)
        _kill_master_unsafe(node)


# ── node discovery ────────────────────────────────────────────────────────────

def _discover_nodes() -> tuple[dict, bool]:
    """Return ``(nodes, complete)`` from squeue.

    ``complete`` is false on a command/parse/nodelist-expansion failure.  That
    distinction is crucial: an empty *successful* squeue means allocations
    ended, while a failed squeue says nothing and must not age every known node
    toward destructive cleanup.
    """
    nodes = {
        'localhost': {
            'time': '-',
            'job_name': 'local',
            'job_id': '-',
            'state': 'LOCAL',
        }
    }
    complete = True
    try:
        # \x1f (ASCII unit separator) delimits fields — a plain '|' can appear
        # inside a job name (%j) or reason (%R) and would shift every later
        # field (e.g. STATUS would show the username). Job names can't contain
        # control chars, so \x1f is collision-proof.
        out = _hard_check_output(
            ['squeue', '-u', _USER, '-h', '-o',
             '%N\x1f%L\x1f%j\x1f%i\x1f%P\x1f%u\x1f%T\x1f%M\x1f%D\x1f%R'],
            universal_newlines=True, timeout=SQUEUE_TIMEOUT,
        )
        for raw_line in out.splitlines():
            # Do not call strip() on the whole record: Python classifies the
            # ASCII unit separator (\x1f) as whitespace. A PENDING row starts
            # with an empty %N field, so strip() removed its leading delimiter,
            # shifted every column, and incorrectly marked the entire poll
            # incomplete. That prevented ended nodes from ever aging out while
            # the user had any pending job.
            line = raw_line.rstrip('\r\n')
            if not line.strip():
                continue
            parts = line.split('\x1f', 9)
            if len(parts) < 10:
                complete = False
                log.warning(f'malformed squeue row (expected 10 fields): {line[:200]!r}')
                continue
            node_part = parts[0].strip()
            if not node_part or node_part.startswith('(') or node_part in {'n/a', 'N/A', 'None assigned'}:
                continue
                
            info = {
                'time': parts[1].strip(),
                'job_name': parts[2].strip(),
                'job_id': parts[3].strip(),
                'state': parts[6].strip(),
            }

            if '[' in node_part or ',' in node_part:
                if not HOST_EXPR_RE.fullmatch(node_part):
                    complete = False
                    log.warning(f'invalid nodelist expression {node_part!r}; skipping')
                    continue
                try:
                    expanded = _hard_check_output(
                        ['scontrol', 'show', 'hostnames', node_part],
                        universal_newlines=True, timeout=5,
                    )
                    expanded_nodes = [n.strip() for n in expanded.splitlines() if n.strip()]
                    if not expanded_nodes:
                        complete = False
                    for n in expanded_nodes:
                        if HOST_RE.fullmatch(n):
                            # Each expanded host needs its OWN info dict —
                            # sharing one object makes the session loop's
                            # per-node sessions/load overwrite every sibling.
                            nodes[n] = dict(info)
                        else:
                            complete = False
                            log.warning(f'scontrol returned invalid hostname {n!r}; skipping')
                except Exception:
                    # scontrol unavailable — don't insert the raw bracket/comma
                    # expression as a "node": it can never be ssh'd or probed,
                    # so it would sit on the dashboard as a permanent phantom
                    # offline node masking the real hosts. Drop it instead.
                    complete = False
                    log.warning(f'could not expand nodelist {node_part!r}; skipping')
            elif HOST_RE.fullmatch(node_part):
                nodes[node_part] = info
            else:
                complete = False
                log.warning(f'invalid hostname from squeue {node_part!r}; skipping')
    except Exception as e:
        log.warning(f'squeue error: {e}')
        complete = False
    return nodes, complete


def _get_nodes() -> dict:
    """Compatibility/pure-test wrapper returning just the discovered mapping."""
    return _discover_nodes()[0]


# ── daemon main loop ──────────────────────────────────────────────────────────

_known_nodes_info: dict = {}
_master_backoff: dict = {}    # node -> {'next_try': epoch, 'fails': N}
_master_procs: dict = {}      # node -> Popen object for the spawned master
_master_failure_streak: dict = {}  # node -> consecutive shallow-check failures
# Weak values prevent an ever-growing hostname->lock registry without deleting
# a lock while another thread still holds a reference to it (which would allow
# two different locks to guard the same node).
_node_master_locks = weakref.WeakValueDictionary()  # node -> threading.Lock
_gone_node_streak: dict = {}   # node -> consecutive squeue misses
_gone_cleanup_pending: set = set()
_gone_cleanup_active: set = set()
_gone_cleanup_lock = threading.Lock()
_session_generation: dict = {}  # node -> newest dispatched session query
_squeue_text: dict = {
    'long': '', 'pending': '', 'updated': '', 'updated_monotonic': None,
}
_keepalive_health: dict = {
    'enabled': True,
    'in_progress': False,
    'last_attempt_monotonic': None,
    'last_success_monotonic': None,
    'last_error': '',
}
_warm_cleanup_health: dict = {
    'last_sweep_monotonic': None,
    'registered_killed': 0,
    'legacy_killed': 0,
    'stale_records': 0,
    'errors': 0,
}
_lock = threading.Lock()
_status_write_lock = threading.Lock()
_snapshot_file_lock = threading.Lock()
_stop_event = threading.Event()


_RESTORED_INFO_FIELDS = (
    'time', 'job_name', 'job_id', 'state', 'nproc', 'load', 'escape_time',
)


def _restore_last_known_state() -> int:
    """Seed a new daemon from its last owned status snapshot.

    Slurm/controller outages frequently coincide with daemon restarts. Starting
    from an empty map made every remote row disappear until ``squeue`` recovered
    even though healthy ControlMasters and cached session listings still
    existed. Restored rows are explicitly marked and are replaced (or aged out
    through the normal two-poll grace) as soon as discovery succeeds.
    """
    try:
        previous = _read_json_dict(STAT_FILE, _STATE_FILE_LIMIT)
    except (FileNotFoundError, OSError, ValueError, TypeError, UnicodeError):
        return 0
    raw_nodes = previous.get('nodes') if isinstance(previous, dict) else None
    if not isinstance(raw_nodes, dict):
        return 0
    restored = {}
    for node, payload in list(raw_nodes.items())[:4096]:
        if (not isinstance(node, str) or not HOST_RE.fullmatch(node)
                or not isinstance(payload, dict)):
            continue
        raw_info = payload.get('info')
        if not isinstance(raw_info, dict):
            continue
        info = {}
        for field in _RESTORED_INFO_FIELDS:
            value = raw_info.get(field)
            if isinstance(value, str):
                info[field] = value[:4096]
        raw_sessions = raw_info.get('sessions', payload.get('sessions', []))
        sessions = []
        if isinstance(raw_sessions, list):
            for item in raw_sessions[:4096]:
                if (not isinstance(item, list) or not item
                        or not isinstance(item[0], str)
                        or not item[0]
                        or len(item[0].encode(
                            'utf-8', errors='surrogatepass')) > 4096):
                    continue
                windows = item[1] if len(item) > 1 else '?'
                if not isinstance(windows, str):
                    windows = str(windows)[:32]
                sessions.append([item[0], windows[:32]])
        info['sessions'] = sessions
        info['restored_from_cache'] = True
        _set_info_error_locked(
            info, 'discovery',
            'using last-known sessions while Slurm discovery recovers')
        restored[node] = info
    if not restored:
        return 0
    with _lock:
        for node, info in restored.items():
            _known_nodes_info.setdefault(node, info)
        for key, limit in (('squeue_long', 4 * 1024 * 1024),
                           ('squeue_pending', 4 * 1024 * 1024)):
            value = previous.get(key)
            if isinstance(value, str):
                target = 'long' if key == 'squeue_long' else 'pending'
                _squeue_text[target] = value[:limit]
        updated = previous.get('squeue_updated')
        if isinstance(updated, str):
            _squeue_text['updated'] = updated[:128]
        updated_mono = previous.get('squeue_updated_monotonic')
        if (isinstance(updated_mono, (int, float))
                and not isinstance(updated_mono, bool)
                and math.isfinite(float(updated_mono))):
            _squeue_text['updated_monotonic'] = float(updated_mono)
    log.info(f'restored {len(restored)} last-known node(s) before discovery')
    return len(restored)


def _mark_node_missing(node: str) -> bool:
    """Bump the consecutive-miss streak for `node`. Returns True iff the
    streak has reached GONE_NODE_THRESHOLD and we should now clean up.
    Without this grace period, a single squeue blip (transient timeout
    or empty result) kills every master and yanks every attached user."""
    n = _gone_node_streak.get(node, 0) + 1
    _gone_node_streak[node] = n
    return n >= GONE_NODE_THRESHOLD


def _mark_node_seen(node: str) -> None:
    _gone_node_streak.pop(node, None)


def _sweep_warm_orphans() -> dict:
    """Reap only identity-verified warm SSH children with no live owner."""
    try:
        stats = warm_registry.sweep(WARM_DIR, CTL_DIR, include_legacy=True)
    except Exception as error:
        log.exception('warm SSH orphan sweep failed')
        stats = {'registered_killed': 0, 'legacy_killed': 0,
                 'stale_records': 0, 'errors': 1, 'detail': str(error)}
    with _lock:
        _warm_cleanup_health['last_sweep_monotonic'] = time.monotonic()
        for key in ('registered_killed', 'legacy_killed',
                    'stale_records', 'errors'):
            _warm_cleanup_health[key] = int(
                _warm_cleanup_health.get(key, 0)) + int(stats.get(key, 0))
        _warm_cleanup_health['last'] = dict(stats)
    killed = int(stats.get('registered_killed', 0)) + int(
        stats.get('legacy_killed', 0))
    if killed:
        log.warning(f'reaped {killed} orphan warm SSH process(es): {stats}')
    return stats


def _warm_orphan_loop() -> None:
    """Continuously provide defense in depth for crashed frontends."""
    while not _stop_event.is_set():
        _sweep_warm_orphans()
        _write_status()
        if _stop_event.wait(timeout=WARM_ORPHAN_INTERVAL):
            return


def _merge_discovery(node_infos: dict, complete: bool) -> tuple[list, list]:
    """Merge one discovery result; return ``(gone, all_known_nodes)``.

    This pure-ish boundary makes the destructive distinction between an empty
    successful poll and a failed/incomplete poll directly regression-testable.
    """
    with _lock:
        for node, info in node_infos.items():
            old = _known_nodes_info.get(node, {})
            if old.get('job_id') != info.get('job_id'):
                continue
            for key in (
                    'sessions', 'nproc', 'load', 'escape_time', 'last_error',
                    'errors'):
                if key in old:
                    info[key] = old[key]
            # This node was present in the current controller response even if
            # another row made the overall poll incomplete. It is no longer a
            # startup-cache-only row.
            _set_info_error_locked(info, 'discovery', None)
        for node in node_infos:
            _mark_node_seen(node)
        if complete:
            missing = set(_known_nodes_info) - set(node_infos)
            gone = [node for node in missing if _mark_node_missing(node)]
        else:
            gone = []
            _gone_node_streak.clear()
        for node in gone:
            _known_nodes_info.pop(node, None)
        _known_nodes_info.update(node_infos)
        return gone, list(_known_nodes_info)


def _schedule_gone_cleanup(nodes) -> int:
    """Start available gone-node cleanups without delaying state publication.

    Teardown can spend seconds probing/killing one stuck SSH process. Doing it
    serially in the squeue loop made a large allocation disappear from the TUI
    minutes late. Pending work is retained across polls while six fixed slots
    cap process/thread pressure.
    """
    with _gone_cleanup_lock:
        _gone_cleanup_pending.update(
            node for node in nodes if node != 'localhost')
    started = 0
    while True:
        with _gone_cleanup_lock:
            available = _gone_cleanup_pending - _gone_cleanup_active
            if not available:
                break
            node = min(available)
            _gone_cleanup_pending.discard(node)
            _gone_cleanup_active.add(node)

        def clean_and_release(target=node):
            try:
                _cleanup_gone_node(target)
            finally:
                with _gone_cleanup_lock:
                    _gone_cleanup_active.discard(target)

        thread = _bounded_daemon_thread(
            clean_and_release, tuple(), _cleanup_semaphore,
            f'gone-cleanup-{node}')
        if thread is None:
            with _gone_cleanup_lock:
                _gone_cleanup_active.discard(node)
                _gone_cleanup_pending.add(node)
            break
        started += 1
    return started


def _node_master_lock(node: str) -> threading.Lock:
    """Return (lazily creating) a per-node lock that serializes master
    spawn/kill operations. Prevents a race where _squeue_loop and
    _health_loop both decide the master is dead and start two masters
    concurrently — leaving an orphan ssh process untracked."""
    with _lock:
        lock = _node_master_locks.get(node)
        if lock is None:
            lock = threading.Lock()
            _node_master_locks[node] = lock
        return lock

BACKOFF_BASE  = _cfg['backoff_base']  # initial retry delay after a master start fails
BACKOFF_CAP   = _cfg['backoff_cap']   # max retry delay
HEALTH_FAIL_THRESHOLD = 5    # consecutive shallow-check failures before kill+restart


def _load_runtime_configuration(timeout: float = 3.0) -> bool:
    """Load NFS/NSS-backed startup data after daemonization, with a hard bound.

    Import-time reads made even `atd stop` hang when ``~/.config`` or LDAP was
    unhealthy.  Running loaders only after the final fork also avoids forking a
    process that already has helper threads.  A wedged loader is a capped daemon
    thread and defaults keep the service usable.
    """
    global _cfg, _keepalive_mgr, _USER, _notify_cfg
    global SQUEUE_INTERVAL, HEALTH_INTERVAL, DEEP_PROBE_TIMEOUT
    global CONNECT_TIMEOUT, CTL_PERSIST, SQUEUE_TIMEOUT, SNAPSHOT_INTERVAL
    global SESSION_INTERVAL, SERVER_ALIVE_INT, SERVER_ALIVE_MAX
    global GONE_NODE_THRESHOLD, SHALLOW_CHECK_TIMEOUT, BACKOFF_BASE, BACKOFF_CAP
    global NETWORK_BACKOFF_BASE, NETWORK_BACKOFF_CAP, WARM_ORPHAN_INTERVAL
    global _network_coordinator

    cfg_result = {}
    user_result = {}
    cfg_done = threading.Event()
    user_done = threading.Event()

    def load_cfg():
        try:
            cfg_result['daemon'] = config.load()
            cfg_result['keepalive'] = config.load_keepalive()
            cfg_result['notify'] = config.load_notify()
        except BaseException as error:
            cfg_result['error'] = error
        finally:
            cfg_done.set()

    def load_user():
        try:
            user_result['value'] = pwd.getpwuid(_UID).pw_name
        except BaseException as error:
            user_result['error'] = error
        finally:
            user_done.set()

    for target, done, result, name in (
            (load_cfg, cfg_done, cfg_result, 'config-loader'),
            (load_user, user_done, user_result, 'user-loader')):
        try:
            threading.Thread(target=target, daemon=True, name=name).start()
        except BaseException as error:
            result['error'] = error
            done.set()
    deadline = time.monotonic() + max(0.1, timeout)
    cfg_done.wait(timeout=max(0.0, deadline - time.monotonic()))
    user_done.wait(timeout=max(0.0, deadline - time.monotonic()))

    loaded_all = ('daemon' in cfg_result and 'value' in user_result)
    if cfg_done.is_set() and 'daemon' in cfg_result:
        _cfg = cfg_result['daemon']
        ka_cfg = cfg_result['keepalive']
        _notify_cfg = cfg_result['notify']
    else:
        _cfg = dict(config.DEFAULTS)
        ka_cfg = dict(config.KEEPALIVE_DEFAULTS)
        _notify_cfg = dict(config.NOTIFY_DEFAULTS)
        log.warning('configuration load timed out or failed; using defaults')
    if user_done.is_set() and isinstance(user_result.get('value'), str):
        _USER = user_result['value']
    else:
        _USER = str(_UID)
        log.warning(f'username lookup timed out or failed; using uid {_UID}')

    SQUEUE_INTERVAL = _cfg['squeue_interval']
    HEALTH_INTERVAL = _cfg['health_interval']
    DEEP_PROBE_TIMEOUT = _cfg['deep_probe_timeout']
    CONNECT_TIMEOUT = _cfg['connect_timeout']
    CTL_PERSIST = _cfg['ctl_persist']
    SQUEUE_TIMEOUT = _cfg['squeue_timeout']
    SNAPSHOT_INTERVAL = _cfg['snapshot_interval']
    SESSION_INTERVAL = _cfg['session_interval']
    SERVER_ALIVE_INT = _cfg['server_alive_int']
    SERVER_ALIVE_MAX = _cfg['server_alive_max']
    GONE_NODE_THRESHOLD = _cfg['gone_node_threshold']
    SHALLOW_CHECK_TIMEOUT = _cfg['shallow_check_timeout']
    BACKOFF_BASE = _cfg['backoff_base']
    BACKOFF_CAP = _cfg['backoff_cap']
    NETWORK_BACKOFF_BASE = _cfg['network_backoff_base']
    NETWORK_BACKOFF_CAP = _cfg['network_backoff_cap']
    WARM_ORPHAN_INTERVAL = _cfg['warm_orphan_interval']
    _network_coordinator = network.NodeNetworkCoordinator(
        _network_backoff_delays(
            NETWORK_BACKOFF_BASE, NETWORK_BACKOFF_CAP))
    _keepalive_mgr = keepalive.KeepAliveManager(config.KEEPALIVE_PATH, ka_cfg)
    with _lock:
        _keepalive_health.update({
            'enabled': bool(ka_cfg.get('enabled', True)),
            'in_progress': False,
            'last_attempt_monotonic': None,
            'last_success_monotonic': None,
            'last_error': '',
        })
    return loaded_all


def _install_signal_handlers() -> None:
    """Set _stop_event on SIGTERM/SIGINT so loops exit gracefully.
    Ignore SIGHUP — we're a session leader after _daemonize, but a stray
    HUP from a wrapper script should not be allowed to abort us mid-shutdown."""
    def _handler(signum, _frame):
        log.info(f'Received signal {signum}, shutting down')
        _stop_event.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass  # SIGHUP not on Windows; harmless


def _ensure_master(node: str):
    """Ensure a master is running for node; start one if not.

    Honors per-node backoff so we don't hammer a node that's been failing.
    Localhost has no SSH master, so it short-circuits.
    """
    if node == 'localhost':
        return
    if _master_alive(node):
        # This is a genuine successful shallow check and therefore breaks the
        # health loop's *consecutive* failure streak. Without clearing it here,
        # four old failures + one later transient failure could kill a healthy
        # interactive master despite successes in between.
        _streak_clear(node)
        _backoff_clear(node)
        _clear_error_prefix(node, 'ControlMaster check failed')
        return
    if os.path.exists(_ctl_path(node)):
        # An existing socket that missed one shallow check is not permission to
        # unlink it.  The health loop's consecutive-failure threshold is the
        # sole destructive path, protecting interactive slaves from a busy-node
        # false positive.
        _record_error(node, 'ControlMaster check failed; awaiting confirmation')
        return
    if _backoff_should_skip(node):
        return
    log.info(f'Starting ControlMaster for {node}')
    ok = _start_master(node)
    if ok:
        log.info(f'ControlMaster ready for {node}')
        _backoff_clear(node)
    else:
        delay = _backoff_record_failure(node)
        log.warning(f'ControlMaster failed for {node}; next retry in {delay:.0f}s')


SQUEUE_TEXT_LIMIT = 256 * 1024


def _limit_squeue_text(text: str, limit: int = SQUEUE_TEXT_LIMIT) -> str:
    """Bound state-file/UI work for users with exceptionally large queues."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (text[:limit].rstrip()
            + f'\n… squeue output truncated ({omitted:,} characters omitted)\n')


def _get_squeue_text(args: list[str]) -> str:
    """Run a `squeue ...` command and return its raw text output (or an error
    line). Used for the bottom-panel jobs view."""
    try:
        text = _hard_check_output(
            ['squeue', '-u', _USER, *args],
            universal_newlines=True, timeout=SQUEUE_TIMEOUT,
            stderr=subprocess.DEVNULL,
        )
        return _limit_squeue_text(text)
    except subprocess.TimeoutExpired:
        return f'(squeue {" ".join(args)} timed out)'
    except Exception as e:
        return f'(squeue error: {e})'


def _notify_expiring_jobs(node_infos: dict) -> None:
    """Send one reminder per job entering its final `lead_time` seconds.

    Called only with a complete squeue view: a partial poll says nothing about
    a job's remaining time and must not trigger an alarm.  Delivery runs on a
    detached thread so a slow or unreachable webhook cannot stall discovery,
    and a job is recorded as announced only once a POST actually succeeds.
    """
    if not _notify_cfg.get('enabled') or not _notify_cfg.get('webhook_url'):
        return
    # One job can own several nodes; announce the job, not each node.
    jobs = {}
    for node, info in (node_infos or {}).items():
        if not isinstance(info, dict):
            continue
        job_id = str(info.get('job_id') or '').strip()
        if job_id and job_id not in jobs:
            jobs[job_id] = {**info, 'node': node}
    with _notify_lock:
        already = set(_notified_jobs)
        # Forget jobs that have left the queue so the set cannot grow forever.
        _notified_jobs.intersection_update(jobs)
    try:
        due = notify.due_jobs(
            jobs.values(), float(_notify_cfg['lead_time']), already)
    except Exception as error:
        log.warning(f'job reminder check failed: {error}')
        return

    def deliver(job: dict) -> None:
        job_id = str(job.get('job_id') or '')
        # Every login node's daemon polls the same squeue and reaches this
        # point together; the shared claim decides which one actually speaks.
        if not notify.claim_job(config.NOTIFY_CLAIM_DIR, job_id):
            with _notify_lock:
                _notified_jobs.add(job_id)
            log.info(f'job {job_id} reminder already sent by another daemon')
            return
        text = notify.build_message(job, job['remaining'])
        ok, error = notify.post(
            _notify_cfg['webhook_url'], text, float(_notify_cfg['timeout']))
        if ok:
            with _notify_lock:
                _notified_jobs.add(job_id)
            log.info(f'sent expiry reminder for job {job_id}')
            return
        # Hand the claim back too, or no daemon retries until it expires.
        notify.release_claim(config.NOTIFY_CLAIM_DIR, job_id)
        log.warning(f'job {job_id} reminder not delivered: {error}')

    for job in due:
        try:
            threading.Thread(target=deliver, args=(job,), daemon=True,
                             name=f"notify-{job.get('job_id')}").start()
        except RuntimeError as error:
            log.warning(f'could not start reminder thread: {error}')


def _notify_started_jobs(node_infos: dict) -> None:
    """Say when something you queued has started running.

    Only ever called with a complete squeue view, and the first such view a
    daemon sees is *seeded* rather than announced: on a restart every job that
    was already running would otherwise look new. The cost is that a job which
    starts while every daemon is down goes unannounced, which is the right way
    round -- a missed notice beats four spurious ones.
    """
    if (not _notify_cfg.get('enabled') or not _notify_cfg.get('webhook_url')
            or not _notify_cfg.get('job_start')):
        return
    jobs = {}
    for node, info in (node_infos or {}).items():
        if not isinstance(info, dict):
            continue
        job_id = str(info.get('job_id') or '').strip()
        if job_id and job_id not in jobs:
            jobs[job_id] = {**info, 'node': node}
    global _started_seeded
    with _notify_lock:
        seeding = not _started_seeded
        if seeding:
            _started_jobs.clear()
            _started_jobs.update(jobs)
            _started_seeded = True
        else:
            # Drop jobs that have left the queue so the set cannot grow
            # forever, then claim the new ones before releasing the lock.
            _started_jobs.intersection_update(jobs)
            fresh = notify.started_jobs(jobs.values(), set(_started_jobs))
            _started_jobs.update(str(job.get('job_id')) for job in fresh)
    if seeding:
        return

    def deliver(job: dict) -> None:
        job_id = str(job.get('job_id') or '')
        key = f'start:{job_id}'
        if not notify.claim_job(config.NOTIFY_CLAIM_DIR, key):
            return
        ok, error = notify.post(
            _notify_cfg['webhook_url'], notify.build_start_message(job),
            float(_notify_cfg['timeout']))
        if ok:
            log.info(f'sent start notice for job {job_id}')
            return
        notify.release_claim(config.NOTIFY_CLAIM_DIR, key)
        with _notify_lock:
            _started_jobs.discard(job_id)
        log.warning(f'job {job_id} start notice not delivered: {error}')

    for job in fresh:
        try:
            threading.Thread(target=deliver, args=(job,), daemon=True,
                             name=f"start-{job.get('job_id')}").start()
        except RuntimeError as error:
            log.warning(f'could not start notice thread: {error}')


def _idle_tail(entry: dict) -> str:
    """The line the session stopped on, or '' if it cannot be had cheaply.

    Runs on the delivery thread, once per quiet spell rather than per poll, and
    under the same per-node network budget as everything else -- so a node that
    is already struggling spends nothing here. A notice with no quoted line is
    the old notice, which is still worth sending; a notice delayed behind a
    hanging capture is not.
    """
    if not _notify_cfg.get('idle_tail'):
        return ''
    try:
        content = _capture_pane(entry['node'], entry['session'], 'idle-notice')
        return notify.last_output_line(content)
    except Exception as error:
        log.warning(f'idle notice tail unavailable: {error}')
        return ''


def _notify_idle_sessions(node: str, info: dict) -> None:
    """Announce a session that has stopped producing output.

    That is the observable end of a run -- the work finished, or it wedged --
    and it is the thing a user actually wants pushed to them, since noticing
    it otherwise means watching the dashboard.

    Announced once per quiet spell: the local record is cleared as soon as the
    session produces output again, so a long-running job is not re-announced
    every poll, while a session that wakes and stalls again is.
    """
    if not _notify_cfg.get('enabled') or not _notify_cfg.get('webhook_url'):
        return
    threshold = float(_notify_cfg.get('idle_notify') or 0)
    if threshold <= 0:
        return
    try:
        quiet = notify.idle_sessions(node, info, threshold)
    except Exception as error:
        log.warning(f'idle-session check failed: {error}')
        return

    quiet_names = {entry['session'] for entry in quiet}
    with _notify_lock:
        announced = _idle_announced.setdefault(node, set())
        # Re-arm anything that has started moving again.
        announced &= quiet_names
        pending = [entry for entry in quiet
                   if entry['session'] not in announced]
        announced.update(entry['session'] for entry in pending)

    def deliver(entry: dict) -> None:
        # Every login node's daemon watches the same sessions.
        key = f"idle:{entry['node']}:{entry['session']}"
        if not notify.claim_job(config.NOTIFY_CLAIM_DIR, key,
                                ttl=float(_notify_cfg['idle_cooldown'])):
            return
        text = notify.build_idle_message(
            {**entry, 'tail': _idle_tail(entry)},
            link=bool(_notify_cfg.get('attach_link')))
        ok, error = notify.post(
            _notify_cfg['webhook_url'], text, float(_notify_cfg['timeout']))
        if ok:
            log.info(f"sent idle notice for {entry['node']}:"
                     f"{entry['session']} ({entry['idle']}s)")
            return
        # Undo both fences so the next poll can retry rather than losing the
        # notice for a whole cooldown.
        notify.release_claim(config.NOTIFY_CLAIM_DIR, key)
        with _notify_lock:
            _idle_announced.get(entry['node'], set()).discard(entry['session'])
        log.warning(f'idle notice not delivered: {error}')

    for entry in pending:
        try:
            threading.Thread(target=deliver, args=(entry,), daemon=True,
                             name=f"idle-{entry['session'][:16]}").start()
        except RuntimeError as error:
            log.warning(f'could not start idle-notice thread: {error}')


def _squeue_loop():
    """Periodically discover nodes, spin up masters, refresh job listings."""
    last_set: frozenset = frozenset()
    while not _stop_event.is_set():
        try:
            node_infos, discovery_complete = _discover_nodes()
            if discovery_complete:
                _notify_expiring_jobs(node_infos)
                _notify_started_jobs(node_infos)
            # Preserve fields populated by the session loop only for the same
            # job, and never treat incomplete controller data as node loss.
            gone, known_nodes = _merge_discovery(
                node_infos, discovery_complete)
            # Teardown is scheduled outside _lock and never delays publishing
            # the newly-discovered state. Pending cleanup from an earlier full
            # wave is retried even when this poll has no newly-gone nodes.
            _schedule_gone_cleanup(gone)
            for g in gone:
                _gone_node_streak.pop(g, None)
                log.info(f'Node {g} left squeue (>{GONE_NODE_THRESHOLD} misses) '
                         '— cleanup scheduled')
            current_set = frozenset(known_nodes)
            if current_set != last_set:
                added = sorted(current_set - last_set)
                removed = sorted(last_set - current_set)
                msg = f'Nodes ({len(current_set)}): {", ".join(sorted(current_set)) or "(none)"}'
                if added:
                    msg += f'  +{",".join(added)}'
                if removed:
                    msg += f'  -{",".join(removed)}'
                log.info(msg)
                last_set = current_set
            # Start masters in parallel — daemon threads with semaphore-
            # bounded concurrency so we don't burst-fork on clusters with
            # many nodes, and the process can still exit cleanly.
            threads = _start_bounded_batch(
                _ensure_master, [(n,) for n in known_nodes], _ensure_semaphore,
                'ensure', lambda a: f'ensure-{a[0]}')
            _join_threads_until(threads, CONNECT_TIMEOUT + 5)
            # Refresh both jobs-panel views concurrently. When Slurm is down,
            # two sequential timeout+cleanup paths added 30+ seconds to every
            # discovery cycle and made the daemon look frozen.
            texts = {}
            texts_lock = threading.Lock()

            def fetch_text(key, args):
                value = _get_squeue_text(args)
                with texts_lock:
                    texts[key] = value

            text_threads = _start_bounded_batch(
                fetch_text,
                [('long', ['-l']), ('pending', ['-l', '--start'])],
                _squeue_text_semaphore, 'squeue-text',
                lambda a: f'squeue-{a[0]}')
            _join_threads_until(text_threads, SQUEUE_TIMEOUT + 3)
            with texts_lock:
                long_text = texts.get('long', '(squeue -l timed out)')
                pending_text = texts.get(
                    'pending', '(squeue -l --start timed out)')
            with _lock:
                _squeue_text['long'] = long_text
                _squeue_text['pending'] = pending_text
                _squeue_text['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
                _squeue_text['updated_monotonic'] = time.monotonic()
            # Keep-alive: resubmit expiring `占坑` scripts. Runs outside _lock;
            # submits are threaded so a slow sbatch never stalls polling.
            ka_threads = _start_bounded_batch(
                _drive_keepalive, [tuple()], _keepalive_tick_semaphore,
                'keepalive-tick', lambda _a: 'keepalive-tick')
            _join_threads_until(ka_threads, min(5, SQUEUE_TIMEOUT))
            _write_status()
        except Exception:
            log.exception('squeue_loop iteration failed')
        if _stop_event.wait(timeout=SQUEUE_INTERVAL):
            return


def _health_check_node(node: str) -> str:
    """Run one health-check pass for a single node.

    We trust the shallow `ssh -O check` (verifies the local mux socket
    is responsive). We deliberately do NOT do a deep `ssh true` probe —
    on busy compute nodes that probe times out spuriously (busy ≠ broken)
    and would have us killing perfectly good masters every few minutes,
    yanking any user attached through them. TCP-level death is caught
    instead by ssh's own ServerAliveInterval keepalive on the master
    spawn — the master exits itself when keepalives go unanswered, and
    the next shallow check then honestly reports the master as dead.

    A small failure streak still buffers truly transient hiccups (e.g.,
    socket re-binding right after a restart).
    """
    if node == 'localhost':
        return 'skip-localhost'
    with _lock:
        if node not in _known_nodes_info:
            return 'gone'
    if _backoff_should_skip(node):
        return 'skip-backoff'
    if _master_alive(node):
        _streak_clear(node)
        _backoff_clear(node)
        _clear_error_prefix(node, 'ControlMaster check failed')
        return 'alive'
    streak = _streak_bump(node)
    if streak < HEALTH_FAIL_THRESHOLD:
        log.info(f'Master for {node} not responding ({streak}/{HEALTH_FAIL_THRESHOLD})')
        return 'transient-failure'
    log.info(f'Master for {node} unresponsive after {streak} checks, restarting...')
    _streak_clear(node)
    if _restart_master(node):
        log.info(f'Master for {node} restarted')
        _backoff_clear(node)
        return 'restarted'
    with _lock:
        if node not in _known_nodes_info:
            return 'gone'
    delay = _backoff_record_failure(node)
    log.warning(f'Master for {node} failed to restart; next retry in {delay:.0f}s')
    return 'restart-failed'


def _health_loop():
    """Periodically re-check existing masters and restart dead ones."""
    while not _stop_event.is_set():
        if _stop_event.wait(timeout=HEALTH_INTERVAL):
            return
        try:
            with _lock:
                nodes = list(_known_nodes_info.keys())
            threads = _start_bounded_batch(
                _health_check_node, [(n,) for n in nodes], _health_semaphore,
                'health', lambda a: f'health-{a[0]}')
            _join_threads_until(
                threads, SHALLOW_CHECK_TIMEOUT + CONNECT_TIMEOUT + 15)
            _write_status()
        except Exception:
            log.exception('health_loop iteration failed')


def _capture_pane(node: str, session: str,
                  source: str = 'snapshot', history: int = 0) -> str | None:
    """Capture one pane under the shared per-node network budget.

    ``history`` asks for that many lines of scrollback above the visible
    screen. The dashboard's own preview never uses it -- one screen is what
    fits beside the table, and paying for scrollback on every poll would be
    waste -- but reading why something died usually means looking further back
    than the last screenful.
    """
    lease = _network_coordinator.acquire(node, source)
    if lease is None:
        return None
    scroll = ['-S', f'-{int(history)}'] if history > 0 else []
    try:
        if node == 'localhost':
            cmd = ['tmux', 'capture-pane', '-p', '-e', *scroll, '-t', session]
        else:
            if not _master_alive(node):
                lease.failure('ControlMaster unavailable')
                return None
            # ssh ships remaining args as a single string to the remote shell;
            # quote the session in case it contains spaces / metacharacters.
            scroll_args = ' '.join(scroll)
            cmd = ['ssh', '-o', f'ControlPath={_ctl_path(node)}',
                   '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', f'ConnectTimeout={CONNECT_TIMEOUT}',
                   node, f'tmux capture-pane -p -e {scroll_args} '
                         f'-t {shlex.quote(session)}']
        result = _hard_run(
            cmd, timeout=8, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            lease.success()
            content = result.stdout or ''
            if len(content) > _PREVIEW_CONTENT_LIMIT:
                content = ('[... pane output truncated ...]\n'
                           + content[-_PREVIEW_CONTENT_LIMIT:])
            return content
        # A non-255 exit proves SSH transported the command; tmux may simply
        # have removed the session between discovery and capture.
        if node == 'localhost' or result.returncode != 255:
            lease.success()
        else:
            detail = ' '.join((result.stderr or '').split())[:200]
            lease.failure(detail or 'SSH preview command failed')
        return None
    except _CommandCapacityExhausted:
        lease.neutral()
        return None
    except subprocess.TimeoutExpired:
        lease.failure('SSH preview command timed out')
        return None
    except Exception as error:
        lease.failure(f'preview transport error: {error}')
        return None
    finally:
        lease.neutral()


_snapshot_semaphore = threading.Semaphore(6)


def _group_snapshot_pairs(pairs) -> list[tuple[str, tuple[str, ...]]]:
    """Group/deduplicate sessions so one node opens one SSH channel at a time."""
    grouped: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for node, session in pairs:
        node_seen = seen.setdefault(node, set())
        if session in node_seen:
            continue
        node_seen.add(session)
        grouped.setdefault(node, []).append(session)
    return [(node, tuple(sessions)) for node, sessions in grouped.items()]


def _update_snapshot_cache_unlocked(pairs, captured: dict) -> bool:
    """Merge captures into the persistent cache without losing last-good data.

    A missing file is the normal first-run case and must create the cache. Other
    read errors are treated as transient so a partial capture cannot overwrite
    a previously-good file.
    """
    old: dict | None = {}
    needs_normalize = False
    existed = True
    try:
        loaded = _read_json_dict(SNAPSHOT_FILE, _SNAPSHOT_FILE_LIMIT)
        if isinstance(loaded, dict):
            old = {k: v for k, v in loaded.items() if isinstance(v, dict)}
            needs_normalize = len(old) != len(loaded)
        else:
            needs_normalize = True
    except FileNotFoundError:
        existed = False
    except OSError:
        return False
    except (ValueError, TypeError, UnicodeError):
        needs_normalize = True
    merged = dict(old)
    merged.update(captured)
    live_keys = {f'{n}:{s}' for (n, s) in pairs}
    merged = {k: v for k, v in merged.items() if k in live_keys}
    if merged != old or not existed or needs_normalize:
        _atomic_write_json(SNAPSHOT_FILE, merged)
    return True


def _update_snapshot_cache(pairs, captured: dict) -> bool:
    with _snapshot_file_lock:
        return _update_snapshot_cache_unlocked(pairs, captured)


def _update_snapshot_entry(node: str, session: str, content: str) -> bool:
    """Merge one live-preview result without pruning unrelated sessions."""
    with _snapshot_file_lock:
        try:
            old = _read_json_dict(SNAPSHOT_FILE, _SNAPSHOT_FILE_LIMIT)
            if not isinstance(old, dict):
                old = {}
        except (FileNotFoundError, ValueError, TypeError, UnicodeError):
            old = {}
        except OSError:
            return False
        old[f'{node}:{session}'] = {
            'lines': content,
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'captured_epoch': time.time(),
            'captured_monotonic': time.monotonic(),
            'monotonic_clock_id': _CLOCK_ID,
        }
        try:
            _atomic_write_json(SNAPSHOT_FILE, old)
            return True
        except Exception:
            log.exception('failed to update live preview cache')
            return False


_preview_server_slots = threading.Semaphore(8)


def _preview_session_known(node: str, session: str) -> bool:
    with _lock:
        info = _known_nodes_info.get(node)
        if info is None:
            return False
        return any(
            isinstance(item, list) and item and item[0] == session
            for item in info.get('sessions', [])
        )


def _run_node_tmux(node: str, tmux_args: list[str], source: str,
                   timeout: float = 8.0) -> tuple[bool, str]:
    """Run one short tmux command on a node under the shared network budget.

    Mirrors _capture_pane's transport handling: the same per-node lease, the
    same rule that only SSH's own status 255 counts as a transport failure, so
    a tmux error ("session not found") never trips the circuit breaker for the
    whole node.
    """
    lease = _network_coordinator.acquire(node, source)
    if lease is None:
        return False, 'node is busy with other work'
    try:
        if node == 'localhost':
            cmd = ['tmux'] + list(tmux_args)
        else:
            if not _master_alive(node):
                lease.failure('ControlMaster unavailable')
                return False, 'no SSH connection to the node'
            # ssh joins remaining args into one string for the remote shell.
            remote = 'tmux ' + ' '.join(shlex.quote(a) for a in tmux_args)
            cmd = ['ssh', '-o', f'ControlPath={_ctl_path(node)}',
                   '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', f'ConnectTimeout={CONNECT_TIMEOUT}', node, remote]
        result = _hard_run(cmd, timeout=timeout, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        detail = ' '.join((result.stderr or '').split())[:200]
        if result.returncode == 0:
            lease.success()
            return True, ''
        if node == 'localhost' or result.returncode != 255:
            lease.success()
            return False, detail or 'tmux refused the command'
        lease.failure(detail or 'SSH command failed')
        return False, detail or 'SSH command failed'
    except _CommandCapacityExhausted:
        lease.neutral()
        return False, 'the node command pool is full'
    except subprocess.TimeoutExpired:
        lease.failure('tmux command timed out')
        return False, 'tmux command timed out'
    except Exception as error:
        detail = ' '.join(str(error).split())[:200] or 'tmux command failed'
        lease.failure(detail)
        return False, detail


def _apply_session_change(node: str, session: str, verb: str) -> None:
    """Reflect a kill/new in the published state without waiting for a poll.

    The session list refreshes every 15s; leaving a killed session on screen
    that long makes the key look like it did nothing, and invites a second
    press. The next real probe overwrites this either way, so an optimistic
    edit can only be briefly wrong, never durably.
    """
    with _lock:
        info = _known_nodes_info.get(node)
        if not isinstance(info, dict):
            return
        sessions = [item for item in info.get('sessions', [])
                    if isinstance(item, list) and item]
        if verb == 'kill':
            info['sessions'] = [item for item in sessions
                                if item[0] != session]
        elif not any(item[0] == session for item in sessions):
            sessions.append([session, '1', int(time.time())])
            info['sessions'] = sessions
    _write_status()


def _handle_session_request(node: str, request: dict) -> dict:
    verb = request.get('verb')
    session = request.get('session')
    if verb not in config.SESSION_VERBS:
        return {'ok': False, 'kind': 'invalid', 'reason': 'invalid session verb'}
    if not isinstance(session, str) or not session:
        return {'ok': False, 'kind': 'invalid', 'reason': 'invalid session'}
    if verb == 'new':
        if not config.NEW_SESSION_RE.fullmatch(session):
            return {'ok': False, 'kind': 'invalid',
                    'reason': 'name must be letters, digits, _ @ + - '
                              '(no ":" or "." — tmux uses those for targets)'}
        args = ['new-session', '-d', '-s', session]
    else:
        if len(session.encode('utf-8', errors='surrogatepass')) > 4096:
            return {'ok': False, 'kind': 'invalid', 'reason': 'invalid session'}
        if not _preview_session_known(node, session):
            return {'ok': False, 'kind': 'not-found',
                    'reason': 'tmux session no longer exists'}
        args = ['kill-session', '-t', session]
    ok, reason = _run_node_tmux(node, args, f'session-{verb}')
    if not ok:
        state = _network_coordinator.snapshot(node)
        return {'ok': False,
                'kind': 'busy' if state.get('busy') else 'unavailable',
                'reason': reason or f'tmux {verb} failed', 'network': state}
    _apply_session_change(node, session, verb)
    return {'ok': True, 'session': session, 'verb': verb}


def _handle_preview_request(request: dict) -> dict:
    action = request.get('action', 'preview')
    node = request.get('node')
    if not isinstance(node, str) or not HOST_RE.fullmatch(node):
        return {'ok': False, 'kind': 'invalid', 'reason': 'invalid node'}
    with _lock:
        known = node in _known_nodes_info
    if not known:
        return {'ok': False, 'kind': 'not-found',
                'reason': 'node is no longer allocated'}

    if action == 'report':
        outcome = request.get('outcome')
        source = 'frontend:' + ' '.join(
            str(request.get('source') or 'attach').split())[:40]
        if outcome == 'success':
            _network_coordinator.report_success(node, source)
        elif outcome == 'failure':
            _network_coordinator.report_failure(
                node, source,
                ' '.join(str(request.get('reason') or
                             'interactive SSH failed').split())[:200])
        else:
            return {'ok': False, 'kind': 'invalid',
                    'reason': 'invalid report outcome'}
        _write_status()
        return {'ok': True, 'network': _network_coordinator.snapshot(node)}

    if action == 'status':
        return {'ok': True, 'network': _network_coordinator.snapshot(node)}
    if action == 'session':
        return _handle_session_request(node, request)
    if action != 'preview':
        return {'ok': False, 'kind': 'invalid', 'reason': 'invalid action'}
    session = request.get('session')
    if (not isinstance(session, str) or not session
            or len(session.encode('utf-8', errors='surrogatepass')) > 4096):
        return {'ok': False, 'kind': 'invalid', 'reason': 'invalid session'}
    if not _preview_session_known(node, session):
        return {'ok': False, 'kind': 'not-found',
                'reason': 'tmux session no longer exists'}

    try:
        history = int(request.get('history') or 0)
    except (TypeError, ValueError):
        history = 0
    history = max(0, min(config.PREVIEW_HISTORY_MAX, history))
    content = _capture_pane(node, session, source='preview', history=history)
    state = _network_coordinator.snapshot(node)
    if content is None:
        kind = 'busy' if state.get('busy') else (
            'backoff' if state.get('retry_in', 0) > 0 else 'unavailable')
        return {
            'ok': False,
            'kind': kind,
            'reason': state.get('reason') or 'preview temporarily unavailable',
            'retry_after': max(0.25, float(state.get('retry_in') or 0.25)),
            'network': state,
        }
    _update_snapshot_entry(node, session, content)
    return {
        'ok': True,
        'content': content,
        'captured_epoch': time.time(),
        'network': state,
    }


def _preview_peer_is_owner(connection: socket.socket) -> bool:
    option = getattr(socket, 'SO_PEERCRED', None)
    if option is None:
        return True
    try:
        _pid, uid, _gid = struct.unpack(
            '3i', connection.getsockopt(socket.SOL_SOCKET, option, 12))
        return uid == _UID
    except (OSError, struct.error):
        return False


def _serve_preview_client(connection: socket.socket) -> None:
    try:
        connection.settimeout(max(12.0, float(CONNECT_TIMEOUT) + 4.0))
        if not _preview_peer_is_owner(connection):
            return
        try:
            request = ipc.recv_json(connection, ipc.MAX_REQUEST_BYTES)
            response = _handle_preview_request(request)
        except Exception as error:
            response = {'ok': False, 'kind': 'protocol',
                        'reason': ' '.join(str(error).split())[:200]}
        try:
            ipc.send_json(connection, response, ipc.MAX_RESPONSE_BYTES)
        except (OSError, ValueError):
            pass
    finally:
        try:
            connection.close()
        finally:
            _preview_server_slots.release()


def _preview_server_loop() -> None:
    """Serve centralized live previews from the daemon's private socket."""
    server = None
    bound_identity = None
    try:
        try:
            old = os.lstat(PREVIEW_SOCKET)
        except FileNotFoundError:
            old = None
        if old is not None:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != _UID:
                log.error(f'refusing unsafe preview socket {PREVIEW_SOCKET!r}')
                return
            os.unlink(PREVIEW_SOCKET)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(PREVIEW_SOCKET)
        os.chmod(PREVIEW_SOCKET, 0o600)
        current = os.lstat(PREVIEW_SOCKET)
        bound_identity = (current.st_dev, current.st_ino)
        server.listen(32)
        server.settimeout(0.5)
        while not _stop_event.is_set():
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if _stop_event.is_set():
                    break
                continue
            if not _preview_server_slots.acquire(blocking=False):
                try:
                    ipc.send_json(connection, {
                        'ok': False, 'kind': 'busy',
                        'reason': 'preview service is busy',
                        'retry_after': 0.5,
                    }, ipc.MAX_RESPONSE_BYTES)
                except OSError:
                    pass
                connection.close()
                continue
            try:
                threading.Thread(
                    target=_serve_preview_client, args=(connection,),
                    daemon=True, name='preview-client').start()
            except BaseException:
                _preview_server_slots.release()
                connection.close()
                raise
    except Exception:
        log.exception('preview IPC server failed')
    finally:
        if server is not None:
            server.close()
        try:
            current = os.lstat(PREVIEW_SOCKET)
            if bound_identity == (current.st_dev, current.st_ino):
                os.unlink(PREVIEW_SOCKET)
        except OSError:
            pass


def _snapshot_loop():
    """Periodically capture a tmux pane for every (node, session) we know
    about and persist them so the frontend has something to show instantly
    on launch and when switching rows. Concurrency is capped at 6 nodes, and
    sessions on the same node are captured serially so one snapshot pass can't
    exhaust that node's SSH ControlMaster session limit."""
    while not _stop_event.is_set():
        try:
            with _lock:
                pairs = []
                for node, info in _known_nodes_info.items():
                    for s in info.get('sessions') or []:
                        if isinstance(s, list) and s:
                            pairs.append((node, s[0]))
            results: dict = {}
            results_lock = threading.Lock()

            def _grab_node(node, sessions):
                for session in sessions:
                    if _stop_event.is_set():
                        return
                    text = _capture_pane(node, session)
                    if text is not None:
                        with results_lock:
                            results[f'{node}:{session}'] = {
                                'lines': text,
                                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                                'captured_epoch': time.time(),
                                'captured_monotonic': time.monotonic(),
                                'monotonic_clock_id': _CLOCK_ID,
                            }

            groups = _group_snapshot_pairs(pairs)
            threads = _start_bounded_batch(
                _grab_node, groups, _snapshot_semaphore,
                'snapshot', lambda a: f'snapshot-{a[0]}')
            max_per_node = max((len(sessions) for _, sessions in groups),
                               default=1)
            _join_threads_until(
                threads,
                min(60.0, SHALLOW_CHECK_TIMEOUT + 2 + 8 * max_per_node))

            # Timed-out worker threads may still finish later; take a stable
            # copy rather than iterating a dict they can mutate concurrently.
            with results_lock:
                captured = dict(results)
            # Merge with existing snapshots so a transient capture failure for
            # one live session retains its previous pane. Pruning still runs
            # when zero captures succeed, removing sessions which are gone.
            if not _update_snapshot_cache(pairs, captured):
                if _stop_event.wait(timeout=SNAPSHOT_INTERVAL):
                    return
                continue
        except Exception:
            log.exception('snapshot_loop iteration failed')
        if _stop_event.wait(timeout=SNAPSHOT_INTERVAL):
            return


def _keepalive_job_rows() -> list:
    """Every job for the user (name|state|time_left), INCLUDING pending ones.

    We can't reuse the node map from _get_nodes(): it's keyed by assigned node
    and drops rows with no nodelist — i.e. every PENDING job. Keep-alive must
    see pending replacements (a just-submitted job sits queued for minutes),
    otherwise it can't tell a replacement is already on the way and would
    resubmit on every poll."""
    out = _hard_check_output(
        ['squeue', '-u', _USER, '-h', '-o', '%i\x1f%j\x1f%T\x1f%L'],
        universal_newlines=True, timeout=SQUEUE_TIMEOUT,
    )
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split('\x1f')   # \x1f: a '|' can appear inside a job name
        if len(parts) < 4:
            # Treat partial controller output as unknown, never as "all jobs
            # disappeared": the latter would submit duplicate allocations for
            # every keep-alive entry.
            raise ValueError(f'malformed keep-alive squeue row: {line[:200]!r}')
        # id/state/time_left never contain the delimiter; only the name (%j)
        # conceivably could, so anchor on the ends and treat the middle as the
        # name — a stray delimiter in a name can't shift it.
        rows.append({'id': parts[0].strip(),
                     'name': '\x1f'.join(parts[1:-2]).strip(),
                     'state': parts[-2].strip(),
                     'time_left': parts[-1].strip()})
    return rows


def _drive_keepalive() -> None:
    """Feed this poll's jobs to the renewal manager. Only queries squeue when
    there's actually an enabled entry. Failures must never break polling."""
    attempted = time.monotonic()
    with _lock:
        _keepalive_health.update({
            'enabled': bool(_keepalive_mgr.cfg.get('enabled', True)),
            'in_progress': True,
            'last_attempt_monotonic': attempted,
        })
    try:
        if _keepalive_mgr.poll_needed():
            _keepalive_mgr.tick(_keepalive_job_rows())
    except Exception as error:
        with _lock:
            _keepalive_health['last_error'] = str(error)[:200]
        log.exception('keep-alive tick failed')
    else:
        with _lock:
            _keepalive_health['last_success_monotonic'] = time.monotonic()
            _keepalive_health['last_error'] = ''
    finally:
        # If NFS/kernel I/O wedges the worker, execution never reaches here;
        # the published old attempt + in_progress flag then lets the TUI warn
        # that renewal checks are stalled instead of claiming protection.
        with _lock:
            _keepalive_health['in_progress'] = False


def _build_status() -> dict:
    """Build one internally-consistent frontend/status snapshot."""
    # Take a deep-ish snapshot under the lock so json.dump can't race with
    # _session_loop / _record_error mutating the inner dicts.
    with _lock:
        snapshot = {
            n: {
                'info': dict(info),
                'sessions': list(info.get('sessions', [])),
                'last_error': info.get('last_error', ''),
            }
            for n, info in _known_nodes_info.items()
        }
        squeue_long = _squeue_text.get('long', '')
        squeue_pending = _squeue_text.get('pending', '')
        squeue_updated = _squeue_text.get('updated', '')
        squeue_updated_monotonic = _squeue_text.get('updated_monotonic')
        keepalive_health = dict(_keepalive_health)
        keepalive_health['interval'] = SQUEUE_INTERVAL
        warm_cleanup_health = dict(_warm_cleanup_health)
    # 'alive' here is a cheap socket-presence check (localhost is always up),
    # NOT a live `ssh -O check` per node. Doing an ssh probe per node on every
    # state write — called from three loops — made a large allocation (100+
    # nodes with wedged sockets) spend minutes here and stall the loops. The
    # health loop owns real liveness: it probes and unlinks dead sockets,
    # so socket-presence tracks reality within one HEALTH_INTERVAL.
    def _alive(n):
        return n == 'localhost' or os.path.exists(_ctl_path(n))
    status = {
        'pid': os.getpid(),
        'user': _USER,
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'updated_monotonic': time.monotonic(),
        'monotonic_clock_id': _CLOCK_ID,
        'squeue_long': squeue_long,
        'squeue_pending': squeue_pending,
        'squeue_updated': squeue_updated,
        'squeue_updated_monotonic': squeue_updated_monotonic,
        'nodes': {
            n: {
                'alive': _alive(n),
                'socket': _ctl_path(n) if n != 'localhost' else '',
                'network': _network_coordinator.snapshot(n),
                **snap,
            }
            for n, snap in snapshot.items()
        },
        'keepalive': _keepalive_mgr.status(),
        'keepalive_health': keepalive_health,
        'warm_cleanup': warm_cleanup_health,
        'ssh_config': {
            'connect_timeout': int(CONNECT_TIMEOUT),
            'server_alive_int': int(SERVER_ALIVE_INT),
            'server_alive_max': int(SERVER_ALIVE_MAX),
        },
    }
    return status


def _write_status() -> bool:
    """Publish status without blocking behind or overtaking another writer.

    Unique temporary files prevent corruption, but did not prevent a slow
    writer with an older snapshot from replacing a newer file after the newer
    writer completed. One non-blocking transaction lock preserves ordering and
    keeps the daemon loops from queueing behind a wedged filesystem write.
    """
    if not _status_write_lock.acquire(blocking=False):
        return False
    try:
        _atomic_write_json(STAT_FILE, _build_status())
        return True
    except Exception:
        log.exception('failed to write state file')
        return False
    finally:
        _status_write_lock.release()


def _serve_until_stopped() -> None:
    """Wait for shutdown while guarding against runtime-directory replacement.

    A detached daemon can outlive its systemd login runtime directory.  Once
    that directory is replaced, its log and lock descriptors refer to orphaned
    inodes and a newly launched frontend looks in a different namespace.  Exit
    after three consecutive validation failures so the stable guard is released
    and frontend recovery can start a clean daemon; recreate a missing ctl child
    while the original base inode is still intact.
    """
    failures = 0
    while not _stop_event.wait(timeout=1):
        try:
            paths.ensure_runtime_dirs()
        except Exception as error:
            failures += 1
            if failures == 1:
                log.warning(f'runtime directory validation failed: {error}')
            if failures >= 3:
                log.error('runtime directory was lost or replaced; shutting down '
                          'so a fresh daemon can recover')
                _stop_event.set()
                return
        else:
            failures = 0


def run_foreground():
    """Run the daemon loops in the foreground (useful for debugging)."""
    print(f'[autotmux_daemon] Running in foreground. Logs: {LOG_FILE}')
    try:
        acquired = _acquire_singleton_lock()
    except OSError as error:
        print(f'[autotmux_daemon] Could not acquire daemon lock: {error}',
              file=sys.stderr)
        return False
    if not acquired:
        print('[autotmux_daemon] Another daemon is already running — refusing to start.')
        return False
    try:
        _stop_event.clear()
        _install_signal_handlers()
        _configure_logging()
        _sweep_stale_tmp()
        _load_runtime_configuration()
        _restore_last_known_state()
        log.info(f'autotmux_daemon starting (pid={os.getpid()}, user={_USER})')
        _write_pid(ready=False)
        # Keep foreground startup/CTRL-C responsive even with many stale mux
        # sockets; cleanup has its own bounded daemon workers.
        threading.Thread(target=_cleanup_orphan_sockets,
                         daemon=True, name='cleanup').start()
        threading.Thread(target=_squeue_loop, daemon=True).start()
        threading.Thread(target=_health_loop, daemon=True).start()
        threading.Thread(target=_session_loop, daemon=True).start()
        threading.Thread(target=_snapshot_loop, daemon=True).start()
        threading.Thread(target=_preview_server_loop, daemon=True,
                         name='preview-server').start()
        threading.Thread(target=_warm_orphan_loop, daemon=True,
                         name='warm-orphan-sweeper').start()
        _write_lock_metadata(os.getpid(), ready=True)
        _serve_until_stopped()
    except KeyboardInterrupt:
        _stop_event.set()
    finally:
        if _read_pid() == os.getpid():
            try:
                os.unlink(PID_FILE)
            except OSError:
                pass
        _release_singleton_lock()
    print('\n[autotmux_daemon] Stopped.')
    return True


# ── daemon fork / control ─────────────────────────────────────────────────────

def _wait_startup_message(fd: int, timeout: float) -> tuple[bool, int | None, str]:
    """Wait for the final daemon's PID and ready/error handshake."""
    deadline = time.monotonic() + max(0.1, timeout)
    pending = b''
    daemon_pid = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, daemon_pid, 'daemon startup timed out'
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            return False, daemon_pid, 'daemon startup timed out'
        chunk = os.read(fd, 1024)
        if not chunk:
            return (False, daemon_pid,
                    'daemon exited before reporting readiness')
        pending += chunk
        if len(pending) > 4096:
            return False, daemon_pid, 'invalid daemon startup response'
        while b'\n' in pending:
            raw_line, pending = pending.split(b'\n', 1)
            line = raw_line.decode('utf-8', errors='replace')
            if line.startswith('PID '):
                try:
                    candidate = int(line[4:])
                except ValueError:
                    return False, daemon_pid, 'invalid daemon startup PID'
                if candidate <= 0:
                    return False, daemon_pid, 'invalid daemon startup PID'
                daemon_pid = candidate
            elif line == 'READY':
                if daemon_pid is None:
                    return False, None, 'daemon omitted its startup PID'
                return True, daemon_pid, ''
            elif line.startswith('ERROR '):
                detail = line[6:].strip() or 'unknown startup error'
                return False, daemon_pid, detail[:500]


def _abort_starting_daemon(pid: int | None) -> None:
    """Stop only the exact detached child advertised by the handshake."""
    if pid is None:
        return
    token = lifecycle.process_token(pid)
    if not lifecycle.is_autotmux_daemon(pid):
        return
    lifecycle.signal_same_process(pid, token, signal.SIGTERM)
    deadline = time.monotonic() + 1.0
    while lifecycle.same_process(pid, token) and time.monotonic() < deadline:
        time.sleep(0.02)
    if lifecycle.same_process(pid, token):
        lifecycle.signal_same_process(pid, token, signal.SIGKILL)


def _notify_startup(fd: int | None, ready: bool, detail: str = '') -> None:
    """Report final-child readiness exactly once and close its pipe."""
    if fd is None:
        return
    message = b'READY\n' if ready else f'ERROR {detail[:500]}\n'.encode(
        'utf-8', errors='replace')
    try:
        os.write(fd, message)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _daemonize() -> int:
    """Double-fork and return a child-side startup notification fd.

    The original controller waits for the final child to finish its critical
    startup sequence, so ``atd start`` cannot return zero merely because the
    first fork succeeded.
    """
    # Flush BEFORE forking — otherwise the buffered output is duplicated into
    # every forked process and printed multiple times when each one exits.
    for fd in (sys.stdout, sys.stderr):
        try:
            fd.flush()
        except Exception:
            pass
    read_fd, write_fd = os.pipe()
    first_pid = os.fork()
    if first_pid > 0:
        os.close(write_fd)
        try:
            ready, daemon_pid, detail = _wait_startup_message(
                read_fd, _DAEMON_READY_TIMEOUT)
        finally:
            os.close(read_fd)
            try:
                os.waitpid(first_pid, os.WNOHANG)
            except OSError:
                pass
        if ready:
            print(f'[autotmux_daemon] Started (pid={daemon_pid})', flush=True)
            os._exit(0)
        _abort_starting_daemon(daemon_pid)
        print(f'[autotmux_daemon] Startup failed: {detail}',
              file=sys.stderr, flush=True)
        os._exit(1)
    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os.close(write_fd)
        os._exit(0)          # first child exits without flushing again
    devnull = open(os.devnull, 'r+')
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(devnull.fileno(), sys.stdout.fileno())
    os.dup2(devnull.fileno(), sys.stderr.fileno())
    # Close the extra handle now that 0/1/2 are dup'd onto it — otherwise the
    # original fd leaks for the daemon's lifetime.
    if devnull.fileno() > 2:
        try:
            devnull.close()
        except OSError:
            pass
    try:
        _write_lock_metadata(os.getpid(), ready=False)
        os.write(write_fd, f'PID {os.getpid()}\n'.encode('ascii'))
    except OSError:
        pass
    return write_fd


_singleton_lock_fd = None  # runtime-dir compatibility lock
_guard_lock_fd = None      # stable /tmp guard; survives XDG dir cleanup


def _acquire_singleton_lock() -> bool:
    """Take an exclusive, non-blocking flock the daemon holds for its whole
    lifetime. Returns False if another daemon already holds it.

    flock is tied to the open file description, so it survives the
    double-fork (inherited fds keep it held) and the kernel auto-releases it
    when the last holder exits — even on SIGKILL. This is the authoritative
    guard against two daemons; the pid file is only advisory.
    """
    global _singleton_lock_fd, _guard_lock_fd
    acquired = []

    def release_partial() -> None:
        for held_fd in acquired:
            try:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(held_fd)
        acquired.clear()

    try:
        for path in (GUARD_FILE, LOCK_FILE):
            fd = lifecycle.open_lock_file(path, create=True)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                release_partial()
                return False
            except BaseException:
                os.close(fd)
                raise
            acquired.append(fd)
    except BaseException:
        release_partial()
        raise
    # Metadata belongs to the lock holder, not merely to the inode. Clear the
    # previous daemon's PID/base before exposing these newly-held locks; a
    # recovery client must not mistake stale JSON for this child's readiness.
    try:
        for fd in acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
    except BaseException:
        release_partial()
        raise
    _guard_lock_fd, _singleton_lock_fd = acquired
    return True


def _release_singleton_lock() -> None:
    global _singleton_lock_fd, _guard_lock_fd
    fds = (_singleton_lock_fd, _guard_lock_fd)
    _singleton_lock_fd = _guard_lock_fd = None
    for fd in fds:
        if fd is None:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _lock_held() -> bool:
    """True if a live daemon holds the singleton flock. Authoritative even when
    the advisory pid file has vanished (systemd cleaning XDG_RUNTIME_DIR between
    logins while the daemon, reparented to init, keeps running)."""
    return (lifecycle.lock_is_held(GUARD_FILE)
            or lifecycle.lock_is_held(LOCK_FILE))


def _write_lock_metadata(pid: int, *, ready: bool) -> None:
    """Publish exact ownership and readiness in the inherited lock inodes."""
    for lock_fd in (_singleton_lock_fd, _guard_lock_fd):
        if lock_fd is None:
            continue
        try:
            payload = (json.dumps({
                'pid': pid, 'base': paths.BASE, 'ready': ready,
            })
                       if lock_fd == _guard_lock_fd else str(pid))
            os.lseek(lock_fd, 0, os.SEEK_SET)
            os.ftruncate(lock_fd, 0)
            os.write(lock_fd, payload.encode('utf-8'))
            os.fsync(lock_fd)
        except OSError:
            log.exception('failed to record pid in singleton lock')


def _write_pid(*, ready: bool = True) -> None:
    pid = os.getpid()
    _atomic_write_json(PID_FILE, pid)
    # Store the PID in the *locked inode* as well.  This remains usable when an
    # aggressive runtime cleanup removes only daemon.pid/state, and lets stop
    # recover a daemon launched by this version without trusting stale files.
    _write_lock_metadata(pid, ready=ready)


def _read_pid() -> int | None:
    return _read_int_file(PID_FILE)


def _pid_running(pid: int) -> bool:
    return lifecycle.pid_running(pid)


def _is_our_daemon(pid: int) -> bool:
    """True only if `pid` looks like an autotmux daemon we own. Guards stop/kill
    against a stale or attacker-planted PID file (see paths._secure_dir): a
    reused PID could otherwise make `atd stop` SIGKILL an unrelated process."""
    return lifecycle.is_autotmux_daemon(pid)


def _read_int_file(path: str) -> int | None:
    try:
        raw = lifecycle.read_owned_regular_file(path, 4096).strip()
        try:
            value = int(raw)
        except ValueError:
            payload = json.loads(raw)
            value = int(payload.get('pid')) if isinstance(payload, dict) else 0
        return value if value > 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _resolve_daemon_pid() -> int | None:
    """Resolve the live daemon PID from strongest to weakest evidence."""
    candidates = []
    if _lock_held():
        for path in (GUARD_FILE, LOCK_FILE):
            # The controller acquired the flock before forking, so /proc/locks
            # may still attribute it to that short-lived parent. The PID
            # written into the inherited inode by the final child is stronger.
            candidates.extend((_read_int_file(path),
                               lifecycle.lock_owner_pid(path)))
    candidates.append(_read_pid())
    try:
        active_base = lifecycle.active_runtime_base(GUARD_FILE)
        active_state = (os.path.join(active_base, 'daemon.json')
                        if active_base else STAT_FILE)
        # PID is the first field in our atomic state document. Do not parse an
        # unbounded/corrupt status file merely to stop the daemon.
        head = lifecycle.read_owned_regular_file(active_state, 4096)
        match = re.search(br'"pid"\s*:\s*(\d+)', head)
        if match:
            candidates.append(int(match.group(1)))
    except (OSError, ValueError):
        pass
    seen = set()
    for pid in candidates:
        if pid in seen or pid is None:
            continue
        seen.add(pid)
        if _pid_running(pid) and _is_our_daemon(pid):
            return pid
    return None


def _remove_pid_if_matches(pid: int) -> None:
    """Remove an old pid file without racing a concurrent new daemon start."""
    fds = []
    current_fd = None
    try:
        for path in (GUARD_FILE, LOCK_FILE):
            current_fd = lifecycle.open_lock_file(path, create=True)
            fcntl.flock(current_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fds.append(current_fd)
            current_fd = None
    except OSError:
        if current_fd is not None:
            os.close(current_fd)
        for fd in fds:
            os.close(fd)
        return
    try:
        if _read_pid() == pid:
            try:
                os.unlink(PID_FILE)
            except OSError:
                pass
    finally:
        for fd in fds:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


_session_semaphore = threading.Semaphore(12)
# A SEPARATE pool for the squeue loop's master-spawn threads, so a slow batch
# of _ensure_master calls can't hold every permit and starve _session_loop
# (and vice-versa) on clusters with many nodes.
_ensure_semaphore = threading.Semaphore(12)
_health_semaphore = threading.Semaphore(12)
_cleanup_semaphore = threading.Semaphore(6)
_keepalive_tick_semaphore = threading.Semaphore(1)
_squeue_text_semaphore = threading.Semaphore(2)
_subprocess_slots = threading.Semaphore(24)
_SUBPROCESS_CLEANUP_GRACE = 2.0
_batch_offsets: dict[str, int] = {}
_batch_lock = threading.Lock()


def _bounded_daemon_thread(target, args, sem: threading.Semaphore, name: str):
    """Spawn a daemon thread only when a concurrency slot is available.

    Acquiring in the child used to create one *waiting thread per node/session*.
    A large allocation could therefore make thousands of threads, and a single
    permanently-hung subprocess caused another waiting cohort every poll.  A
    non-blocking pre-acquire keeps both active and queued thread counts bounded.
    """
    if not sem.acquire(blocking=False):
        return None

    def wrapper():
        try:
            target(*args)
        finally:
            sem.release()
    t = threading.Thread(target=wrapper, daemon=True, name=name)
    try:
        t.start()
    except BaseException:
        sem.release()
        raise
    return t


def _start_bounded_batch(target, task_args, sem, batch_name, name_fn):
    """Fairly rotate tasks and start as many as the fixed slot pool permits."""
    tasks = list(task_args)
    if not tasks:
        return []
    with _batch_lock:
        offset = _batch_offsets.get(batch_name, 0) % len(tasks)
    ordered = tasks[offset:] + tasks[:offset]
    threads = []
    for args in ordered:
        t = _bounded_daemon_thread(target, args, sem, name_fn(args))
        if t is not None:
            threads.append(t)
    # If slots were wedged from a previous cycle, still advance by one so a
    # newly-freed slot does not always favour the first hostname forever.
    with _batch_lock:
        _batch_offsets[batch_name] = (offset + max(1, len(threads))) % len(tasks)
    return threads


def _join_threads_until(threads, timeout: float) -> None:
    """Wait at most ``timeout`` wall-clock seconds for a whole concurrent batch."""
    deadline = time.monotonic() + max(0.0, timeout)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(timeout=remaining)


def _hard_subprocess_call(invoke, argv, *, timeout: float, **kwargs):
    """Invoke a timeout-aware subprocess API behind a hard outer deadline.

    Python's timeout cleanup kills and then waits for the child without a
    second bound.  A process in uninterruptible I/O can therefore strand the
    daemon loop forever.  The actual subprocess call lives on a capped daemon
    thread; after ``timeout + 2`` the loop moves on even if kernel cleanup does
    not.  Permanently wedged calls consume one of 24 slots but cannot create an
    unbounded thread pile.
    """
    if not _subprocess_slots.acquire(blocking=False):
        raise _CommandCapacityExhausted(
            f'subprocess capacity exhausted before running {argv!r}')
    done = threading.Event()
    result = {}

    def run():
        try:
            result['value'] = invoke(argv, timeout=timeout, **kwargs)
        except BaseException as error:
            result['error'] = error
        finally:
            _subprocess_slots.release()
            done.set()

    try:
        threading.Thread(target=run, daemon=True,
                         name=f'command-{os.path.basename(str(argv[0]))}').start()
    except BaseException:
        _subprocess_slots.release()
        raise
    if not done.wait(timeout=max(0.1, timeout) + _SUBPROCESS_CLEANUP_GRACE):
        raise subprocess.TimeoutExpired(argv, timeout)
    if 'error' in result:
        raise result['error']
    return result['value']


def _hard_check_output(argv, *, timeout: float, **kwargs):
    """``subprocess.check_output`` with a hard parent-thread deadline."""
    return _hard_subprocess_call(
        subprocess.check_output, argv, timeout=timeout, **kwargs)


def _hard_run(argv, *, timeout: float, **kwargs):
    """``subprocess.run`` with a hard parent-thread deadline."""
    return _hard_subprocess_call(
        subprocess.run, argv, timeout=timeout, **kwargs)


def _parse_session_payload(out: str) -> tuple[list, str, str, str]:
    """Parse the marker-framed session/load/tmux response from one node.

    NUL-framed markers cannot occur in tmux session names or shell startup
    chatter. This prevents a session literally named ``---NODEINFO---`` (or a
    noisy remote profile) from corrupting every field and replacing the last
    good session list with phantom rows.
    """
    _preamble, found, payload = out.partition(_SESSION_SECTION)
    if not found:
        raise ValueError('missing session payload marker')
    sessions_text, found, info_text = payload.partition(_NODEINFO_SECTION)
    if not found:
        raise ValueError('missing node-info payload marker')
    node_text, found, tmux_text = info_text.partition(_TMUXINFO_SECTION)
    if not found:
        raise ValueError('missing tmux-info payload marker')
    info_lines = [line.strip() for line in node_text.splitlines() if line.strip()]
    nproc = info_lines[0] if info_lines else ''
    load = info_lines[1].split(',')[0].strip() if len(info_lines) >= 2 else ''
    # The remote clock, sampled in the same command as the activity stamps.
    # Comparing against our own clock instead would report nonsense whenever
    # the two hosts disagree.
    try:
        remote_now = int(info_lines[2]) if len(info_lines) >= 3 else None
    except ValueError:
        remote_now = None
    sessions = []
    for line in sessions_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # activity:windows:name — name is last so it may contain ':'.
        parts = line.split(':', 2)
        if len(parts) == 3:
            activity, wins, name = parts
        else:
            activity, wins, name = '', '?', line
        idle = None
        if remote_now is not None:
            try:
                idle = max(0, remote_now - int(activity))
            except ValueError:
                idle = None
        entry = [name, wins or '?']
        if idle is not None:
            entry.append(idle)
        sessions.append(entry)
    tmux_lines = [line.strip() for line in tmux_text.splitlines() if line.strip()]
    escape_time = tmux_lines[0] if tmux_lines else ''
    if not escape_time.isdigit():
        escape_time = ''
    return sessions, nproc, load, escape_time


def _session_probe_script() -> str:
    """One shell round trip for sessions, load and tmux latency metadata."""
    return (
        # A 500 ms tmux escape window is indistinguishable from lost shortcuts
        # over a weak link, and two nested tmux servers stack that delay.  Tune
        # the live server before publishing its metadata.  This is idempotent,
        # never raises a user's already-lower value, and does not edit tmux.conf.
        "_atmux_escape=$(tmux show-options -s -v escape-time 2>/dev/null || true);"
        " case $_atmux_escape in ''|*[!0-9]*) ;; *)"
        " if [ $_atmux_escape -gt 10 ]; then"
        " tmux set-option -s escape-time 10 >/dev/null 2>&1 || true;"
        " fi ;; esac;"
        "printf '\\000AUTOTMUX_SESSIONS\\000';"
        # Activity and window count lead so a session name may contain ':'.
        # #{session_activity} is the epoch of the session's last activity, read
        # on the same host as the clock below so the two always agree.
        "tmux list-sessions"
        " -F '#{session_activity}:#{session_windows}:#{session_name}'"
        " 2>/dev/null;"
        " printf '\\000AUTOTMUX_NODEINFO\\000\\n';"
        # Keep the CPU count on its own line even on failure so a load value
        # cannot slide into the CPU slot.
        #
        # --all, not plain nproc: nproc reports the CPUs this process may run
        # on, and an SSH session adopted into a job's cgroup sees as few as 1.
        # The load average on the next line is node-wide, so pairing it with an
        # affinity-limited count made an idle 96-core node read as "4.2/1",
        # four times oversubscribed. Both numbers now describe the machine.
        " (nproc --all 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null"
        " || echo '?');"
        " LC_ALL=C uptime | sed -n 's/.*load average: //p';"
        " (date +%s 2>/dev/null || echo '?');"
        " printf '\\000AUTOTMUX_TMUXINFO\\000\\n';"
        " (tmux show-options -s -v escape-time 2>/dev/null || echo '?');"
        " exit 0"
    )


def _session_loop():
    """Periodically fetch tmux sessions for all alive masters."""
    while not _stop_event.is_set():
        try:
            with _lock:
                tasks = []
                for node, node_info in _known_nodes_info.items():
                    generation = _session_generation.get(node, 0) + 1
                    _session_generation[node] = generation
                    tasks.append((node, node_info.get('job_id'), generation))

            def current_info(node, job_id, generation):
                """Return the still-current info dict; caller holds _lock."""
                info = _known_nodes_info.get(node)
                if (info is None
                        or _session_generation.get(node) != generation
                        or info.get('job_id') != job_id):
                    return None
                return info

            def fetch_node(node, job_id, generation):
                lease = _network_coordinator.acquire(node, 'session')
                if lease is None:
                    return
                try:
                    if node != 'localhost' and not _master_alive(node):
                        lease.failure('ControlMaster unavailable')
                        with _lock:
                            info = current_info(node, job_id, generation)
                            if info is not None:
                                _set_info_error_locked(
                                    info, 'session',
                                    'tmux list unavailable: SSH master is down')
                        return
                    # Combine list-sessions + nproc + load into one ssh round
                    # trip. `; exit 0` keeps the exit code happy when the node
                    # has no tmux sessions yet.
                    remote_script = _session_probe_script()
                    if node == 'localhost':
                        cmd = ['sh', '-c', remote_script]
                    else:
                        cmd = ['ssh', '-o', f'ControlPath={_ctl_path(node)}',
                               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                               '-o', f'ConnectTimeout={CONNECT_TIMEOUT}',
                               node, remote_script]
                    out = _hard_check_output(
                        cmd, universal_newlines=True, timeout=10,
                        stderr=subprocess.PIPE,
                    )
                    sessions, nproc, load, escape_time = (
                        _parse_session_payload(out))
                    lease.success()
                    snapshot = None
                    with _lock:
                        info = current_info(node, job_id, generation)
                        if info is not None:
                            info['sessions'] = sessions
                            if nproc:
                                info['nproc'] = nproc
                            if load:
                                info['load'] = load
                            if escape_time:
                                info['escape_time'] = escape_time
                            _set_info_error_locked(info, 'session', None)
                            snapshot = dict(info)
                    if snapshot is not None:
                        # Outside the lock: delivery must never hold up the
                        # poll loop that draws every dashboard.
                        _notify_idle_sessions(node, snapshot)
                except subprocess.CalledProcessError as error:
                    # The remote script ends with `exit 0`, so a non-zero status
                    # means ssh/shell failed, not merely "no tmux sessions".
                    # Keep the last-good listing so one overloaded poll does not
                    # make every session disappear from the UI.
                    detail = ' '.join(str(error.stderr or '').split())[:160]
                    lease.failure(detail or 'tmux list command failed')
                    with _lock:
                        info = current_info(node, job_id, generation)
                        if info is not None:
                            _set_info_error_locked(
                                info, 'session', 'tmux list command failed')
                except subprocess.TimeoutExpired:
                    lease.failure('tmux list timed out')
                    with _lock:
                        info = current_info(node, job_id, generation)
                        if info is not None:
                            _set_info_error_locked(
                                info, 'session', 'tmux list timed out')
                except Exception as e:
                    if isinstance(e, _CommandCapacityExhausted):
                        lease.neutral()
                    else:
                        lease.failure(f'tmux list error: {e}')
                    with _lock:
                        info = current_info(node, job_id, generation)
                        if info is not None:
                            _set_info_error_locked(
                                info, 'session', f'list error: {e}')
                finally:
                    lease.neutral()

            threads = _start_bounded_batch(
                fetch_node, tasks, _session_semaphore,
                'session', lambda a: f'session-{a[0]}')
            _join_threads_until(threads, SHALLOW_CHECK_TIMEOUT + 12)

            _write_status()
        except Exception:
            log.exception('session_loop iteration failed')
        if _stop_event.wait(timeout=SESSION_INTERVAL):
            return


def _stop_legacy_daemon() -> None:
    """Stop a pre-XDG daemon still running under the old /tmp pid file.

    Without this, after upgrading to XDG paths the new frontend wouldn't see
    the old daemon (different pid-file location) and would start a second one,
    leaving two daemons fighting over the same ControlMaster sockets.
    """
    if LEGACY_PID_FILE == PID_FILE:
        return  # paths resolved to /tmp anyway; nothing to migrate
    pid = _read_int_file(LEGACY_PID_FILE)
    if pid is None:
        return
    if pid == os.getpid() or not _pid_running(pid):
        return
    if not _is_our_daemon(pid):
        log.warning(f'migrated: legacy pid file points to non-autotmux pid={pid}; '
                    'refusing to signal it')
        try:
            os.unlink(LEGACY_PID_FILE)
        except OSError:
            pass
        return
    log.info(f'migrated: stopping legacy daemon (pid={pid})')
    token = lifecycle.process_token(pid)
    if not lifecycle.signal_same_process(pid, token, signal.SIGTERM):
        return
    deadline = time.monotonic() + 5
    while lifecycle.same_process(pid, token) and time.monotonic() < deadline:
        time.sleep(0.1)
    if lifecycle.same_process(pid, token):
        lifecycle.signal_same_process(pid, token, signal.SIGKILL)


def cmd_start():
    pid = _resolve_daemon_pid()
    if _lock_held() or pid is not None:
        detail = f'pid={pid}' if pid is not None else 'singleton lock held'
        print(f'[autotmux_daemon] Already running ({detail})')
        return True
    # Authoritative guard against a concurrent `start`/`restart` racing us:
    # grab the singleton lock BEFORE forking so the daemon inherits it.
    try:
        acquired = _acquire_singleton_lock()
    except OSError as error:
        print(f'[autotmux_daemon] Could not acquire daemon lock: {error}',
              file=sys.stderr)
        return False
    if not acquired:
        print('[autotmux_daemon] Already running (another instance holds the lock)')
        return True
    # Recheck a lock-less legacy daemon after winning the lock; and discard a
    # stale/reused advisory PID only while start is mutually exclusive.
    old_pid = _read_pid()
    if old_pid and _pid_running(old_pid) and _is_our_daemon(old_pid):
        _release_singleton_lock()
        print(f'[autotmux_daemon] Already running (legacy pid={old_pid})')
        return True
    if old_pid is not None:
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass
    _stop_event.clear()
    print(f'[autotmux_daemon] Starting daemon... (log: {LOG_FILE})')
    try:
        startup_fd = _daemonize()
    except OSError as error:
        _release_singleton_lock()
        print(f'[autotmux_daemon] Could not detach daemon: {error}',
              file=sys.stderr)
        return False
    startup_reported = False
    try:
        # Install signal handlers FIRST so we don't have a window where a
        # stray SIGTERM kills the daemon without graceful shutdown.
        _install_signal_handlers()
        _configure_logging()
        _sweep_stale_tmp()
        _load_runtime_configuration()
        _restore_last_known_state()
        _stop_legacy_daemon()
        _write_pid(ready=False)
        log.info(f'Daemon started (pid={os.getpid()}, version={__version__})')
        # Cleanup runs in a background thread — it probes orphan sockets per
        # orphan socket which can take many seconds. If we did it on the main
        # thread, SIGTERM wouldn't be processed until cleanup finished.
        threading.Thread(target=_cleanup_orphan_sockets,
                         daemon=True, name='cleanup').start()
        threading.Thread(target=_squeue_loop, daemon=True).start()
        threading.Thread(target=_health_loop, daemon=True).start()
        threading.Thread(target=_session_loop, daemon=True).start()
        threading.Thread(target=_snapshot_loop, daemon=True).start()
        threading.Thread(target=_preview_server_loop, daemon=True,
                         name='preview-server').start()
        threading.Thread(target=_warm_orphan_loop, daemon=True,
                         name='warm-orphan-sweeper').start()
        _write_lock_metadata(os.getpid(), ready=True)
        _notify_startup(startup_fd, True)
        startup_fd = None
        startup_reported = True
        _serve_until_stopped()
        log.info('Daemon shutting down')
        return True
    except Exception as error:
        if not startup_reported:
            _notify_startup(startup_fd, False,
                            f'{type(error).__name__}: {error}')
            startup_fd = None
        try:
            log.exception('daemon startup/runtime failure')
        except Exception:
            pass
        return False
    finally:
        if startup_fd is not None:
            _notify_startup(startup_fd, False, 'daemon startup aborted')
        # Startup can fail after the PID is published (thread creation,
        # configuration, etc.). Never leave a stale PID behind in that case.
        if _read_pid() == os.getpid():
            try:
                os.unlink(PID_FILE)
            except OSError:
                pass
        _release_singleton_lock()


def cmd_stop():
    lock_held = _lock_held()
    pid = _resolve_daemon_pid()
    if pid is None:
        # The daemon may have exited between the first lock probe and PID
        # resolution. Recheck before reporting an unresolvable held lock; a
        # stale True here made `restart` abort even though nothing was running.
        lock_held = _lock_held()
        if lock_held:
            print('[autotmux_daemon] A daemon lock is held, but its PID could not '
                  'be verified; refusing an unsafe kill.')
            return False
        print('[autotmux_daemon] Not running.')
        stale = _read_pid()
        if stale is not None:
            _remove_pid_if_matches(stale)
        return True
    token = lifecycle.process_token(pid)
    if not lifecycle.signal_same_process(pid, token, signal.SIGTERM):
        print(f'[autotmux_daemon] Could not safely signal pid={pid}.')
        return False
    deadline = time.monotonic() + 10
    while lifecycle.same_process(pid, token) and time.monotonic() < deadline:
        time.sleep(0.1)
    if lifecycle.same_process(pid, token):
        lifecycle.signal_same_process(pid, token, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while lifecycle.same_process(pid, token) and time.monotonic() < deadline:
            time.sleep(0.05)
    if lifecycle.same_process(pid, token):
        print(f'[autotmux_daemon] Failed to stop pid={pid}; it may be stuck in '
              'uninterruptible I/O.')
        return False
    _remove_pid_if_matches(pid)
    print(f'[autotmux_daemon] Stopped (was pid={pid})')
    return True


def _state_age_seconds(data: dict) -> float | None:
    """Age of a published daemon state, preferring the host monotonic clock."""
    monotonic = data.get('updated_monotonic') if isinstance(data, dict) else None
    clock_id = data.get('monotonic_clock_id') if isinstance(data, dict) else None
    if ((clock_id is None or clock_id == lifecycle.monotonic_clock_id())
            and isinstance(monotonic, (int, float))
            and not isinstance(monotonic, bool)):
        age = time.monotonic() - float(monotonic)
        if age >= 0 and math.isfinite(age):
            return age
    updated = data.get('updated') if isinstance(data, dict) else None
    if isinstance(updated, str):
        try:
            age = time.time() - time.mktime(
                time.strptime(updated, '%Y-%m-%d %H:%M:%S'))
            return age if age >= 0 else None
        except (OverflowError, ValueError):
            pass
    return None


def cmd_status(as_json: bool = False):
    pid = _resolve_daemon_pid()
    # The flock is authoritative; the pid file is advisory and may be missing
    # under a live daemon (see _lock_held).
    running = _lock_held() or pid is not None
    active_base = lifecycle.active_runtime_base(GUARD_FILE)
    view_base = active_base or paths.BASE
    view_state = os.path.join(view_base, 'daemon.json')
    view_log = os.path.join(view_base, 'daemon.log')
    view_ctl = os.path.join(view_base, 'ctl')

    if as_json:
        # Re-emit the daemon's state file, plus our own running/pid state.
        try:
            data = _read_json_dict(view_state, _STATE_FILE_LIMIT)
        except Exception:
            data = {}
        age = _state_age_seconds(data)
        out = {
            'running': running,
            'pid': pid,
            'version': __version__,
            'runtime_base': view_base,
            'log_file': view_log,
            'ctl_dir': view_ctl,
            'state_age_seconds': age,
            # Missing/malformed timestamps are unknown, never "fresh".  This
            # keeps automation from trusting a corrupt last-known state merely
            # because its age could not be computed.
            'state_stale': not data or age is None or age > 60,
            'state': data,
        }
        print(json.dumps(out, indent=2))
        return running

    if not running:
        print('[autotmux_daemon] ✗ Not running')
    elif pid is None:
        print('[autotmux_daemon] ✓ Running (PID unavailable, '
              f'version {__version__})')
    else:
        print(f'[autotmux_daemon] ✓ Running (pid={pid}, version {__version__})')

    try:
        data = _read_json_dict(view_state, _STATE_FILE_LIMIT)
        age = _state_age_seconds(data)
        age_note = ''
        if age is not None and age > 60:
            age_note = f'  ⚠ stale ({age:.0f}s old)'
        elif not running:
            age_note = '  (last-known state)'
        elif age is None:
            age_note = '  ⚠ timestamp unavailable'
        print(f"  Last updated : {data.get('updated', '?')}{age_note}")
        nodes = data.get('nodes', {})
        if not nodes:
            print('  Nodes        : (none discovered yet)')
        else:
            heading = ('Last-known nodes'
                       if not running or (age is not None and age > 60)
                       else 'Nodes')
            print(f'  {heading} ({len(nodes)}):')
            for node, info in sorted(nodes.items()):
                mark = '✓' if info.get('alive') else '✗'
                socket = info.get('socket') or ''
                detail = f'[{socket}]' if socket else '(local)'
                err = info.get('last_error') or ''
                err_part = f'  ! {err}' if err else ''
                print(f'    {mark} {node}  {detail}{err_part}')
    except Exception:
        print('  (no status file yet - daemon may still be starting)')

    print(f'\n  Log  : {view_log}')
    print(f'  CTL  : {view_ctl}/')
    return running


def _tail_owned_text(path: str, lines: int = 50,
                     max_bytes: int = _LOG_TAIL_LIMIT) -> str:
    """Return a bounded tail from an owned regular UTF-8-ish text file."""
    fd = lifecycle.open_owned_regular_file(path)
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - max_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        remaining = max_bytes
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b''.join(chunks)
    rendered = raw.decode('utf-8', errors='replace').splitlines(keepends=True)
    text = ''.join(rendered[-max(1, lines):])
    if start > 0 and len(rendered) <= lines:
        text = '[... earlier log content omitted ...]\n' + text
    return text


def cmd_logs(follow: bool = False):
    """Tail the daemon log. With --follow, runs `tail -f` so the user
    can watch live."""
    active_base = lifecycle.active_runtime_base(GUARD_FILE)
    log_file = (os.path.join(active_base, 'daemon.log')
                if active_base else LOG_FILE)
    if follow:
        try:
            fd = lifecycle.open_owned_regular_file(log_file)
            os.close(fd)
        except FileNotFoundError:
            print(f'(no log yet at {log_file})')
            return True
        except OSError as error:
            print(f'[autotmux_daemon] Could not read log: {error}',
                  file=sys.stderr)
            return False
        try:
            os.execvp('tail', ['tail', '-n', '50', '-F', '--', log_file])
        except OSError:
            pass  # tail not available or other exec error — fall through
        # Manual tail -f if tail isn't available
        try:
            fd = lifecycle.open_owned_regular_file(log_file)
            with os.fdopen(fd, 'r', errors='replace') as f:
                f.seek(0, 2)  # end
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.5)
        except KeyboardInterrupt:
            return True
        except OSError as error:
            print(f'[autotmux_daemon] Could not read log: {error}',
                  file=sys.stderr)
            return False
    else:
        try:
            sys.stdout.write(_tail_owned_text(log_file))
        except FileNotFoundError:
            # The daemon can rotate/recreate its runtime directory between the
            # existence check and open; this is an ordinary empty-log state.
            print(f'(no log yet at {log_file})')
            return True
        except OSError as error:
            print(f'[autotmux_daemon] Could not read log: {error}',
                  file=sys.stderr)
            return False
    return True


def cmd_restart():
    if not cmd_stop():
        print('[autotmux_daemon] Restart aborted because the old daemon did not stop.')
        return False
    # cmd_stop already waits until the exact old process is gone and both
    # singleton flocks are released, so an extra fixed delay only makes every
    # successful restart feel sluggish.
    return cmd_start()


# ── entry point ───────────────────────────────────────────────────────────────

USAGE = """Usage: atmux-daemon <command> [options]  (legacy alias: atd)
Commands:
  start            Start daemon in background
  stop             Stop daemon
  restart          Restart daemon (preserves healthy ssh masters)
  status [--json]  Show node status and master health
  logs [-f]        Show the daemon log (tail). -f to follow.
  run              Run in foreground (for debugging / testing)
  --version        Print version
"""


def main_entry():
    """Entry point for the `atd` console script."""
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)
    cmd = sys.argv[1].lower()
    extra = sys.argv[2:]
    allowed_extra = {
        'status': {'--json'},
        'logs': {'-f', '--follow'},
    }.get(cmd, set())
    if (any(item not in allowed_extra for item in extra)
            or len(extra) > (1 if allowed_extra else 0)):
        detail = ' '.join(extra) if extra else '(none)'
        print(f'atd: unsupported option(s) for {cmd}: {detail}',
              file=sys.stderr)
        sys.exit(2)
    ok = True
    if cmd == 'start':
        ok = cmd_start()
    elif cmd == 'stop':
        ok = cmd_stop()
    elif cmd == 'restart':
        ok = cmd_restart()
    elif cmd == 'status':
        ok = cmd_status(as_json=('--json' in extra))
    elif cmd == 'logs':
        ok = cmd_logs(follow=any(f in extra for f in ('-f', '--follow')))
    elif cmd == 'run':
        ok = run_foreground()
    elif cmd in ('--version', '-v', 'version'):
        print(f'autotmux-daemon {__version__}')
    else:
        print(f'Unknown command: {cmd}\n')
        print(USAGE)
        sys.exit(1)
    if ok is False:
        sys.exit(1)


if __name__ == '__main__':
    main_entry()
