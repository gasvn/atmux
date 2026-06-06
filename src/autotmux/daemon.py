#!/usr/bin/env python3
"""
autotmux_daemon.py - SSH ControlMaster keep-alive daemon
=========================================================
Runs in the background and maintains persistent SSH ControlMaster connections
for all nodes returned by `squeue`. This allows autotmux (and plain ssh) to
attach to remote tmux sessions instantly, without re-authenticating.

Sockets are stored in /tmp/autotmux_ctl_<uid>/ (local fs, never NFS).

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
import logging
from logging.handlers import RotatingFileHandler

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

# ── logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger('autotmux_daemon')
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    _handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    log.addHandler(_handler)

_cfg = config.load()

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
HOST_EXPR_RE      = re.compile(r'^[A-Za-z0-9._\-\[\],]+$')

os.makedirs(CTL_DIR, mode=0o700, exist_ok=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data) -> None:
    """Write JSON atomically — write to a tmp file, then os.replace.

    Without this, a frontend polling the state file can read a half-written
    document and bail with a JSON error.
    """
    tmp = f'{path}.tmp.{os.getpid()}'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── ControlMaster helpers ────────────────────────────────────────────────────

def _ctl_path(node: str) -> str:
    safe = node.replace('/', '_').replace(':', '_')
    return os.path.join(CTL_DIR, f'cm_{safe}')


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
        r = subprocess.run(
            ['ssh', '-o', f'ControlPath={ctl}', '-O', 'check', node],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=SHALLOW_CHECK_TIMEOUT,
        )
        if r.returncode != 0:
            return False
    except Exception:
        return False
    if not deep:
        return True
    try:
        r = subprocess.run(
            ['ssh', '-o', f'ControlPath={ctl}', '-o', 'BatchMode=yes', node, 'true'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=DEEP_PROBE_TIMEOUT,
        )
        return r.returncode == 0
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
        out = subprocess.check_output(
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
    if os.path.exists(ctl):
        try:
            subprocess.run(
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
            try:
                os.kill(master_pid, 0)
            except OSError:
                return  # gone
            time.sleep(0.05)
        try:
            os.kill(master_pid, signal.SIGKILL)
        except OSError:
            pass


def _kill_ssh_with_ctl_path(node: str) -> int:
    """SIGTERM any ssh process whose argv mentions our ControlPath for this
    node. Returns the count of killed PIDs. Used at startup to clean up
    orphan masters left by a previous daemon that crashed without reaping
    its children — they wouldn't be in _master_procs."""
    ctl = _ctl_path(node)
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', f'ControlPath={ctl}'],
            universal_newlines=True, timeout=3,
        )
    except subprocess.CalledProcessError:
        return 0
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    return killed


def _cleanup_orphan_sockets() -> None:
    """At startup, decide what to do with each leftover ControlMaster socket.

    Healthy masters are *adopted* (left alone) so a user attached to a tmux
    session via the previous daemon doesn't get yanked when they `atd
    restart`. Hung / dead masters are killed so we can rebuild them cleanly.
    """
    if not os.path.isdir(CTL_DIR):
        return
    for entry in os.listdir(CTL_DIR):
        if not entry.startswith('cm_'):
            continue
        node = entry[3:]
        # Deep probe: if the master answers a real ssh command, it's
        # genuinely usable — keep it.
        if _master_alive(node, deep=True):
            log.info(f'Adopting healthy orphan master for {node}')
            continue
        _kill_master(node)
        killed = _kill_ssh_with_ctl_path(node)
        if killed:
            log.info(f'Cleaned dead orphan master for {node} (+{killed} stray ssh procs)')
        else:
            log.info(f'Cleaned dead orphan master for {node}')


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
            prev.wait(timeout=2)
    except Exception:
        pass


def _start_master(node: str) -> bool:
    """Start a ControlMaster for node. Serialized per-node so concurrent
    calls (e.g., _squeue_loop and _health_loop racing on the same dead
    master) don't both fork a new ssh and leak one. Returns True on
    success."""
    with _node_master_lock(node):
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
            time.sleep(0.1)
            if os.path.exists(ctl):
                _record_error(node, None)
                return True
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


def _record_error(node: str, err: str | None) -> None:
    """Set or clear the last_error field for a node."""
    with _lock:
        info = _known_nodes_info.get(node)
        if info is None:
            return
        if err is None:
            info.pop('last_error', None)
        else:
            info['last_error'] = err[:200]


def _backoff_should_skip(node: str) -> bool:
    bk = _master_backoff.get(node)
    return bool(bk and time.time() < bk['next_try'])


def _backoff_record_failure(node: str) -> float:
    bk = _master_backoff.get(node, {'next_try': 0.0, 'fails': 0})
    bk['fails'] += 1
    delay = min(BACKOFF_BASE * (2 ** (bk['fails'] - 1)), BACKOFF_CAP)
    bk['next_try'] = time.time() + delay
    _master_backoff[node] = bk
    return delay


def _backoff_clear(node: str) -> None:
    _master_backoff.pop(node, None)


def _cleanup_gone_node(node: str) -> None:
    """Drop all per-node bookkeeping when a node leaves squeue.

    Without this, master Popen handles, backoff entries, the
    ControlMaster socket, and the deep-probe failure streak all
    persist for hours after the slurm job ended.
    """
    if node == 'localhost':
        return
    _backoff_clear(node)
    _master_failure_streak.pop(node, None)
    _kill_master(node)  # also wipes _master_procs entry via _kill_orphan_master_proc


# ── node discovery ────────────────────────────────────────────────────────────

def _get_nodes() -> dict:
    """Return dict of nodes with their job info from squeue for current user.

    Always includes a synthetic 'localhost' entry so local tmux sessions show
    up alongside the slurm nodes.
    """
    nodes = {
        'localhost': {
            'time': '-',
            'job_name': 'local',
            'job_id': '-',
            'state': 'LOCAL',
        }
    }
    try:
        out = subprocess.check_output(
            ['squeue', '-u', _USER, '-h', '-o', '%N|%L|%j|%i|%P|%u|%T|%M|%D|%R'],
            universal_newlines=True, timeout=SQUEUE_TIMEOUT,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('|', 9)
            if len(parts) < 10:
                continue
            node_part = parts[0]
            if not node_part or node_part.startswith('(') or node_part in {'n/a', 'N/A', 'None assigned'}:
                continue
                
            info = {
                'time': parts[1],
                'job_name': parts[2],
                'job_id': parts[3],
                'state': parts[6]
            }

            if '[' in node_part or ',' in node_part:
                try:
                    expanded = subprocess.check_output(
                        ['scontrol', 'show', 'hostnames', node_part],
                        universal_newlines=True, timeout=5,
                    )
                    for n in expanded.splitlines():
                        if n.strip():
                            nodes[n.strip()] = info
                except Exception:
                    nodes[node_part] = info
            else:
                nodes[node_part] = info
    except Exception as e:
        log.warning(f'squeue error: {e}')
    return nodes


# ── daemon main loop ──────────────────────────────────────────────────────────

_known_nodes_info: dict = {}
_master_backoff: dict = {}    # node -> {'next_try': epoch, 'fails': N}
_master_procs: dict = {}      # node -> Popen object for the spawned master
_master_failure_streak: dict = {}  # node -> consecutive deep-probe failures
_node_master_locks: dict = {}  # node -> threading.Lock for master spawn/kill
_gone_node_streak: dict = {}   # node -> consecutive squeue misses
_squeue_text: dict = {'long': '', 'pending': '', 'updated': ''}
_lock = threading.Lock()
_stop_event = threading.Event()


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


def _node_master_lock(node: str) -> threading.Lock:
    """Return (lazily creating) a per-node lock that serializes master
    spawn/kill operations. Prevents a race where _squeue_loop and
    _health_loop both decide the master is dead and start two masters
    concurrently — leaving an orphan ssh process untracked."""
    lock = _node_master_locks.get(node)
    if lock is None:
        with _lock:
            lock = _node_master_locks.get(node)
            if lock is None:
                lock = threading.Lock()
                _node_master_locks[node] = lock
    return lock

BACKOFF_BASE  = _cfg['backoff_base']  # initial retry delay after a master start fails
BACKOFF_CAP   = _cfg['backoff_cap']   # max retry delay
HEALTH_FAIL_THRESHOLD = 5    # consecutive shallow-check failures before kill+restart


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
        _backoff_clear(node)
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


def _get_squeue_text(args: list[str]) -> str:
    """Run a `squeue ...` command and return its raw text output (or an error
    line). Used for the bottom-panel jobs view."""
    try:
        return subprocess.check_output(
            ['squeue', '-u', _USER, *args],
            universal_newlines=True, timeout=SQUEUE_TIMEOUT,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f'(squeue {" ".join(args)} timed out)'
    except Exception as e:
        return f'(squeue error: {e})'


def _squeue_loop():
    """Periodically discover nodes, spin up masters, refresh job listings."""
    last_set: frozenset = frozenset()
    while not _stop_event.is_set():
        try:
            node_infos = _get_nodes()
            with _lock:
                # Preserve fields that come from _session_loop (sessions,
                # nproc, load, last_error) — squeue doesn't know about them
                # and we don't want to wipe them every 30 s.
                for node, info in node_infos.items():
                    old = _known_nodes_info.get(node, {})
                    for key in ('sessions', 'nproc', 'load', 'last_error'):
                        if key in old:
                            info[key] = old[key]
                # Reset the gone-streak for every node we just saw.
                for n in node_infos:
                    _mark_node_seen(n)
                # Identify nodes that are missing this cycle. We DON'T
                # remove them yet — wait for GONE_NODE_THRESHOLD misses
                # so a single squeue blip can't kill every master.
                missing = set(_known_nodes_info) - set(node_infos)
                gone = [n for n in missing if _mark_node_missing(n)]
                for g in gone:
                    _known_nodes_info.pop(g, None)
                _known_nodes_info.update(node_infos)
            # _cleanup_gone_node uses subprocess + _lock internally; do
            # it OUTSIDE the lock to avoid reentrancy.
            for g in gone:
                _cleanup_gone_node(g)
                _gone_node_streak.pop(g, None)
                log.info(f'Node {g} left squeue (>{GONE_NODE_THRESHOLD} misses) — cleaned')
            current_set = frozenset(node_infos.keys())
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
            threads = [_bounded_daemon_thread(_ensure_master, (n,),
                                              _session_semaphore,
                                              f'ensure-{n}')
                       for n in node_infos.keys()]
            for t in threads:
                t.join(timeout=CONNECT_TIMEOUT + 5)
            # Refresh `squeue -l` and `--start` text for the jobs panel.
            long_text = _get_squeue_text(['-l'])
            pending_text = _get_squeue_text(['-l', '--start'])
            with _lock:
                _squeue_text['long'] = long_text
                _squeue_text['pending'] = pending_text
                _squeue_text['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
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
        _master_failure_streak.pop(node, None)
        _backoff_clear(node)
        return 'alive'
    streak = _master_failure_streak.get(node, 0) + 1
    _master_failure_streak[node] = streak
    if streak < HEALTH_FAIL_THRESHOLD:
        log.info(f'Master for {node} not responding ({streak}/{HEALTH_FAIL_THRESHOLD})')
        return 'transient-failure'
    log.info(f'Master for {node} unresponsive after {streak} checks, restarting...')
    _kill_master(node)
    _master_failure_streak.pop(node, None)
    if _start_master(node):
        log.info(f'Master for {node} restarted')
        _backoff_clear(node)
        return 'restarted'
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
            for node in nodes:
                if _stop_event.is_set():
                    return
                _health_check_node(node)
            _write_status()
        except Exception:
            log.exception('health_loop iteration failed')


def _capture_pane(node: str, session: str) -> str | None:
    """Run `tmux capture-pane -p -e -t <session>` for one (node, session)."""
    try:
        if node == 'localhost':
            cmd = ['tmux', 'capture-pane', '-p', '-e', '-t', session]
        else:
            if not _master_alive(node):
                return None
            # ssh ships remaining args as a single string to the remote shell;
            # quote the session in case it contains spaces / metacharacters.
            cmd = ['ssh', '-o', f'ControlPath={_ctl_path(node)}',
                   '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                   node, f'tmux capture-pane -p -e -t {shlex.quote(session)}']
        out = subprocess.check_output(
            cmd, universal_newlines=True, timeout=8,
            stderr=subprocess.DEVNULL,
        )
        return out
    except Exception:
        return None


_snapshot_semaphore = threading.Semaphore(6)


def _snapshot_loop():
    """Periodically capture a tmux pane for every (node, session) we know
    about and persist them so the frontend has something to show instantly
    on launch and when switching rows. Concurrency capped at 6 so we don't
    burst-load overloaded compute nodes."""
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

            def _grab(node, session):
                text = _capture_pane(node, session)
                if text is not None:
                    with results_lock:
                        results[f'{node}:{session}'] = {
                            'lines': text,
                            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                        }

            threads = [_bounded_daemon_thread(_grab, (n, s), _snapshot_semaphore,
                                              f'snapshot-{n}-{s}')
                       for (n, s) in pairs]
            for t in threads:
                t.join(timeout=12)

            if results:
                # Merge with any existing snapshots so a transient failure for
                # one session doesn't drop the previously-good snapshot.
                merged: dict = {}
                try:
                    with open(SNAPSHOT_FILE) as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        # Drop legacy entries (v0.3.x stored values as lists);
                        # they'll be repopulated on the next capture.
                        merged = {k: v for k, v in loaded.items() if isinstance(v, dict)}
                except Exception:
                    merged = {}
                merged.update(results)
                # Drop snapshots for sessions we no longer track
                live_keys = {f'{n}:{s}' for (n, s) in pairs}
                merged = {k: v for k, v in merged.items() if k in live_keys}
                _atomic_write_json(SNAPSHOT_FILE, merged)
        except Exception:
            log.exception('snapshot_loop iteration failed')
        if _stop_event.wait(timeout=SNAPSHOT_INTERVAL):
            return


def _write_status():
    """Write a JSON status snapshot for the `status` command and the frontend."""
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
    # _master_alive can take seconds (spawns ssh) — do it OUTSIDE the lock.
    status = {
        'pid': os.getpid(),
        'user': _USER,
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'squeue_long': squeue_long,
        'squeue_pending': squeue_pending,
        'squeue_updated': squeue_updated,
        'nodes': {
            n: {
                'alive': _master_alive(n),
                'socket': _ctl_path(n) if n != 'localhost' else '',
                **snap,
            }
            for n, snap in snapshot.items()
        }
    }
    try:
        _atomic_write_json(STAT_FILE, status)
    except Exception:
        log.exception('failed to write state file')


def run_foreground():
    """Run the daemon loops in the foreground (useful for debugging)."""
    log.info(f'autotmux_daemon starting (pid={os.getpid()}, user={_USER})')
    print(f'[autotmux_daemon] Running in foreground. Logs: {LOG_FILE}')
    _install_signal_handlers()
    _cleanup_orphan_sockets()
    threading.Thread(target=_squeue_loop, daemon=True).start()
    threading.Thread(target=_health_loop, daemon=True).start()
    threading.Thread(target=_session_loop, daemon=True).start()
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    try:
        while not _stop_event.wait(timeout=1):
            pass
    except KeyboardInterrupt:
        _stop_event.set()
    print('\n[autotmux_daemon] Stopped.')


# ── daemon fork / control ─────────────────────────────────────────────────────

def _daemonize():
    """Double-fork to fully detach from terminal."""
    # Flush BEFORE forking — otherwise the buffered output is duplicated into
    # every forked process and printed multiple times when each one exits.
    for fd in (sys.stdout, sys.stderr):
        try:
            fd.flush()
        except Exception:
            pass
    if os.fork() > 0:
        os._exit(0)          # parent exits without flushing again
    os.setsid()
    if os.fork() > 0:
        os._exit(0)          # first child exits without flushing again
    devnull = open(os.devnull, 'r+')
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(devnull.fileno(), sys.stdout.fileno())
    os.dup2(devnull.fileno(), sys.stderr.fileno())


def _write_pid():
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def _read_pid() -> int | None:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_session_semaphore = threading.Semaphore(12)


def _bounded_daemon_thread(target, args, sem: threading.Semaphore, name: str):
    """Spawn a daemon thread that respects a concurrency semaphore. Daemon
    flag matters: it lets the process exit cleanly even if the thread is
    stuck on a slow ssh — atexit won't wait for daemon threads."""
    def wrapper():
        with sem:
            target(*args)
    t = threading.Thread(target=wrapper, daemon=True, name=name)
    t.start()
    return t


def _session_loop():
    """Periodically fetch tmux sessions for all alive masters."""
    while not _stop_event.is_set():
        try:
            with _lock:
                nodes = list(_known_nodes_info.keys())

            def fetch_node(node):
                if node != 'localhost' and not _master_alive(node):
                    return
                # Combine list-sessions + nproc + load into one ssh round
                # trip. `; exit 0` keeps the exit code happy when the node has
                # no tmux sessions yet (tmux list-sessions returns non-zero).
                remote_script = (
                    "tmux list-sessions -F '#{session_name}:#{session_windows}' 2>/dev/null;"
                    " echo '---NODEINFO---';"
                    " nproc 2>/dev/null;"
                    " uptime | sed -n 's/.*load average: //p';"
                    " exit 0"
                )
                try:
                    if node == 'localhost':
                        cmd = ['sh', '-c', remote_script]
                    else:
                        cmd = ['ssh', '-o', f'ControlPath={_ctl_path(node)}',
                               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                               '-o', 'ConnectTimeout=5', node, remote_script]
                    out = subprocess.check_output(
                        cmd, universal_newlines=True, timeout=10,
                        stderr=subprocess.DEVNULL,
                    )
                    sessions_text, _, info_text = out.partition('---NODEINFO---')
                    sessions = []
                    for line in sessions_text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        if ':' in line:
                            name, _, wins = line.partition(':')
                            sessions.append([name, wins or '?'])
                        else:
                            sessions.append([line, '?'])
                    nproc = ''
                    load = ''
                    info_lines = [l.strip() for l in info_text.splitlines() if l.strip()]
                    if len(info_lines) >= 1:
                        nproc = info_lines[0]
                    if len(info_lines) >= 2:
                        # Take the 1-minute load (first comma-separated token).
                        load = info_lines[1].split(',')[0].strip()
                    with _lock:
                        if node in _known_nodes_info:
                            _known_nodes_info[node]['sessions'] = sessions
                            if nproc:
                                _known_nodes_info[node]['nproc'] = nproc
                            if load:
                                _known_nodes_info[node]['load'] = load
                            # Successful list = node is reachable; clear errors
                            if 'last_error' in _known_nodes_info[node]:
                                _known_nodes_info[node].pop('last_error', None)
                except subprocess.CalledProcessError:
                    # tmux exits non-zero when there are no sessions (or no
                    # local server). Treat as "no sessions", not an error.
                    with _lock:
                        if node in _known_nodes_info:
                            _known_nodes_info[node]['sessions'] = []
                except subprocess.TimeoutExpired:
                    with _lock:
                        if node in _known_nodes_info:
                            _known_nodes_info[node]['sessions'] = []
                            _known_nodes_info[node]['last_error'] = 'tmux list timed out'
                except Exception as e:
                    with _lock:
                        if node in _known_nodes_info:
                            _known_nodes_info[node]['sessions'] = []
                            _known_nodes_info[node]['last_error'] = f'list error: {e}'[:200]

            threads = [_bounded_daemon_thread(fetch_node, (n,), _session_semaphore,
                                              f'session-{n}') for n in nodes]
            for t in threads:
                t.join(timeout=12)

            _write_status()
        except Exception:
            log.exception('session_loop iteration failed')
        if _stop_event.wait(timeout=SESSION_INTERVAL):
            return


def cmd_start():
    pid = _read_pid()
    if pid and _pid_running(pid):
        print(f'[autotmux_daemon] Already running (pid={pid})')
        return
    print(f'[autotmux_daemon] Starting daemon... (log: {LOG_FILE})')
    _daemonize()
    # Install signal handlers FIRST so we don't have a window where a
    # stray SIGTERM kills the daemon without graceful shutdown.
    _install_signal_handlers()
    _write_pid()
    log.info(f'Daemon started (pid={os.getpid()}, version={__version__})')
    # Cleanup runs in a background thread — it does deep ssh probes per
    # orphan socket which can take many seconds. If we did it on the main
    # thread, SIGTERM wouldn't be processed until cleanup finished.
    threading.Thread(target=_cleanup_orphan_sockets,
                     daemon=True, name='cleanup').start()
    threading.Thread(target=_squeue_loop, daemon=True).start()
    threading.Thread(target=_health_loop, daemon=True).start()
    threading.Thread(target=_session_loop, daemon=True).start()
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    while not _stop_event.wait(timeout=1):
        pass
    log.info('Daemon shutting down')
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


def cmd_stop():
    pid = _read_pid()
    if not pid or not _pid_running(pid):
        print('[autotmux_daemon] Not running.')
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        if not _pid_running(pid):
            break
    if _pid_running(pid):
        os.kill(pid, signal.SIGKILL)
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
    print(f'[autotmux_daemon] Stopped (was pid={pid})')


def cmd_status(as_json: bool = False):
    pid = _read_pid()
    running = pid is not None and _pid_running(pid)

    if as_json:
        # Re-emit the daemon's state file, plus our own running/pid state.
        try:
            with open(STAT_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        out = {
            'running': running,
            'pid': pid,
            'version': __version__,
            'log_file': LOG_FILE,
            'ctl_dir': CTL_DIR,
            'state': data,
        }
        print(json.dumps(out, indent=2))
        return

    if not running:
        print('[autotmux_daemon] ✗ Not running')
    else:
        print(f'[autotmux_daemon] ✓ Running (pid={pid}, version {__version__})')

    try:
        with open(STAT_FILE) as f:
            data = json.load(f)
        print(f"  Last updated : {data.get('updated', '?')}")
        nodes = data.get('nodes', {})
        if not nodes:
            print('  Nodes        : (none discovered yet)')
        else:
            print(f'  Nodes ({len(nodes)}):')
            for node, info in sorted(nodes.items()):
                mark = '✓' if info.get('alive') else '✗'
                socket = info.get('socket') or ''
                detail = f'[{socket}]' if socket else '(local)'
                err = info.get('last_error') or ''
                err_part = f'  ! {err}' if err else ''
                print(f'    {mark} {node}  {detail}{err_part}')
    except Exception:
        print('  (no status file yet - daemon may still be starting)')

    print(f'\n  Log  : {LOG_FILE}')
    print(f'  CTL  : {CTL_DIR}/')


def cmd_logs(follow: bool = False):
    """Tail the daemon log. With --follow, runs `tail -f` so the user
    can watch live."""
    if not os.path.exists(LOG_FILE):
        print(f'(no log yet at {LOG_FILE})')
        return
    if follow:
        try:
            os.execvp('tail', ['tail', '-n', '50', '-F', LOG_FILE])
        except OSError:
            pass  # tail not available or other exec error — fall through
        # Manual tail -f if tail isn't available
        with open(LOG_FILE) as f:
            f.seek(0, 2)  # end
            try:
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                return
    else:
        with open(LOG_FILE) as f:
            sys.stdout.write(f.read())


def cmd_restart():
    cmd_stop()
    time.sleep(1)
    cmd_start()


# ── entry point ───────────────────────────────────────────────────────────────

USAGE = """Usage: atd <command> [options]
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
    if   cmd == 'start':   cmd_start()
    elif cmd == 'stop':    cmd_stop()
    elif cmd == 'restart': cmd_restart()
    elif cmd == 'status':
        cmd_status(as_json=('--json' in extra))
    elif cmd == 'logs':
        cmd_logs(follow=any(f in extra for f in ('-f', '--follow')))
    elif cmd == 'run':     run_foreground()
    elif cmd in ('--version', '-V', 'version'):
        print(f'autotmux-daemon {__version__}')
    else:
        print(f'Unknown command: {cmd}\n')
        print(USAGE)
        sys.exit(1)


if __name__ == '__main__':
    main_entry()
