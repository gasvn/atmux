#!/usr/bin/env python3
"""AutoTmux – Textual frontend.

The dashboard reads daemon-published state and asks the daemon for previews;
it never runs squeue or background SSH itself.  Interactive SSH/tmux commands
run only after the user explicitly presses Enter, s, or o.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import enum
import errno
import fcntl
from functools import partial
import functools
import hashlib
import json
import math
import os
import pty
import queue
import re
import select
import shutil
import shlex
import signal
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import uuid

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, Select, SelectionList,
    Static,
)
import rich.text

from autotmux import __version__

from autotmux import (
    config, gateway as gateway_client, ipc, keepalive, keypad, lifecycle,
    notify, paths, urlhandler, warm_registry, webcontrol,
)
from autotmux import model
# The rows the table is built from are the model, not the view, and the
# browser client renders the same ones as a list. Imported rather than
# duplicated: two derivations of "which session is stale" is two answers.
# The two idle thresholds are deliberately NOT re-exported. They are
# rebound at runtime by apply_idle_thresholds(), and `from x import name`
# copies the binding -- a reader here would keep answering 300 forever
# while the model had moved on. Reach for model.IDLE_* instead.
from autotmux.model import (                                    # noqa: F401
    _IDLE_DOT, _OFFLINE_SESSION, _START_SHELL_SESSION,
    _attention_rank, _coerce_idle_seconds, _format_idle, _idle_marker,
    _idle_tier, _looks_stale, _session_label, _split_idle_marker,
    build_session_rows, filter_rows, plan_rows, session_rank,
)

STATE_FILE = paths.STATE_FILE
CTL_DIR = paths.CTL_DIR
PID_FILE = paths.PID_FILE
LOCK_FILE = PID_FILE + '.lock'
GUARD_FILE = paths.GUARD_FILE
SNAPSHOT_FILE = paths.SNAPSHOT_FILE
PREVIEW_SOCKET = paths.PREVIEW_SOCKET
WARM_DIR = paths.WARM_DIR
INTERACTIVE_CTL_DIR = paths.INTERACTIVE_CTL_DIR
_RUNTIME_DISCOVERY_ENABLED = False
_runtime_paths_lock = threading.Lock()
_NODE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_OFFLOAD_SLOTS = threading.Semaphore(8)
_CONTROL_COMMAND_SLOTS = threading.Semaphore(4)
_SLURM_COMMAND_SLOTS = threading.Semaphore(2)
_DAEMON_LAUNCH_SLOTS = threading.Semaphore(1)
_FRONTEND_COMMAND_CLEANUP_GRACE = 2.0
# Display prefix the gateway pool gives a login node (see gateway._login_key).
_LOGIN_NODE_PREFIX = 'login--'
_UI_FILE_READ_TIMEOUT = 2.5
_STATE_FILE_LIMIT = 8 * 1024 * 1024
_SNAPSHOT_FILE_LIMIT = 64 * 1024 * 1024
_CLOCK_ID = lifecycle.monotonic_clock_id()
# A process-local handle for this atmux instance's share of the outer-tmux
# passthrough lease.  The authoritative state lives in a session-local tmux
# user option so independent atmux processes cannot restore the outer prefix
# while another nested attach still needs it.
_outer_tmux_state: dict[str, object] | None = None
_OUTER_TMUX_OPTIONS = ('prefix', 'prefix2', 'key-table', 'status')
_OUTER_TMUX_LEASE_VERSION = 1
_OUTER_TMUX_LEASE_LIMIT = 64 * 1024
_OUTER_TMUX_OWNER_LIMIT = 128
_OUTER_TMUX_LOCK_TIMEOUT = 2.0
# tmux 2.7 defaults to a 500 ms ambiguity window after ESC.  In a nested
# client that window exists on both servers, which makes Vim/Alt/function-key
# input feel intermittently sticky.  Keep a small, non-zero window while an
# inner tmux is attached; 10 ms is tmux's widely-used low-latency setting and
# still leaves room for a terminal escape sequence delivered in one packet.
_OUTER_TMUX_NESTED_ESCAPE_TIME = 10
_OUTER_TMUX_LATENCY_LEASE_VERSION = 1
_PROXY_INPUT_BUFFER_LIMIT = 16 * 1024
_PROXY_OUTPUT_BUFFER_LIMIT = 64 * 1024
_PROXY_IO_CHUNK = 8 * 1024
_PROXY_IDLE_TIMEOUT = 0.1
_PREVIEW_LOOP_TICK = 0.25
_PREVIEW_CHANGED_DELAY = 1.0
_PREVIEW_UNCHANGED_MAX_DELAY = 8.0
_PREVIEW_CAPTURE_TIMEOUT = 8.0
_WARM_DRAIN_IDLE_GRACE = 0.01
_WARM_DRAIN_MAX_SECONDS = 0.25
_SSH_SETTINGS_LOCK = threading.Lock()
_SSH_SETTINGS = {
    'connect_timeout': int(config.DEFAULTS['connect_timeout']),
    'server_alive_int': int(config.DEFAULTS['server_alive_int']),
    'server_alive_max': int(config.DEFAULTS['server_alive_max']),
}
_GATEWAY_POOL: gateway_client.GatewayPool | None = None
_CLIENT_CONFIG_TIMEOUT = 2.5


def _gateway_mode() -> bool:
    return _GATEWAY_POOL is not None


def _operation_timeout(native: float) -> float:
    """Bound one operation across all gateways, not once per gateway."""
    if _GATEWAY_POOL is None:
        return float(native)
    return max(float(native),
               float(_GATEWAY_POOL.settings['state_timeout']) + 1.0)


def _load_client_config_bounded(timeout: float = _CLIENT_CONFIG_TIMEOUT):
    """Read optional client config without letting a stuck home mount freeze UI."""
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            value = (True, config.load_client())
        except Exception:
            value = (False, None)
        try:
            result.put(value, block=False)
        except queue.Full:
            pass

    threading.Thread(target=worker, daemon=True,
                     name='atmux-client-config').start()
    try:
        return result.get(timeout=max(0.01, float(timeout)))
    except queue.Empty:
        return False, None


def _next_preview_cadence(unchanged: bool,
                          streak: int) -> tuple[int, float]:
    """Return the next unchanged streak and delay after a preview result."""
    if not unchanged:
        return 0, _PREVIEW_CHANGED_DELAY
    streak = max(0, int(streak)) + 1
    delay = min(
        _PREVIEW_UNCHANGED_MAX_DELAY,
        2.0 ** min(streak, 20),
    )
    return streak, delay


def fmt_age(seconds) -> str:
    """A duration, in the largest unit that still says something.

    Every age on this screen was printed in raw seconds, which is fine for
    the first minute and unreadable after it: a stale daemon reported
    "stale (19313589s old)", which is a number nobody converts in their head
    and which reads as a fault in the display rather than in the daemon. The
    browser client has always said "5m ago" for the same quantity, so the two
    halves of the same tool disagreed about how to say the same thing.

    Kept coarse on purpose. These are all "how out of date is this", and past
    a minute the answer is a magnitude, not a measurement.
    """
    if seconds is None:
        return '?'
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return '?'
    if not math.isfinite(value):
        return '?'
    value = max(0.0, value)
    if value < 60:
        return f'{value:.0f}s'
    if value < 3600:
        return f'{value / 60:.0f}m'
    if value < 86400:
        return f'{value / 3600:.0f}h'
    return f'{value / 86400:.0f}d'


def _snapshot_age_seconds(snapshot: dict) -> float | None:
    if not isinstance(snapshot, dict):
        return None
    monotonic = snapshot.get('captured_monotonic')
    clock_id = snapshot.get('monotonic_clock_id')
    if ((clock_id is None or clock_id == _CLOCK_ID)
            and isinstance(monotonic, (int, float))
            and not isinstance(monotonic, bool)):
        age = time.monotonic() - float(monotonic)
        if math.isfinite(age) and age >= 0:
            return age
    epoch = snapshot.get('captured_epoch')
    if (isinstance(epoch, (int, float)) and not isinstance(epoch, bool)):
        age = time.time() - float(epoch)
        if math.isfinite(age) and age >= 0:
            return age
    return None


def _sync_active_runtime_paths() -> bool:
    """Follow the live daemon when login sessions disagree on XDG paths."""
    if not _RUNTIME_DISCOVERY_ENABLED:
        return False
    base = lifecycle.active_runtime_base(GUARD_FILE)
    if base is None:
        return False
    global STATE_FILE, SNAPSHOT_FILE, PREVIEW_SOCKET, WARM_DIR
    global PID_FILE, LOCK_FILE, CTL_DIR, INTERACTIVE_CTL_DIR
    with _runtime_paths_lock:
        changed = STATE_FILE != os.path.join(base, 'daemon.json')
        STATE_FILE = os.path.join(base, 'daemon.json')
        SNAPSHOT_FILE = os.path.join(base, 'snapshots.json')
        PREVIEW_SOCKET = os.path.join(base, 'preview.sock')
        WARM_DIR = os.path.join(base, 'warm')
        PID_FILE = os.path.join(base, 'daemon.pid')
        LOCK_FILE = PID_FILE + '.lock'
        CTL_DIR = os.path.join(base, 'ctl')
        INTERACTIVE_CTL_DIR = os.path.join(base, 'interactive-ctl')
    return changed


async def _offload(func, /, *args, **kwargs):
    """Run blocking work on a capped daemon thread and await its result.

    ``asyncio.to_thread`` uses the loop's default ThreadPoolExecutor.  Loop
    shutdown must join that executor, which can hold atmux open indefinitely if
    a filesystem call is stuck in NFS/D-state (and has a known shutdown-wakeup
    failure on some Python 3.12 builds).  These threads are daemonised, capped,
    and never joined by app shutdown; a wedged dependency degrades one slot
    instead of making `q` hang forever.
    """
    if not _OFFLOAD_SLOTS.acquire(blocking=False):
        raise RuntimeError('background I/O capacity is exhausted')
    # Do not use loop.call_soon_threadsafe() to publish the result.  Some
    # Python 3.12 builds used on the cluster fail to wake an idle debug event
    # loop through its self-pipe; IsolatedAsyncioTestCase reproduces the same
    # indefinite selector sleep.  A short event-loop-side poll needs no
    # cross-thread wakeup and still leaves the UI responsive.
    done = threading.Event()
    outcome = []

    def run():
        try:
            result = func(*args, **kwargs)
        except BaseException as error:
            outcome.append((False, error))
        else:
            outcome.append((True, result))
        finally:
            done.set()
            _OFFLOAD_SLOTS.release()

    thread = threading.Thread(target=run, daemon=True,
                              name=f'atmux-io-{getattr(func, "__name__", "work")}')
    try:
        thread.start()
    except BaseException:
        _OFFLOAD_SLOTS.release()
        raise
    while not done.is_set():
        await asyncio.sleep(0.02)
    succeeded, value = outcome[0]
    if succeeded:
        return value
    raise value


async def _offload_for(timeout: float, func, /, *args, **kwargs):
    """Give an interactive offload a user-visible wall-clock deadline."""
    return await asyncio.wait_for(
        _offload(func, *args, **kwargs), timeout=max(0.1, timeout))


class _FrontendCommandCapacityExhausted(RuntimeError):
    """A fixed command pool is occupied by earlier stuck cleanup calls."""


def _hard_subprocess_run(argv, *, timeout: float,
                         slots: threading.Semaphore, **kwargs):
    """Run a non-interactive command behind a second, hard wall deadline.

    ``subprocess.run(timeout=...)`` kills a timed-out child and then waits for
    it without another bound.  On an unhealthy NFS/Slurm path that cleanup can
    itself wedge forever.  Put it on a capped daemon thread so an interactive
    keypress or daemon recovery receives an answer while at most a fixed number
    of irrecoverable kernel waits remain in the process.
    """
    if not slots.acquire(blocking=False):
        raise _FrontendCommandCapacityExhausted(
            f'command capacity exhausted before running {argv!r}')
    done = threading.Event()
    result = {}

    def run():
        try:
            result['value'] = subprocess.run(
                argv, timeout=timeout, **kwargs)
        except BaseException as error:
            result['error'] = error
        finally:
            slots.release()
            done.set()

    try:
        threading.Thread(
            target=run, daemon=True,
            name=f'atmux-command-{os.path.basename(str(argv[0]))}',
        ).start()
    except BaseException:
        slots.release()
        raise
    hard_timeout = max(0.1, timeout) + _FRONTEND_COMMAND_CLEANUP_GRACE
    if not done.wait(timeout=hard_timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    if 'error' in result:
        raise result['error']
    return result['value']


def _valid_node(node: str) -> bool:
    """Whether ``node`` is a plain ssh destination rather than an option."""
    return isinstance(node, str) and bool(_NODE_RE.fullmatch(node))


_node_label = model.node_label


def _time_left_label(value) -> str:
    """Slurm's ``D-HH:MM:SS`` in a form that costs 5 columns instead of 10.

    The exact second of a walltime that ends tomorrow is noise; the magnitude
    is what the user acts on, and the space it frees is what lets LOAD and
    STATUS fit beside it at all.
    """
    text = str(value or '').strip()
    if not text or text in {'-', '?'}:
        return text or ''
    seconds = keepalive.parse_time_left(text)
    if seconds is None:
        return text
    if seconds == math.inf:
        return '∞'
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f'{days}d{hours}h' if hours else f'{days}d'
    if hours:
        return f'{hours}h{minutes:02d}' if minutes else f'{hours}h'
    return f'{minutes}m'


def _cluster_health_note(clusters) -> str:
    """Which whole clusters are not answering, which is usually none.

    A single unreachable login node is routine and the pool routes around it;
    a cluster with *no* reachable entry point means every machine behind it
    has silently left the table, and silence is the wrong way to say that.
    """
    if not isinstance(clusters, list) or len(clusters) < 2:
        return ''
    down = [str(entry.get('name') or '?') for entry in clusters
            if isinstance(entry, dict) and entry.get('last_error')
            and not entry.get('nodes')]
    if not down:
        return ''
    return f" · ⚠ cluster unreachable: {', '.join(sorted(down))}"


def _dedent_block(text: str) -> str:
    """Drop the indent every line shares.

    ``squeue`` right-aligns JOBID in a field wide enough for any of them, so
    its output arrives with about ten leading spaces on every row. On a desktop
    that is invisible; on a 58-column phone it is a sixth of the screen spent
    on nothing. Only whitespace common to *all* non-blank lines goes, so the
    columns stay aligned with each other.
    """
    if not isinstance(text, str) or not text:
        return ''
    lines = text.splitlines()
    indents = [len(line) - len(line.lstrip(' '))
               for line in lines if line.strip()]
    if not indents:
        return text
    common = min(indents)
    if not common:
        return text
    return '\n'.join(line[common:] if line.strip() else line
                      for line in lines)


def _gateway_health_note(items) -> str:
    """What to say about the gateways we are *not* using, which is usually
    nothing.

    The old text was ``1/4 healthy``, which read as "three are broken" when in
    fact three had simply never been needed: sticky routing sends every request
    to one gateway, so the others hold no successful probe and are counted
    ``unknown``.  Saying nothing while nothing is wrong matches the STATUS
    column, and leaves a warning here meaning what a warning should.
    """
    if not isinstance(items, list):
        return ''
    down = sorted(
        str(entry.get('name') or '?')
        for entry in items
        if isinstance(entry, dict) and entry.get('state') in ('backoff', 'probing')
    )
    if not down:
        return ''
    return f" · ⚠ unreachable: {', '.join(down)}"


def _load_label(load, cpus) -> str:
    """One cell for "how busy" and "how big": ``4.4/12``.

    Two separate columns spent a separator and a header on a number that is
    only meaningful next to the other one.
    """
    load = str(load or '').strip()
    cpus = str(cpus or '').strip()
    try:
        # One decimal: the second is false precision on a 1-minute average,
        # and it is a column the table cannot spare.
        load = f'{float(load):.1f}'
    except ValueError:
        pass
    if load and cpus:
        return f'{load}/{cpus}'
    return load or cpus or ''


def _session_cell(session: str, windows) -> str:
    """SESSION as shown in the table, carrying the window count only when it
    says something.

    A dedicated WIN column spent a header and ~5 cells of width on a number
    that is ``1`` for virtually every session; a suffix costs nothing until
    a session actually has more than one window.
    """
    label = _session_label(session)
    try:
        count = int(str(windows).strip())
    except (TypeError, ValueError):
        return label
    return f'{label} ·{count}' if count > 1 else label




def _ctl_path(node: str) -> str:
    return paths.control_path(node, CTL_DIR)


def _interactive_ctl_path(node: str) -> str:
    return paths.control_path(f'interactive-{node}', INTERACTIVE_CTL_DIR)


def _apply_daemon_ssh_settings(state: dict) -> None:
    """Adopt validated daemon-published SSH settings without reading NFS."""
    published = state.get('ssh_config') if isinstance(state, dict) else None
    if not isinstance(published, dict):
        return
    validated = {}
    bounds = {
        'connect_timeout': (1, 600),
        'server_alive_int': (1, 3600),
        'server_alive_max': (1, 100),
    }
    for key, (minimum, maximum) in bounds.items():
        value = published.get(key)
        if (isinstance(value, int) and not isinstance(value, bool)
                and minimum <= value <= maximum):
            validated[key] = value
    with _SSH_SETTINGS_LOCK:
        _SSH_SETTINGS.update(validated)


def _get_ssh_args(node: str, *, direct: bool = False,
                  interactive: bool = False) -> list:
    """Return ControlPath + keepalive flags so an idle slave channel
    doesn't get silently dropped by NAT/firewalls (which is what kicks
    `<Start Shell>` users out — plain bash has no keepalive of its own,
    unlike tmux which talks to its server periodically)."""
    with _SSH_SETTINGS_LOCK:
        settings = dict(_SSH_SETTINGS)
    args = [
        '-o', 'BatchMode=yes',
        '-o', f"ConnectTimeout={settings['connect_timeout']}",
        '-o', 'ConnectionAttempts=1',
        '-o', f"ServerAliveInterval={settings['server_alive_int']}",
        '-o', f"ServerAliveCountMax={settings['server_alive_max']}",
    ]
    if interactive:
        # Some networks mishandle OpenSSH's interactive DSCP marking.  Newer
        # clients also add fixed-rate keystroke packets; under loss those extra
        # packets amplify queueing and make input arrive in visible bursts.
        # IgnoreUnknown keeps the argv compatible with OpenSSH < 9.5.
        args += gateway_client.interactive_ssh_options()
    if direct:
        # Explicitly bypass a stale/exhausted mux. Merely omitting ControlPath
        # still lets ~/.ssh/config select one again.
        args += ['-o', 'ControlPath=none', '-o', 'ControlMaster=no']
    elif interactive:
        args += [
            '-o', 'ControlMaster=auto',
            '-o', 'ControlPersist=300',
            '-o', f'ControlPath={_interactive_ctl_path(node)}',
        ]
    else:
        ctl = _ctl_path(node)
        if os.path.exists(ctl):
            args += ['-o', f'ControlPath={ctl}']
    return args


def _warm_helper_argv(node: str, control_path: str,
                      ssh_argv: list[str]) -> list[str]:
    parent_pid = os.getpid()
    parent_token = lifecycle.process_token(parent_pid)
    if not parent_token:
        raise OSError('cannot identify atmux parent for warm SSH')
    return [
        sys.executable, '-m', 'autotmux.ssh_child',
        '--parent-pid', str(parent_pid),
        '--parent-token', parent_token,
        '--registry-dir', WARM_DIR,
        '--node', node,
        '--control-path', control_path,
        '--', *ssh_argv,
    ]


def _copy_terminal_winsize(pty_fd: int) -> bool:
    """Copy the real terminal geometry to an ssh pty.

    A newly-created pty starts at 0x0 on Linux.  If the pre-warmed ssh shell
    runs ``tmux attach`` before its pty has been resized, tmux can initially
    register that client at a tiny/default size and letterbox the whole
    session.  Search the standard streams for the controlling terminal and
    copy its complete winsize (including pixel fields) before starting ssh and
    again immediately before attach.
    """
    empty = struct.pack('HHHH', 0, 0, 0, 0)
    for terminal_fd in (0, 1, 2):
        try:
            packed = fcntl.ioctl(terminal_fd, termios.TIOCGWINSZ, empty)
            rows, cols, _xpixels, _ypixels = struct.unpack('HHHH', packed)
            if rows <= 0 or cols <= 0:
                continue
            fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, packed)
            return True
        except (OSError, TypeError, ValueError, struct.error):
            continue
    return False


# ── nested-tmux prefix handling ──────────────────────────────────────────────
#
# When atmux itself runs inside tmux and the user attaches ANOTHER tmux session
# (local or remote), the two servers nest in one terminal. The OUTER tmux eats
# the prefix (C-b) before the inner one ever sees it, so the inner session's key
# bindings appear dead while ordinary typing still works. The canonical fix is
# to make the outer tmux transparent for the duration of the nested attach —
# prefix/prefix2 None, a private key-table, status hidden — so every key (C-b
# included) flows straight to the inner tmux. F12 in that private table restores
# the exact outer settings as a crash-safety escape without replacing user
# binds.  The lease metadata below is shared by every atmux process using the
# same outer session; otherwise one concurrent attach can restore the prefix
# while another is still active and make the inner shortcuts appear to die.


def _outer_tmux_context() -> dict[str, str] | None:
    """Parse the outer server and session identity from ``$TMUX``.

    Socket paths may legally contain commas, so split from the right.  The
    server PID is part of the identity to prevent a restarted server reusing a
    socket path/session number from colliding with a stale lock file.
    """
    raw = os.environ.get('TMUX', '')
    parts = raw.rsplit(',', 2)
    if (len(parts) != 3 or not parts[0]
            or not parts[1].isdigit() or not parts[2].isdigit()):
        return None
    sock, server_pid, session_number = parts
    server_identity = f'{sock}\0{server_pid}'
    server_digest = hashlib.sha256(
        server_identity.encode('utf-8', 'surrogateescape')).hexdigest()[:20]
    identity = f'{server_identity}\0{session_number}'
    digest = hashlib.sha256(
        identity.encode('utf-8', 'surrogateescape')).hexdigest()[:20]
    return {
        'socket': sock,
        'server_pid': server_pid,
        'session': f'${session_number}',
        'digest': digest,
        'server_digest': server_digest,
        'table': f'autotmux-off-{digest}',
        'state_key': f'@autotmux_nested_{digest}',
        'latency_state_key': f'@autotmux_latency_{server_digest}',
        # GUARD_FILE is deliberately host-stable by default (/tmp + uid), even
        # when two login environments disagree about XDG_RUNTIME_DIR.
        'lock_path': f'{os.path.abspath(GUARD_FILE)}.nested-{digest}.lock',
        'latency_lock_path': (
            f'{os.path.abspath(GUARD_FILE)}.latency-{server_digest}.lock'),
    }


def _acquire_outer_tmux_lock(context: dict[str, str]) -> int | None:
    """Take a short cross-process lock for lease read/modify/write operations."""
    try:
        fd = lifecycle.open_lock_file(context['lock_path'], create=True)
    except OSError:
        return None
    deadline = time.monotonic() + _OUTER_TMUX_LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.02)
        except OSError:
            os.close(fd)
            return None


def _release_outer_tmux_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _outer_tmux_snapshot(
        context: dict[str, str],
) -> tuple[dict[str, str | None], str | None] | None:
    """Return session-local options and the encoded shared lease, if present."""
    output = _tmux_output('show-options', '-t', context['session'])
    if output is None:
        return None
    options: dict[str, str | None] = {
        option: None for option in _OUTER_TMUX_OPTIONS
    }
    encoded = None
    try:
        for line in output.splitlines():
            name, separator, _raw_value = line.partition(' ')
            if name == context['state_key']:
                # tmux quotes user-option strings and backslash-escapes the
                # JSON quotes in `show-options`; shlex reverses that display
                # encoding without interpreting the JSON itself.
                parts = shlex.split(line)
                encoded = parts[1] if separator and len(parts) >= 2 else ''
                continue
            if name not in options:
                continue
            parts = shlex.split(line)
            if len(parts) >= 2:
                options[name] = parts[1]
    except ValueError:
        return None
    return options, encoded


def _decode_outer_tmux_lease(
        encoded: str | None, context: dict[str, str],
) -> dict[str, object] | None:
    """Validate untrusted tmux user-option metadata before using it as argv."""
    if encoded is None:
        return None
    if not encoded or len(encoded.encode('utf-8', 'surrogateescape')) > _OUTER_TMUX_LEASE_LIMIT:
        raise ValueError('invalid outer-tmux lease')
    try:
        lease = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid outer-tmux lease') from error
    if (not isinstance(lease, dict)
            or lease.get('version') != _OUTER_TMUX_LEASE_VERSION
            or lease.get('table') != context['table']):
        raise ValueError('invalid outer-tmux lease')
    original = lease.get('original')
    if not isinstance(original, dict) or set(original) != set(_OUTER_TMUX_OPTIONS):
        raise ValueError('invalid outer-tmux lease')
    clean_original: dict[str, str | None] = {}
    for option in _OUTER_TMUX_OPTIONS:
        value = original.get(option)
        if value is not None and (not isinstance(value, str) or len(value) > 512):
            raise ValueError('invalid outer-tmux lease')
        clean_original[option] = value
    owners = lease.get('owners')
    if not isinstance(owners, list) or len(owners) > _OUTER_TMUX_OWNER_LIMIT:
        raise ValueError('invalid outer-tmux lease')
    clean_owners = []
    seen = set()
    for owner in owners:
        if not isinstance(owner, dict):
            raise ValueError('invalid outer-tmux lease')
        owner_id = owner.get('id')
        pid = owner.get('pid')
        token = owner.get('token')
        if (not isinstance(owner_id, str) or not owner_id or len(owner_id) > 128
                or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                or (token is not None
                    and (not isinstance(token, str) or len(token) > 128))):
            raise ValueError('invalid outer-tmux lease')
        if owner_id in seen:
            continue
        seen.add(owner_id)
        clean_owners.append({'id': owner_id, 'pid': pid, 'token': token})
    return {
        'version': _OUTER_TMUX_LEASE_VERSION,
        'table': context['table'],
        'original': clean_original,
        'owners': clean_owners,
    }


def _live_outer_tmux_owners(owners) -> list[dict[str, object]]:
    """Drop processes that died (or whose PID has since been reused)."""
    live = []
    for owner in owners:
        try:
            alive = lifecycle.same_process(owner['pid'], owner.get('token'))
        except (OSError, TypeError, ValueError):
            alive = False
        if alive:
            live.append(owner)
    return live


def _encode_outer_tmux_lease(lease: dict[str, object]) -> str:
    return json.dumps(lease, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def _outer_tmux_latency_snapshot(
        context: dict[str, str],
) -> tuple[int, str | None] | None:
    """Return the server escape-time and its shared low-latency lease.

    ``escape-time`` is a server option rather than a session option.  The
    metadata therefore lives in a global user option and is protected by a
    server-wide lock, so nested attaches in different outer sessions cannot
    restore the value out from under each other.
    """
    raw_escape = _tmux_output('show-options', '-s', '-v', 'escape-time')
    global_options = _tmux_output('show-options', '-g')
    if raw_escape is None or global_options is None:
        return None
    value = raw_escape.strip()
    if not re.fullmatch(r'[0-9]+', value):
        return None
    escape_time = int(value)
    encoded = None
    try:
        for line in global_options.splitlines():
            name, separator, _raw_value = line.partition(' ')
            if name != context['latency_state_key']:
                continue
            parts = shlex.split(line)
            encoded = parts[1] if separator and len(parts) >= 2 else ''
            break
    except ValueError:
        return None
    return escape_time, encoded


def _decode_outer_tmux_latency_lease(
        encoded: str | None, context: dict[str, str],
) -> dict[str, object] | None:
    """Validate server-wide escape-time lease metadata from tmux."""
    if encoded is None:
        return None
    if (not encoded
            or len(encoded.encode('utf-8', 'surrogateescape'))
            > _OUTER_TMUX_LEASE_LIMIT):
        raise ValueError('invalid outer-tmux latency lease')
    try:
        lease = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid outer-tmux latency lease') from error
    original = lease.get('original') if isinstance(lease, dict) else None
    target = lease.get('target') if isinstance(lease, dict) else None
    if (not isinstance(lease, dict)
            or lease.get('version') != _OUTER_TMUX_LATENCY_LEASE_VERSION
            or isinstance(original, bool) or not isinstance(original, int)
            or isinstance(target, bool) or not isinstance(target, int)
            or not 0 <= original <= 2_147_483_647
            or not 0 <= target <= _OUTER_TMUX_NESTED_ESCAPE_TIME
            or target > original):
        raise ValueError('invalid outer-tmux latency lease')
    owners = lease.get('owners')
    if not isinstance(owners, list) or len(owners) > _OUTER_TMUX_OWNER_LIMIT:
        raise ValueError('invalid outer-tmux latency lease')
    clean_owners = []
    seen = set()
    for owner in owners:
        if not isinstance(owner, dict):
            raise ValueError('invalid outer-tmux latency lease')
        owner_id = owner.get('id')
        pid = owner.get('pid')
        token = owner.get('token')
        if (not isinstance(owner_id, str) or not owner_id or len(owner_id) > 128
                or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                or (token is not None
                    and (not isinstance(token, str) or len(token) > 128))):
            raise ValueError('invalid outer-tmux latency lease')
        if owner_id in seen:
            continue
        seen.add(owner_id)
        clean_owners.append({'id': owner_id, 'pid': pid, 'token': token})
    return {
        'version': _OUTER_TMUX_LATENCY_LEASE_VERSION,
        'original': original,
        'target': target,
        'owners': clean_owners,
    }


def _encode_outer_tmux_latency_lease(lease: dict[str, object]) -> str:
    return json.dumps(lease, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def _acquire_outer_tmux_latency(
        context: dict[str, str],
) -> dict[str, object] | None:
    """Lease a low outer-server escape-time for one nested attach."""
    lock_context = {'lock_path': context['latency_lock_path']}
    lock_fd = _acquire_outer_tmux_lock(lock_context)
    if lock_fd is None:
        return None
    try:
        snapshot = _outer_tmux_latency_snapshot(context)
        if snapshot is None:
            return None
        current, encoded = snapshot
        try:
            lease = _decode_outer_tmux_latency_lease(encoded, context)
        except ValueError:
            return None
        if lease is None:
            lease = {
                'version': _OUTER_TMUX_LATENCY_LEASE_VERSION,
                'original': current,
                'target': min(current, _OUTER_TMUX_NESTED_ESCAPE_TIME),
                'owners': [],
            }
        owners = _live_outer_tmux_owners(lease['owners'])
        if len(owners) >= _OUTER_TMUX_OWNER_LIMIT:
            return None
        owner = {
            'id': uuid.uuid4().hex,
            'pid': os.getpid(),
            'token': lifecycle.process_token(os.getpid()),
        }
        owners.append(owner)
        lease['owners'] = owners
        try:
            encoded_lease = _encode_outer_tmux_latency_lease(lease)
            lease = _decode_outer_tmux_latency_lease(encoded_lease, context)
        except ValueError:
            return None
        if not _tmux(
                'set-option', '-g', context['latency_state_key'],
                encoded_lease, ';',
                'set-option', '-s', 'escape-time', str(lease['target'])):
            return None
        return {
            'owner_id': owner['id'],
            'original': lease['original'],
            'target': lease['target'],
        }
    finally:
        _release_outer_tmux_lock(lock_fd)


def _release_outer_tmux_latency(
        context: dict[str, str], handle: dict[str, object] | None,
) -> bool:
    """Release one server-wide low-latency owner and restore the last value."""
    if handle is None:
        return True
    lock_context = {'lock_path': context['latency_lock_path']}
    lock_fd = _acquire_outer_tmux_lock(lock_context)
    if lock_fd is None:
        return False
    try:
        snapshot = _outer_tmux_latency_snapshot(context)
        if snapshot is None:
            return False
        _current, encoded = snapshot
        if encoded is None:
            # F12 normally restores before deleting the metadata.  Also repair
            # a manually-deleted lease instead of silently leaving 10 ms set.
            original = handle.get('original')
            if (isinstance(original, bool) or not isinstance(original, int)
                    or not 0 <= original <= 2_147_483_647):
                return False
            return _tmux(
                'set-option', '-s', 'escape-time', str(original))
        try:
            lease = _decode_outer_tmux_latency_lease(encoded, context)
        except ValueError:
            return False
        if lease is None:
            return False
        owner_id = handle.get('owner_id')
        remaining = [
            owner for owner in lease['owners']
            if owner.get('id') != owner_id
        ]
        lease['owners'] = _live_outer_tmux_owners(remaining)
        if lease['owners']:
            return _tmux(
                'set-option', '-g', context['latency_state_key'],
                _encode_outer_tmux_latency_lease(lease), ';',
                'set-option', '-s', 'escape-time', str(lease['target']))
        return _tmux(
            'set-option', '-s', 'escape-time', str(lease['original']), ';',
            'set-option', '-g', '-u', context['latency_state_key'])
    finally:
        _release_outer_tmux_lock(lock_fd)


def _outer_tmux_recovery_args(
        context: dict[str, str], original: dict[str, str | None],
        latency: dict[str, object] | None = None,
) -> list[str]:
    """Commands bound to F12; escaped separators keep them inside the bind."""
    separator = '\\;'
    recovery: list[str] = []
    for option in _OUTER_TMUX_OPTIONS:
        value = original[option]
        if value is None:
            recovery.extend((
                'set-option', '-u', '-t', context['session'], option, separator))
        else:
            recovery.extend((
                'set-option', '-t', context['session'], option, value, separator))
    recovery.extend((
        'set-option', '-u', '-t', context['session'], context['state_key'], separator,
        'unbind-key', '-T', context['table'], 'F12', separator,
    ))
    if latency is not None:
        escape_time = latency.get('original')
        if (isinstance(escape_time, int) and not isinstance(escape_time, bool)
                and 0 <= escape_time <= 2_147_483_647):
            recovery.extend((
                'set-option', '-s', 'escape-time', str(escape_time), separator,
                'set-option', '-g', '-u', context['latency_state_key'], separator,
            ))
    recovery.extend(('refresh-client', '-S'))
    return recovery


def _outer_tmux_restore_args(
        context: dict[str, str], original: dict[str, str | None],
) -> list[str]:
    restore: list[str] = []
    for option in _OUTER_TMUX_OPTIONS:
        value = original[option]
        if value is None:
            restore.extend((
                'set-option', '-u', '-t', context['session'], option, ';'))
        else:
            restore.extend((
                'set-option', '-t', context['session'], option, value, ';'))
    restore.extend((
        'set-option', '-u', '-t', context['session'], context['state_key'], ';',
        'unbind-key', '-T', context['table'], 'F12',
    ))
    return restore


def _tmux(*args) -> bool:
    """Run one tmux control command against the OUTER server (the one named in
    $TMUX), swallowing all output/errors. No-op when not inside tmux.

    The subprocess itself lives on a capped daemon thread. Python's ordinary
    timeout path kills *and then waits without a second bound*; a tmux process
    stuck in kernel I/O could otherwise freeze the Textual event loop during
    step-aside/restore and make the whole UI look dead.
    """
    context = _outer_tmux_context()
    if context is None:
        return False
    if not _CONTROL_COMMAND_SLOTS.acquire(blocking=False):
        return False
    sock = context['socket']
    done = threading.Event()
    result = {}

    def run():
        try:
            result['process'] = subprocess.run(
                ['tmux', '-S', sock, *args],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1)
        except BaseException as error:
            result['error'] = error
        finally:
            _CONTROL_COMMAND_SLOTS.release()
            done.set()

    try:
        threading.Thread(target=run, daemon=True, name='atmux-tmux-control').start()
    except BaseException:
        _CONTROL_COMMAND_SLOTS.release()
        return False
    if not done.wait(timeout=1.5):
        return False
    proc = result.get('process')
    return proc is not None and proc.returncode == 0


def _tmux_output(*args) -> str | None:
    """Bounded outer-tmux query; return stdout only on a clean exit."""
    context = _outer_tmux_context()
    if context is None or not _CONTROL_COMMAND_SLOTS.acquire(blocking=False):
        return None
    sock = context['socket']
    done = threading.Event()
    result = {}

    def run():
        try:
            result['process'] = subprocess.run(
                ['tmux', '-S', sock, *args],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=1)
        except BaseException as error:
            result['error'] = error
        finally:
            _CONTROL_COMMAND_SLOTS.release()
            done.set()

    try:
        threading.Thread(target=run, daemon=True,
                         name='atmux-tmux-query').start()
    except BaseException:
        _CONTROL_COMMAND_SLOTS.release()
        return None
    if not done.wait(timeout=1.5):
        return None
    proc = result.get('process')
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def _tmux_step_aside() -> bool:
    """Make the OUTER tmux transparent so a nested tmux we're about to attach
    receives every key, including the C-b prefix. F12 is bound only in an
    atmux-private temporary table as a crash-safety escape: even if atmux dies
    before _tmux_restore() runs, F12 brings the exact outer settings back while
    any user binding for F12 remains untouched.

    The `'\\;'` argv tokens are literal escaped semicolons: that is the one form
    that makes tmux 2.7 bind the whole command list to the key (a bare ';' token
    ends the bind-key command instead). Verified on tmux 2.7."""
    global _outer_tmux_state
    # If an earlier restore failed, do not overwrite this process's only lease
    # handle. A successful restore may leave another process's lease active.
    if _outer_tmux_state is not None and not _tmux_restore():
        return False
    context = _outer_tmux_context()
    if context is None:
        return False
    lock_fd = _acquire_outer_tmux_lock(context)
    if lock_fd is None:
        return False
    try:
        snapshot = _outer_tmux_snapshot(context)
        if snapshot is None:
            return False
        current_options, encoded = snapshot
        try:
            lease = _decode_outer_tmux_lease(encoded, context)
        except ValueError:
            # Never treat a corrupted transparent state as the user's original
            # configuration; F12 from the existing binding remains the safe
            # recovery path.
            return False
        if lease is None:
            lease = {
                'version': _OUTER_TMUX_LEASE_VERSION,
                'table': context['table'],
                'original': current_options,
                'owners': [],
            }
        owners = _live_outer_tmux_owners(lease['owners'])
        if len(owners) >= _OUTER_TMUX_OWNER_LIMIT:
            return False
        owner = {
            'id': uuid.uuid4().hex,
            'pid': os.getpid(),
            'token': lifecycle.process_token(os.getpid()),
        }
        owners.append(owner)
        lease['owners'] = owners
        try:
            encoded_lease = _encode_outer_tmux_lease(lease)
            # New snapshots need the same validation as shared metadata. This
            # prevents an exotic oversized tmux option from creating a lease
            # that no process could later decode and restore.
            lease = _decode_outer_tmux_lease(encoded_lease, context)
        except ValueError:
            return False
        original = lease['original']
        # escape-time is server-wide, so it has a separate lease shared across
        # every outer session on this tmux server.  Failure only disables the
        # latency optimization; the essential prefix passthrough still works.
        latency = _acquire_outer_tmux_latency(context)
        _outer_tmux_state = {
            'context': context,
            'owner_id': owner['id'],
            'original': original,
            'latency': latency,
        }
        recovery = _outer_tmux_recovery_args(context, original, latency)
        enabled = _tmux(
            'bind-key', '-T', context['table'], 'F12', *recovery, ';',
            'set-option', '-t', context['session'], context['state_key'],
            encoded_lease, ';',
            'set-option', '-t', context['session'], 'prefix', 'None', ';',
            'set-option', '-t', context['session'], 'prefix2', 'None', ';',
            'set-option', '-t', context['session'], 'key-table',
            context['table'], ';',
            'set-option', '-t', context['session'], 'status', 'off',
        )
        return enabled
    finally:
        _release_outer_tmux_lock(lock_fd)


def _tmux_restore() -> bool:
    """Release this process's passthrough lease.

    The final live owner restores the exact outer options; earlier owners only
    remove themselves from shared metadata. Safe after the user presses F12,
    which restores the session and removes that metadata atomically.
    """
    global _outer_tmux_state
    if _outer_tmux_state is None:
        return True
    handle = _outer_tmux_state
    context = handle.get('context')
    if not isinstance(context, dict):
        return False
    lock_fd = _acquire_outer_tmux_lock(context)
    if lock_fd is None:
        return False
    session_released = False
    repaint_needed = False
    try:
        snapshot = _outer_tmux_snapshot(context)
        if snapshot is None:
            return False
        current_options, encoded = snapshot
        if encoded is None:
            # F12 removes metadata only after restoring the key table. If the
            # private table is still active, metadata was deleted/corrupted by
            # some other path and claiming success would silently strand the
            # outer session without its shortcuts.
            if current_options.get('key-table') == context['table']:
                return False
            # F12 already performed the session-local recovery.  Continue
            # below so its server-wide escape-time lease is reconciled too.
            session_released = True
            repaint_needed = True
        else:
            try:
                lease = _decode_outer_tmux_lease(encoded, context)
            except ValueError:
                return False
            if lease is None:
                return False
            owner_id = handle.get('owner_id')
            remaining = [
                owner for owner in lease['owners']
                if owner.get('id') != owner_id
            ]
            lease['owners'] = _live_outer_tmux_owners(remaining)
            if lease['owners']:
                session_released = _tmux(
                    'set-option', '-t', context['session'], context['state_key'],
                    _encode_outer_tmux_lease(lease))
            else:
                session_released = _tmux(
                    *_outer_tmux_restore_args(context, lease['original']))
                repaint_needed = session_released
    finally:
        _release_outer_tmux_lock(lock_fd)
    if not session_released:
        return False
    if repaint_needed:
        # Restoring the status line changes the pane height. This repaint is
        # best-effort and deliberately separate: `refresh-client` has no
        # target in a detached tmux server and must never turn a successful
        # option restore into a reported failure.
        _tmux('refresh-client', '-S')
    latency_released = _release_outer_tmux_latency(
        context, handle.get('latency'))
    if latency_released:
        _outer_tmux_state = None
    return latency_released


def _will_nest_tmux(sess: str) -> bool:
    """True when attaching `sess` from our current context will nest a tmux
    inside the outer tmux — i.e. we're inside tmux and the target is a real
    tmux session (not a plain shell or an offline placeholder)."""
    return bool(os.environ.get('TMUX')) and sess not in (
        _START_SHELL_SESSION, _OFFLINE_SESSION)


def _handoff_outer_tmux_client(helper_args: list[str]) -> bool:
    """Run an interactive helper outside the surrounding tmux client.

    Making the outer server transparent fixes key bindings, but its renderer is
    still in the byte path and can lag behind a smooth direct SSH connection.
    tmux 2.7+'s ``detach-client -E`` lets the real client temporarily replace
    itself with our attach helper.  The dashboard keeps running in its detached
    pane; after the helper exits, the same client reattaches to it.

    In gateway mode the helper receives the live pool's gateways explicitly,
    with the current route first, so one-shot overrides survive the handoff.
    """
    context = _outer_tmux_context()
    if context is None:
        return False
    client_tty = _tmux_output('display-message', '-p', '#{client_tty}')
    if client_tty is None:
        return False
    client_tty = client_tty.strip()
    if (not client_tty or len(client_tty) > 4096
            or not os.path.isabs(client_tty)
            or any(ord(char) < 32 or ord(char) == 127 for char in client_tty)):
        return False
    deployment_args = []
    if _GATEWAY_POOL is not None:
        gateways = list(_GATEWAY_POOL.gateways)
        active = _GATEWAY_POOL.active_gateway
        if active in gateways:
            gateways.remove(active)
            gateways.insert(0, active)
        for gateway in gateways:
            deployment_args.extend(['--gateway', gateway])
    helper = [
        sys.executable, '-m', 'autotmux.cli',
        *deployment_args, *helper_args,
    ]
    reattach = [
        'tmux', '-S', context['socket'],
        'attach-session', '-t', context['session'],
    ]
    command = (
        f"unset TMUX TMUX_PANE; {shlex.join(helper)}; "
        f"exec {shlex.join(reattach)}"
    )
    return _tmux(
        'detach-client', '-t', client_tty, '-E', command)


def _local_attach_argv(session: str) -> list[str]:
    """Attach to a tmux session on this machine, taking the session over.

    ``-d`` detaches whatever else is holding it.  tmux sizes a session's
    windows to its *smallest* attached client, so a second client -- a window
    on another display, or one left behind by a connection that dropped without
    tmux noticing -- pins the window to that smaller size and leaves the rest
    of the bigger window blank.  Every remote path already attaches this way
    (see the agent, and the rewrite in _run_remote_user_command); local ones
    did not, which is exactly where two displays of different sizes meet.
    """
    return ['tmux', 'attach-session', '-d', '-t', session]


def _handover_banner(node: str, session: str) -> str:
    """The line printed as the dashboard hands the terminal over.

    A tmux session that finished hours ago paints one static screen and then
    nothing. With the table gone and no other output, that is exactly what a
    hung dashboard looks like -- so the one thing worth saying is what you are
    now looking at and which key brings the table back.
    """
    if session == _START_SHELL_SESSION:
        return (f'\n[atmux] shell on {node} — exit returns to the dashboard.')
    label = _session_label(session)
    if node == 'localhost':
        where = 'this machine'
    else:
        where = node
    # The prefix is whatever the user has bound; naming the default is a hint,
    # not a promise, so it is qualified rather than stated outright.
    return (f'\n[atmux] attaching to {label} on {where} — detach '
            f'(prefix then d, Ctrl-B d by default) returns to the dashboard.')


def _open_new_terminal_window(node: str, session: str) -> tuple[bool, str]:
    """Open a separate terminal window already attached to this session.

    `o` promised "a new window" but could only deliver one when atmux was
    itself running inside tmux, since that is the only window manager it had.
    Run straight from a terminal -- which is how the local client is normally
    used -- it fell through to attaching in place, i.e. exactly what Enter
    does, so the key appeared to do nothing.

    macOS already has the missing piece: the ``atmux://`` handler installed for
    chat links opens a window and attaches. Reusing it means `o` inherits the
    same behaviour, including raising the window a session is already showing
    rather than opening a second client on it.

    Goes through ``open`` rather than driving the terminal directly so that
    atmux never needs to send Apple events itself: the applet holds that
    permission, and asking for a second grant here would put a consent dialog
    in front of a keypress.
    """
    if sys.platform != 'darwin':
        return False, 'opening a separate window needs the macOS atmux:// handler'
    url = notify.attach_url(node, session)
    if not url:
        return False, 'this row cannot be expressed as an atmux:// link'
    try:
        result = subprocess.run(['open', url], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return False, ' '.join(str(error).split())[:120]
    if result.returncode != 0:
        detail = ' '.join((result.stderr or '').split())[:120]
        return False, (detail
                       or 'no handler for atmux:// — run '
                          'contrib/install-url-handler-macos.sh')
    return True, ''


def _run_user_command(argv) -> tuple[int, str]:
    """Run an intentional interactive command without crashing the TUI.

    Output remains attached to the user's terminal. The returned error string
    lets the caller show a durable notification after Textual redraws its
    alternate screen (where a transient shell/ssh error would otherwise vanish).
    """
    try:
        return subprocess.call(argv), ''
    except OSError as error:
        command = os.path.basename(str(argv[0])) if argv else 'command'
        if error.errno == errno.ENOENT:
            # "tmux: No such file or directory" reads as a missing *session*.
            # It is nearly always a PATH problem instead, and a surprising one:
            # a window opened from a GUI (a clicked atmux:// link, an editor's
            # terminal) inherits /usr/bin:/bin and not the user's PATH.
            return 127, (f'{command} is not on PATH — install it, or launch '
                         f'atmux from a shell where {command} works')
        detail = error.strerror or str(error)
        return 127, f'{command}: {detail}'


def _node_network_degraded(state: dict, node: str) -> bool:
    nodes = state.get('nodes') if isinstance(state, dict) else None
    item = nodes.get(node) if isinstance(nodes, dict) else None
    network_state = item.get('network') if isinstance(item, dict) else None
    return (isinstance(network_state, dict)
            and network_state.get('state') in {
                'suspect', 'offline', 'half-open'})


def _report_network_event(node: str, outcome: str, reason: str,
                          source: str) -> None:
    """Best-effort publication of an interactive result to the shared breaker."""
    if node == 'localhost' or outcome not in {'success', 'failure'}:
        return
    if _GATEWAY_POOL is not None:
        # The remote interactive agent reports the compute transport result to
        # its daemon, while GatewayPool records the outer login transport.  A
        # second RPC here would duplicate both signals after every detach.
        return
    try:
        ipc.request(PREVIEW_SOCKET, {
            'action': 'report', 'node': node, 'outcome': outcome,
            'reason': reason, 'source': source,
        }, 0.75)
    except Exception:
        pass


def _run_remote_user_command(node: str, remote_args: list[str] | None,
                             *, direct: bool = False) -> tuple[int, str, bool]:
    """Run SSH and retry rc=255 once while explicitly bypassing a bad mux."""
    if _GATEWAY_POOL is not None:
        return _GATEWAY_POOL.run_interactive(
            node, remote_args, direct=direct)

    def argv(use_direct: bool) -> list[str]:
        command = list(remote_args or ())
        if (len(command) == 4
                and command[:3] == ['tmux', 'attach', '-t']):
            # A dead-but-not-yet-timed-out SSH connection leaves a tmux client
            # behind.  Detaching it prevents its old window size and delayed
            # keystrokes from affecting the newly connected client.
            command = [
                'tmux', 'attach-session', '-d', '-t', command[3]]
        base = (['ssh'] + _get_ssh_args(
                    node, direct=use_direct, interactive=True)
                + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node])
        return base + command

    mode = 'direct SSH' if direct else 'SSH'
    print(f"\n[atmux] connecting to {node} via {mode}…", flush=True)
    print("[atmux] if the connection stalls: press Enter, then type ~. to disconnect.",
          flush=True)
    returncode, error = _run_user_command(argv(direct))
    if returncode == 255 and not direct:
        _report_network_event(
            node, 'failure', 'ControlMaster interactive attach failed',
            'attach-mux')
        print("\n[atmux] low-latency SSH path failed; retrying once with a fresh connection…",
              flush=True)
        returncode, error = _run_user_command(argv(True))
        direct = True
    return returncode, error, direct


def _publish_remote_command_result(node: str, returncode: int, error: str,
                                   source: str) -> None:
    """Feed an intentional SSH result back into the daemon's shared circuit."""
    if returncode == 255:
        _report_network_event(
            node, 'failure', error or 'interactive SSH failed', source)
    elif returncode != 127 or not error:
        # Remote command exit codes still prove that SSH transported it. A
        # local exec failure is the one case which says nothing about network.
        _report_network_event(node, 'success', '', source)


def _set_keepalive_enabled(path: str, job_name: str, enabled: bool,
                           command: str = '', workdir: str = '', **kwargs) -> bool:
    """Write keep-alive intent on the host which owns Slurm state."""
    # Which cluster's registry: a JobID only means anything to the Slurm that
    # issued it, so the row's node decides, not whichever gateway is active.
    node = kwargs.pop('node', None)
    if _GATEWAY_POOL is not None:
        return _GATEWAY_POOL.set_keepalive(
            job_name, enabled, command, workdir,
            job_id=kwargs.get('job_id'), entry_id=kwargs.get('entry_id'),
            node=node)
    return keepalive.set_entry_enabled(
        path, job_name, enabled, command, workdir, **kwargs)


def _read_json_dict_checked(path: str, max_bytes: int) -> tuple[bool, dict]:
    """Read a JSON object and report whether the value was trustworthy.

    Callers with a last-good value need to distinguish a legitimate empty
    object from a transient missing, corrupt, or unreadable file. Collapsing
    both to ``{}`` made one bad refresh erase the table and its selection.
    """
    fd = None
    try:
        flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
        fd = os.open(path, flags)
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_size > max_bytes):
            return False, {}
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > max_bytes:
            return False, {}
        value = json.loads(chunks)
        if isinstance(value, dict):
            return True, value
    except (OSError, UnicodeError, ValueError, TypeError):
        return False, {}
    finally:
        if fd is not None:
            os.close(fd)
    return False, {}


def _read_state_checked() -> tuple[bool, dict]:
    if _GATEWAY_POOL is not None:
        return _GATEWAY_POOL.fetch_state()
    return _read_json_dict_checked(STATE_FILE, _STATE_FILE_LIMIT)


_WARNED_JOBS_FILE = os.path.join(paths.BASE, 'warned-jobs.json')
_WARNED_JOBS_LIMIT = 4096


def _load_warned_jobs() -> set:
    """JobIDs already announced, so a TUI restart does not re-announce them."""
    try:
        with open(_WARNED_JOBS_FILE, encoding='utf-8') as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return set()
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value[:_WARNED_JOBS_LIMIT]
            if isinstance(item, (str, int)) and not isinstance(item, bool)}


def _save_warned_jobs(job_ids) -> None:
    """Persist announced JobIDs. Best-effort: never break a refresh."""
    try:
        payload = json.dumps(sorted(job_ids)[:_WARNED_JOBS_LIMIT])
        tmp = f'{_WARNED_JOBS_FILE}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            handle.write(payload)
        os.replace(tmp, _WARNED_JOBS_FILE)
    except (OSError, ValueError, TypeError):
        try:
            os.unlink(tmp)
        except (OSError, NameError, UnboundLocalError):
            pass


def read_state() -> dict:
    """Read the daemon state file.  Returns {} on any error."""
    return _read_state_checked()[1]


def _read_snapshots_checked() -> tuple[bool, dict]:
    if _GATEWAY_POOL is not None:
        return _GATEWAY_POOL.read_snapshots()
    return _read_json_dict_checked(SNAPSHOT_FILE, _SNAPSHOT_FILE_LIMIT)


def read_snapshots() -> dict:
    """Read the persistent preview snapshot cache. Returns {} on any error."""
    return _read_snapshots_checked()[1]


class WarmHandoffStatus(enum.Enum):
    ATTACHED = 'attached'
    SHELL_COMPLETED = 'shell-completed'
    UNAVAILABLE = 'unavailable'
    READY_TIMEOUT = 'ready-timeout'
    TRANSPORT_LOST = 'transport-lost'
    REMOTE_REJECTED = 'remote-rejected'
    CANCELLED = 'cancelled'


@dataclass(frozen=True)
class WarmHandoffResult:
    status: WarmHandoffStatus
    detail: str = ''

    def __bool__(self) -> bool:
        return self.status in {
            WarmHandoffStatus.ATTACHED,
            WarmHandoffStatus.SHELL_COMPLETED,
        }

    @property
    def bypass_master(self) -> bool:
        return self.status in {
            WarmHandoffStatus.READY_TIMEOUT,
            WarmHandoffStatus.TRANSPORT_LOST,
        }


class WarmSlavePool:
    """Maintains an idle interactive ssh slave per remote node, so that
    `tmux attach` is near-instant.

    The slow part of an attach over a high-load compute node is not the SSH
    handshake itself (the master takes care of that) — it's the remote sshd
    forking a fresh shell process that has to wait its turn on a busy CPU.
    By keeping a long-lived `ssh -tt` running in the background, that fork
    has already happened. When the user presses Enter we send ``tmux attach``
    into that shell and proxy bytes between the local terminal and the pty.
    A private completion marker lets us stop the proxy after detach without
    exiting ssh, so the same fully-started remote shell can serve later
    attaches too.

    Slaves run on an explicitly allocated pty, so we hold the pty master fd in
    this process and can read/write to the remote shell.  We intentionally use
    ``Popen`` rather than ``pty.fork``: warm-up runs in a worker thread, and
    executing Python after fork in a multithreaded Textual process can deadlock
    on an inherited interpreter/libc lock.  Failure to spawn or attach silently
    falls back to the cold subprocess-based path.
    """

    def __init__(self) -> None:
        # node -> (pid, master_fd)
        self._slaves: dict = {}
        # pid -> Popen.  Keeping the handle gives liveness/reaping one owner and
        # eliminates PID-reuse races from raw kill/waitpid pairs.
        self._procs: dict = {}
        # pid -> marker emitted by the first command queued into a new shell.
        # Popen being alive only proves that the local ssh client exists; this
        # marker proves that sshd, the remote PTY and the login shell are all
        # ready before we send tmux into the channel.
        self._ready_markers: dict[int, bytes] = {}
        # pid -> exact registry record created by ssh_child.  The daemon uses
        # it to reap the child if this frontend is SIGKILLed.
        self._registry_paths: dict[int, str] = {}
        # node -> pid while a slave is handed to the synchronous terminal
        # proxy.  A 5-second UI refresh must not mistake that temporary absence
        # from _slaves for a missing channel and spawn a competing ssh session.
        self._in_use: dict[str, int] = {}
        # Last node set requested by warm_all().  If a Slurm allocation leaves
        # the view while its channel is in use, don't return that channel to the
        # idle pool after detach.
        self._wanted_nodes: set[str] | None = None
        # Nodes whose Popen constructor is currently outside the lock.  This
        # preserves idempotence without letting a stuck executable lookup hold
        # _lock and freeze shutdown forever.
        self._spawning: set = set()
        # Serialize spawn/cleanup so concurrent warm() calls (e.g. from
        # back-to-back _refresh_table workers) can't both fork for the
        # same node, leaking one slave.
        self._lock = threading.Lock()
        # Set by shutdown() so a warm worker still in flight can't spawn a
        # new slave after we've torn everything down (would orphan an ssh).
        self._closed = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def warm(self, node: str) -> None:
        """Idempotently spawn a warm slave for node, if the master socket
        is up and we don't already have a live one."""
        if node == 'localhost' or not _valid_node(node):
            return
        ctl = _ctl_path(node)
        if not os.path.exists(ctl):
            return
        with self._lock:
            if self._closed:
                return
            if node in self._in_use:
                return
            if self._still_alive_locked(node):
                return
            if node in self._spawning:
                return
            self._spawning.add(node)

        master_fd = slave_fd = None
        proc = None
        token = uuid.uuid4().hex
        ready_marker = f'\x1eAUTOTMUX_READY_{token}\x1f'.encode('ascii')
        ready_command = (
            f"printf '\\036AUTOTMUX_READY_{token}\\037'\n".encode('ascii'))
        try:
            warm_registry.ensure_directory(WARM_DIR)
            master_fd, slave_fd = pty.openpty()
            # Set a valid size before ssh starts.  Waiting until _proxy() is
            # too late: the remote tmux client may already have attached using
            # the pty's initial 0x0/default geometry.
            _copy_terminal_winsize(slave_fd)
            ssh_argv = [
                'ssh',
                *_get_ssh_args(node),
                '-o', 'StrictHostKeyChecking=accept-new',
                '-tt', node,
            ]
            argv = _warm_helper_argv(node, ctl, ssh_argv)
            try:
                proc = subprocess.Popen(
                    argv, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, start_new_session=True,
                )
            finally:
                os.close(slave_fd)
                slave_fd = None
            # parent — make the master fd close-on-exec so child processes don't
            # accidentally inherit it.
            try:
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFD)
                fcntl.fcntl(master_fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
            except Exception:
                pass
            # Queue a binary-framed readiness probe immediately.  The textual
            # command may be echoed by the local/remote PTY, but it cannot be
            # confused with the actual control-byte marker produced by printf.
            if not self._write_all(master_fd, ready_command):
                raise OSError('could not queue warm-shell readiness probe')
        except Exception:
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if proc is not None:
                if self._terminate_proc(proc, timeout=0.2):
                    warm_registry.remove_record(
                        warm_registry.registry_path(WARM_DIR, proc.pid))
            proc = None

        discard = False
        with self._lock:
            self._spawning.discard(node)
            if proc is None:
                return
            if (self._closed or node in self._slaves
                    or not os.path.exists(ctl)):
                discard = True
            else:
                self._slaves[node] = (proc.pid, master_fd)
                self._procs[proc.pid] = proc
                self._ready_markers[proc.pid] = ready_marker
                self._registry_paths[proc.pid] = (
                    warm_registry.registry_path(WARM_DIR, proc.pid))
        if discard:
            try:
                os.close(master_fd)
            except OSError:
                pass
            if self._terminate_proc(proc):
                warm_registry.remove_record(
                    warm_registry.registry_path(WARM_DIR, proc.pid))

    def warm_all(self, nodes) -> None:
        """Spawn warm slaves for `nodes` and tear down slaves for any node
        that's no longer in the set — so departing slurm allocations don't
        leave orphan ssh processes for hours."""
        wanted = set(nodes)
        with self._lock:
            self._wanted_nodes = wanted
            current = set(self._slaves)
        # Kill slaves for nodes that left the view.
        self._cleanup_many(current - wanted)
        # Spawn missing ones.
        for n in wanted:
            self.warm(n)

    def shutdown(self) -> None:
        """Tear down every warm slave. Called from on_unmount.

        Sets _closed under the lock (so an in-flight warm() either finished
        before us — and is in the snapshot we clean up — or will see _closed
        and bail), then cleans up the snapshot."""
        with self._lock:
            self._closed = True
            nodes = list(self._slaves)
        self._cleanup_many(nodes)

    # ── attach path ─────────────────────────────────────────────────────────

    # A shell proxy that returns faster than this almost certainly means the warm
    # slave was stale (local ssh alive, but its channel / remote shell had
    # quietly died) — not a real attach the user interacted with. Treat it as
    # a failure so the caller falls back to a fresh cold attach instead of
    # popping the user straight back out.
    _MIN_PROXY_SECONDS = 0.5
    _READY_TIMEOUT = 15.0

    @staticmethod
    def _write_all(fd: int, data: bytes) -> bool:
        """Write a complete terminal chunk; short PTY writes lose keystrokes."""
        remaining = memoryview(data)
        while remaining:
            try:
                written = os.write(fd, remaining)
            except InterruptedError:
                continue
            except OSError:
                return False
            if written <= 0:
                return False
            remaining = remaining[written:]
        return True

    def attach(self, node: str, session: str) -> WarmHandoffResult:
        """Attach through a warm channel and preserve the failure category."""
        token = uuid.uuid4().hex
        ok_marker = f'\x1eAUTOTMUX_ATTACH_{token}_OK\x1f'.encode('ascii')
        fail_marker = f'\x1eAUTOTMUX_ATTACH_{token}_FAIL\x1f'.encode('ascii')
        command = (
            f"tmux attach -t {shlex.quote(session)}; "
            f"if [ \"$?\" -eq 0 ]; then "
            f"printf '\\036AUTOTMUX_ATTACH_{token}_OK\\037'; "
            f"else printf '\\036AUTOTMUX_ATTACH_{token}_FAIL\\037'; fi\n"
        ).encode()
        return self._handoff(
            node, command, end_markers={ok_marker: 'ok', fail_marker: 'fail'})

    def shell(self, node: str) -> WarmHandoffResult:
        """Hand an already-warm remote login shell to the user's terminal."""
        # _drain() consumes the original prompt. A blank command makes the
        # remote shell paint a fresh one after the proxy takes over.
        return self._handoff(node, b'\n')

    def is_starting(self, node: str) -> bool:
        """Whether the idle channel still needs its first readiness marker."""
        with self._lock:
            slave = self._slaves.get(node)
            return bool(slave and slave[0] in self._ready_markers)

    def _handoff(self, node: str, command: bytes,
                 end_markers: dict[bytes, str] | None = None
                 ) -> WarmHandoffResult:
        """Consume one warm slave, send its handoff command, and proxy it."""
        slave = self._take(node)
        if not slave:
            return WarmHandoffResult(WarmHandoffStatus.UNAVAILABLE)
        pid, master_fd = slave
        outcome = WarmHandoffResult(WarmHandoffStatus.TRANSPORT_LOST)
        return_to_pool = False
        try:
            with self._lock:
                ready_marker = self._ready_markers.pop(pid, None)
            if ready_marker is not None:
                marker_state, detail = self._await_marker_state(
                    master_fd, ready_marker, self._READY_TIMEOUT)
                if marker_state != 'ready':
                    status = {
                        'timeout': WarmHandoffStatus.READY_TIMEOUT,
                        'cancelled': WarmHandoffStatus.CANCELLED,
                    }.get(marker_state, WarmHandoffStatus.TRANSPORT_LOST)
                    return WarmHandoffResult(status, detail)
            # Drain whatever the remote bash already printed (welcome
            # banner, prompt, etc.) so the user sees a clean tmux paint.
            self._drain(master_fd)
            # The terminal may have been resized since this slave was warmed.
            # Synchronize immediately before the remote shell executes tmux so
            # that the new tmux client is born with the correct dimensions.
            _copy_terminal_winsize(master_fd)
            if not self._write_all(master_fd, command):
                return WarmHandoffResult(
                    WarmHandoffStatus.TRANSPORT_LOST,
                    'could not write to warm SSH channel')
            start = time.monotonic()
            result = self._proxy(master_fd, pid, end_markers)
            elapsed = time.monotonic() - start
            if end_markers is not None:
                # Seeing either framed result proves that the reusable shell is
                # alive and back at command level.  Only the OK marker means a
                # real tmux attach succeeded; a FAIL marker keeps the healthy
                # shell warm while asking the caller to use its cold fallback.
                return_to_pool = result in end_markers.values()
                if result == 'ok':
                    return WarmHandoffResult(WarmHandoffStatus.ATTACHED)
                if result == 'fail':
                    return WarmHandoffResult(
                        WarmHandoffStatus.REMOTE_REJECTED,
                        'tmux session no longer exists')
                return WarmHandoffResult(
                    WarmHandoffStatus.TRANSPORT_LOST,
                    'warm SSH ended before the attach result arrived')
            returncode = None
            with self._lock:
                proc = self._procs.get(pid)
            # A successful tmux detach exits 0 even when the user detaches very
            # quickly. Duration alone treated that as a dead warm channel and
            # immediately cold-attached them a second time. Give a just-ended
            # child a tiny reap window and trust its clean exit status.
            if proc is not None and elapsed < self._MIN_PROXY_SECONDS:
                try:
                    returncode = proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    returncode = None
            if returncode == 0 or elapsed >= self._MIN_PROXY_SECONDS:
                outcome = WarmHandoffResult(
                    WarmHandoffStatus.SHELL_COMPLETED)
            else:
                outcome = WarmHandoffResult(
                    WarmHandoffStatus.TRANSPORT_LOST,
                    'warm SSH shell ended unexpectedly')
        finally:
            returned = self._finish_handoff(
                node, pid, master_fd, reusable=return_to_pool)
            if not returned:
                self._reap_child(pid)
                try:
                    os.close(master_fd)
                except OSError:
                    pass
        return outcome

    @staticmethod
    def _await_marker_state(master_fd: int, marker: bytes,
                            timeout: float) -> tuple[str, str]:
        """Wait for readiness while retaining a bounded SSH diagnostic."""
        deadline = time.monotonic() + timeout
        pending = bytearray()
        diagnostic = bytearray()
        keep = max(0, len(marker) - 1)
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select(
                    [master_fd], [], [],
                    max(0.0, min(0.1, deadline - time.monotonic())))
                if master_fd not in readable:
                    continue
                try:
                    data = os.read(master_fd, _PROXY_IO_CHUNK)
                except InterruptedError:
                    continue
                except OSError as error:
                    return 'error', str(error)
                if not data:
                    return 'eof', diagnostic.decode(errors='replace')[-500:]
                if len(diagnostic) < 4096:
                    diagnostic.extend(data[:4096 - len(diagnostic)])
                pending.extend(data)
                if marker in pending:
                    return 'ready', ''
                if len(pending) > keep:
                    del pending[:len(pending) - keep]
        except KeyboardInterrupt:
            return 'cancelled', 'cancelled by user'
        except (OSError, ValueError) as error:
            return 'error', str(error)
        detail = ' '.join(diagnostic.decode(errors='replace').split())[-500:]
        return 'timeout', detail or 'warm SSH readiness timed out'

    @staticmethod
    def _await_marker(master_fd: int, marker: bytes, timeout: float) -> bool:
        """Compatibility boolean wrapper for readiness tests/callers."""
        return WarmSlavePool._await_marker_state(
            master_fd, marker, timeout)[0] == 'ready'

    @staticmethod
    def _filter_marker_chunk(pending: bytes, data: bytes,
                             markers: dict[bytes, str]):
        """Split relay output at a possibly fragmented completion marker.

        Returns ``(safe_output, retained_tail, result)``.  Keeping at most one
        marker-length tail is what lets a marker span two PTY reads without
        delaying the bulk of a large tmux redraw.
        """
        combined = pending + data
        match = None
        for marker, result in markers.items():
            offset = combined.find(marker)
            if offset >= 0 and (match is None or offset < match[0]):
                match = (offset, result)
        if match is not None:
            offset, result = match
            return combined[:offset], b'', result
        keep = max((len(marker) for marker in markers), default=1) - 1
        safe = max(0, len(combined) - keep)
        return combined[:safe], combined[safe:], None

    def _finish_handoff(self, node: str, pid: int, master_fd: int,
                        reusable: bool) -> bool:
        """Release an in-use slot and, when safe, return it to the idle pool."""
        with self._lock:
            if self._in_use.get(node) == pid:
                self._in_use.pop(node, None)
            proc = self._procs.get(pid)
            wanted = (self._wanted_nodes is None
                      or node in self._wanted_nodes)
            if (reusable and not self._closed and wanted
                    and node not in self._slaves and proc is not None
                    and proc.poll() is None):
                self._slaves[node] = (pid, master_fd)
                return True
        return False

    @staticmethod
    def _terminate_proc(proc, timeout: float = 1.0) -> bool:
        """Bounded terminate/kill/reap; return whether the child is gone."""
        try:
            if proc.poll() is not None:
                return True
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
                return True
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                    return True
                except subprocess.TimeoutExpired:
                    # Keep one owner until the kernel eventually releases an
                    # uninterruptible child; otherwise it becomes a permanent
                    # zombie after this bounded cleanup drops the Popen.
                    lifecycle.defer_popen_reap(proc)
                    return False
        except (OSError, ChildProcessError):
            return True

    @staticmethod
    def _remove_registry_path(path: str | None) -> None:
        if path:
            warm_registry.remove_record(path)

    def _reap_child(self, pid: int) -> None:
        """Terminate and reap the unique Popen owner for ``pid``."""
        with self._lock:
            self._ready_markers.pop(pid, None)
            proc = self._procs.pop(pid, None)
            registry_path = self._registry_paths.pop(pid, None)
        if proc is not None:
            if self._terminate_proc(proc):
                self._remove_registry_path(registry_path)
            return
        # Compatibility fallback for an externally-injected/raw child.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            self._remove_registry_path(registry_path)
            return
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    self._remove_registry_path(registry_path)
                    return
            except OSError:
                self._remove_registry_path(registry_path)
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            self._remove_registry_path(registry_path)
            return
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    self._remove_registry_path(registry_path)
                    return
            except OSError:
                self._remove_registry_path(registry_path)
                return
            time.sleep(0.05)

    # ── internals ───────────────────────────────────────────────────────────

    def _still_alive(self, node: str) -> bool:
        with self._lock:
            return self._still_alive_locked(node)

    def _still_alive_locked(self, node: str) -> bool:
        """Caller must hold self._lock."""
        slave = self._slaves.get(node)
        if not slave:
            return False
        pid, fd = slave
        proc = self._procs.get(pid)
        if proc is not None:
            if proc.poll() is None:
                return True
            self._procs.pop(pid, None)
        else:
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid == 0:
                    return True
            except OSError:
                pass
        # Process gone — drop it AND close its pty master fd. Forgetting the
        # close here leaks one fd per silently-dead slave until EMFILE.
        self._slaves.pop(node, None)
        self._ready_markers.pop(pid, None)
        registry_path = self._registry_paths.pop(pid, None)
        try:
            os.close(fd)
        except OSError:
            pass
        self._remove_registry_path(registry_path)
        return False

    def _take(self, node: str):
        with self._lock:
            if not self._still_alive_locked(node):
                return None
            slave = self._slaves.pop(node, None)
            if slave is not None:
                self._in_use[node] = slave[0]
            return slave

    def _cleanup(self, node: str) -> None:
        self._cleanup_many([node])

    def _cleanup_many(self, nodes) -> None:
        """Terminate a set of slaves within one shared deadline.

        Sequential two-second reaps made quitting take up to 24 seconds for the
        normal 12-slave pool.  Signal all children first, then reap them as a
        group so shutdown remains bounded independent of pool size.
        """
        items = []
        with self._lock:
            for node in list(nodes):
                slave = self._slaves.pop(node, None)
                if slave:
                    pid, fd = slave
                    self._ready_markers.pop(pid, None)
                    items.append((
                        pid, fd, self._procs.pop(pid, None),
                        self._registry_paths.pop(pid, None)))
        if not items:
            return
        pending = []
        for pid, fd, proc, _registry_path in items:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                if proc is not None:
                    if proc.poll() is None:
                        proc.terminate()
                        pending.append((pid, proc))
                else:
                    os.kill(pid, signal.SIGTERM)
                    pending.append((pid, None))
            except (OSError, ChildProcessError):
                pass

        def still_running(item):
            pid, proc = item
            if proc is not None:
                return proc.poll() is None
            try:
                return os.waitpid(pid, os.WNOHANG)[0] == 0
            except OSError:
                return False

        deadline = time.monotonic() + 1.0
        while pending and time.monotonic() < deadline:
            pending = [item for item in pending if still_running(item)]
            if pending:
                time.sleep(0.05)
        for pid, proc in pending:
            try:
                if proc is not None:
                    proc.kill()
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 1.0
        while pending and time.monotonic() < deadline:
            pending = [item for item in pending if still_running(item)]
            if pending:
                time.sleep(0.05)
        for _pid, proc in pending:
            if proc is not None:
                lifecycle.defer_popen_reap(proc)
        still_running_pids = {pid for pid, _proc in pending}
        for pid, _fd, _proc, registry_path in items:
            if pid not in still_running_pids:
                self._remove_registry_path(registry_path)

    @staticmethod
    def _drain(master_fd: int) -> None:
        """Discard a leftover prompt without adding visible attach latency.

        Readiness framing already consumed the login banner. This short cap is
        only for a prompt that arrived just after that marker; a chatty prompt
        must not hold an otherwise-ready attach for seconds.
        """
        deadline = time.monotonic() + _WARM_DRAIN_MAX_SECONDS
        try:
            while time.monotonic() < deadline:
                r, _, _ = select.select(
                    [master_fd], [], [], _WARM_DRAIN_IDLE_GRACE)
                if master_fd not in r:
                    return
                try:
                    if not os.read(master_fd, 8192):
                        return
                except OSError:
                    return
        except (OSError, ValueError):
            return

    def _proxy(self, master_fd: int, child_pid: int,
               end_markers: dict[bytes, str] | None = None) -> str | None:
        """Bridge fd 0/1 to/from master_fd until the child exits.

        Mirrors what `ssh` itself does between user terminal and remote pty,
        but we're in front of the ssh client this time. Warm ssh processes use
        ``start_new_session=True`` and do not own the pre-opened slave PTY as a
        controlling terminal. Consequently, TIOCSWINSZ updates its dimensions
        but the kernel does not wake ssh; explicitly forwarding SIGWINCH is
        required for ssh to send a window-change request to the remote tmux.

        Both directions are buffered and non-blocking.  A large pane redraw can
        fill the local terminal's output queue; synchronously writing the whole
        redraw used to stop us from reading fd 0, making keystrokes appear lost
        until output drained.  Bounded buffers apply backpressure to the noisy
        direction while the input direction keeps making progress.
        """
        try:
            old_attr = termios.tcgetattr(0)
        except (OSError, termios.error):
            old_attr = None

        resize_pending = True

        def on_winch(_sig=None, _frame=None):
            # Python signal handlers may interrupt code while self._lock is
            # held. Only set a flag here; do the ioctl/Popen work from the
            # ordinary proxy loop to avoid a re-entrant lock deadlock.
            nonlocal resize_pending
            resize_pending = True

        def forward_resize():
            nonlocal resize_pending
            if not resize_pending:
                return
            # Clear first so a second signal arriving during the copy remains
            # pending for the next iteration.
            resize_pending = False
            if not _copy_terminal_winsize(master_fd):
                return
            with self._lock:
                proc = self._procs.get(child_pid)
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

        old_winch = None
        old_flags: dict[int, int] = {}
        to_remote = bytearray()
        to_local = bytearray()
        marker_tail = bytearray()
        matched_result = None
        local_input_open = True
        remote_open = True
        try:
            old_winch = signal.signal(signal.SIGWINCH, on_winch)
            forward_resize()
            if old_attr is not None:
                tty.setraw(0)
            # select() readiness followed by a blocking write still has a race
            # (and a terminal driver may accept only part of a large chunk).
            # Preserve and restore each descriptor's exact status flags.
            # Snapshot every fd before changing any of them. stdin/stdout are
            # often dup()s of the same terminal open-file description; doing
            # GETFL/SETFL one fd at a time would record O_NONBLOCK as fd 1's
            # "original" after fd 0 had already enabled it, then mistakenly
            # leave the surrounding shell non-blocking during restoration.
            for fd in dict.fromkeys((0, 1, master_fd)):
                try:
                    old_flags[fd] = fcntl.fcntl(fd, fcntl.F_GETFL)
                except OSError:
                    pass
            for fd, flags in old_flags.items():
                try:
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                except OSError:
                    pass
            while True:
                forward_resize()
                read_fds = []
                if (local_input_open and remote_open
                        and len(to_remote) < _PROXY_INPUT_BUFFER_LIMIT):
                    read_fds.append(0)
                if (remote_open
                        and len(to_local) < _PROXY_OUTPUT_BUFFER_LIMIT):
                    read_fds.append(master_fd)
                write_fds = []
                if remote_open and to_remote:
                    write_fds.append(master_fd)
                if to_local:
                    write_fds.append(1)
                if not read_fds and not write_fds:
                    break
                try:
                    readable, writable, _ = select.select(
                        read_fds, write_fds, [], _PROXY_IDLE_TIMEOUT)
                except (OSError, ValueError):
                    break

                failed = False
                # Input gets first turn on every wakeup.  It is intentionally
                # handled before a readable flood from the remote pane.
                if 0 in readable:
                    try:
                        room = _PROXY_INPUT_BUFFER_LIMIT - len(to_remote)
                        data = os.read(0, min(_PROXY_IO_CHUNK, room))
                    except (BlockingIOError, InterruptedError):
                        data = None
                    except OSError:
                        failed = True
                        data = None
                    if data == b'':
                        local_input_open = False
                        failed = True
                    elif data:
                        to_remote.extend(data)

                if master_fd in writable and to_remote:
                    try:
                        written = os.write(
                            master_fd, bytes(to_remote[:_PROXY_IO_CHUNK]))
                    except (BlockingIOError, InterruptedError):
                        written = 0
                    except OSError:
                        failed = True
                        written = 0
                    if written > 0:
                        del to_remote[:written]

                if master_fd in readable:
                    try:
                        room = _PROXY_OUTPUT_BUFFER_LIMIT - len(to_local)
                        data = os.read(master_fd, min(_PROXY_IO_CHUNK, room))
                    except (BlockingIOError, InterruptedError):
                        data = None
                    except OSError:
                        # Linux PTY masters commonly report EIO rather than an
                        # empty read after the final slave closes.
                        data = b''
                    if data == b'':
                        remote_open = False
                        local_input_open = False
                        to_remote.clear()
                        if marker_tail:
                            to_local.extend(marker_tail)
                            marker_tail.clear()
                    elif data:
                        if end_markers:
                            output, tail, result = self._filter_marker_chunk(
                                bytes(marker_tail), data, end_markers)
                            to_local.extend(output)
                            marker_tail[:] = tail
                            if result is not None:
                                matched_result = result
                                marker_tail.clear()
                                # Bytes after the marker are the reusable
                                # shell's prompt. Leave them out of the tmux
                                # screen and stop consuming user input.
                                remote_open = False
                                local_input_open = False
                                to_remote.clear()
                        else:
                            to_local.extend(data)

                if 1 in writable and to_local:
                    try:
                        written = os.write(
                            1, bytes(to_local[:_PROXY_IO_CHUNK]))
                    except (BlockingIOError, InterruptedError):
                        written = 0
                    except OSError:
                        failed = True
                        written = 0
                    if written > 0:
                        del to_local[:written]

                if failed:
                    break
                if not remote_open and not to_local:
                    break
                # Popen owns the reap, so polling cannot race a raw kill of a
                # recycled PID.  This also exits when a broken pty fails to
                # deliver EOF after ssh has already died.
                with self._lock:
                    proc = self._procs.get(child_pid)
                if (proc is not None and proc.poll() is not None
                        and not readable and not to_local):
                    break
        finally:
            if old_winch is not None:
                try:
                    signal.signal(signal.SIGWINCH, old_winch)
                except Exception:
                    pass
            for fd, flags in old_flags.items():
                try:
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags)
                except OSError:
                    pass
            if old_attr is not None:
                try:
                    # Never wait for a stalled terminal output queue just to
                    # restore local input mode after detach/failure.
                    termios.tcsetattr(0, termios.TCSANOW, old_attr)
                except Exception:
                    pass
        return matched_result






# A pane nobody has touched for a while is usually a finished run or a shell
# waiting on a prompt.  Surfacing that costs nothing -- tmux already tracks the
# last activity per session -- and saves opening each one to check.
# ── colour, as tokens rather than as names ──────────────────────────────
# It was `'yellow'` and `'red'`: sixteen-colour names, so what "yellow" meant
# was whatever the reader's theme said, and two apps side by side agreed on
# nothing. The shape the current writing settles on is palette -> semantic
# token -> style, with sixteen colours as the floor and truecolour as an
# enhancement layer rather than a replacement.
#
# So each token carries both, and which one is used depends on what the
# terminal admits to. Over SSH into a cluster that is often the sixteen, and
# the design has to be readable there -- if stripping the colour breaks it,
# the design was leaning on decoration.
_TRUECOLOR = str(os.environ.get('COLORTERM', '')).lower() in {
    'truecolor', '24bit'}

_TOKENS = {
    #            truecolour   sixteen
    'ok':       ('#3fb950',   'green'),
    'warn':     ('#d29922',   'yellow'),
    'danger':   ('#f85149',   'red'),
    'quiet':    ('#8b8b95',   'bright_black'),
    'accent':   ('#46d3a4',   'cyan'),
}


def tone(token: str) -> str:
    """The style for a semantic token, in whatever this terminal has."""
    pair = _TOKENS.get(token)
    if not pair:
        return ''
    return pair[0] if _TRUECOLOR else pair[1]


# Eighths, so a bar is precise to a fraction of a cell rather than to a whole
# one. Six cells of these resolve forty-eight steps, which is finer than a
# one-minute load average deserves and costs six columns.
_EIGHTHS = ' ▏▎▍▌▋▊▉█'


def bar(fraction: float, width: int = 6) -> str:
    """A proportion, drawn.

    `12.60/96` is a division the reader performs; a bar is the answer. This
    is the thing the current crop of terminal apps is actually known for --
    btop is the one everybody names, and what it does is draw the numbers.
    """
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
        return ' ' * width
    if not math.isfinite(fraction):
        return ' ' * width
    fraction = max(0.0, min(1.0, float(fraction)))
    eighths = int(round(fraction * width * 8))
    full, rest = divmod(eighths, 8)
    out = '█' * full + (_EIGHTHS[rest] if rest else '')
    return out.ljust(width)[:width]


def load_fraction(load, cpus):
    """How much of the machine is in use, or None if it cannot be said."""
    try:
        load = float(str(load).strip())
        cpus = float(str(cpus).strip())
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(load) and math.isfinite(cpus)) or cpus <= 0:
        return None
    return max(0.0, load / cpus)


_IDLE_STYLES = {'idle': tone('warn'), 'stale': tone('danger')}


def _apply_idle_thresholds() -> None:
    """Adopt the configured idle thresholds. The model owns them now."""
    model.apply_idle_thresholds()










def _literal_cell(value) -> rich.text.Text:
    """A DataTable renderable that never interprets user text as markup."""
    return rich.text.Text(str(value))


# A healthy row says "Active" and a session-less one says "No sessions" --
# both already visible from the session name or the <shell> placeholder.
_QUIET_STATUSES = ('Active', 'No sessions')


def _status_text(status) -> str:
    """STATUS with the uneventful baseline removed.

    Repeating "Active" down every row spends the column on a constant and
    buries the rows that do have something to report. Anything that is not
    the baseline -- OFFLINE, DEGRADED, an escape-time or network warning --
    is kept verbatim.
    """
    text = str(status).strip()
    if text in _QUIET_STATUSES:
        return ''
    for quiet in _QUIET_STATUSES:
        prefix = f'{quiet} · '
        if text.startswith(prefix):
            # Keep the warning, drop the baseline it was appended to.
            return text[len(prefix):]
    return text




def _idle_cell(marker) -> rich.text.Text:
    """The leading IDLE cell: a dot coloured by how long a session has been
    quiet, plus its age. Only the dot is styled; the rest stays literal."""
    text = str(marker)
    cell = rich.text.Text(text)
    if text.startswith(_IDLE_DOT):
        tier = 'stale' if _looks_stale(text) else 'idle'
        cell.stylize(_IDLE_STYLES[tier], 0, len(_IDLE_DOT))
    return cell




# ── one column, composed by hand ────────────────────────────────────────
# The table was six columns, and a grid is the wrong shape for rows that
# are meant to read as sentences. Every column was shared by everything in
# it, so a band heading had to live in one of them -- it ended up as a `──`
# stub stranded in IDLE with its title starting one column in, reading as a
# value rather than as a heading -- and one folded 60-character line set the
# width of NODE for every row in the table.
#
# One column, padded here, costs the alignment that a grid gives for free and
# buys back the thing the design needs: a line whose last field takes whatever
# width is left, which is where the output goes.
_TAIL_MIN = 24


def _row_widths(rows, total: int) -> tuple:
    """How wide the fixed fields are, from the rows actually present.

    Measured per rebuild rather than fixed, because these are names and a
    fixed guess is either padding or truncation on somebody's cluster.
    """
    lead = max([6] + [len(_idle_marker_text(r)) for r in rows])
    name = max([7] + [len(_session_cell(r[1], r[2])) for r in rows])
    where = max([4] + [len(_node_label(r[0])) for r in rows])
    # The output takes what is left, and the names give way before it does:
    # a truncated session name is still recognisable, and a truncated
    # traceback is not.
    spare = total - (lead + name + where + 6)
    if spare < _TAIL_MIN:
        where = max(4, where - (_TAIL_MIN - spare))
    return lead, name, where


def _fit_node(label: str, width: int) -> str:
    """A machine name in `width` columns, keeping the end.

    Cut from the right, holygpu8a15104 / holygpu8a15304 / holygpu8a15602 all
    become `holygpu8a1` -- the part that is the same on every node in the
    cluster, and none of the part that says which one. Machine names
    distinguish at the end, so that is the end to keep.
    """
    if len(label) <= width:
        return label.ljust(width)
    return '…' + label[-(width - 1):] if width > 1 else label[-width:]


def _idle_marker_text(r) -> str:
    marker, _rest = _split_idle_marker(r[4])
    return str(marker)


def _compose_session(r, widths, tail: str, total: int) -> rich.text.Text:
    """One session, as a line.

    The name comes first now. It is what the reader navigates by -- the
    artefact that argued for this said so while leaving it in column three,
    behind a dot, a duration and a machine name.
    """
    lead, name, where = widths
    marker = _idle_marker_text(r)
    line = rich.text.Text()
    if marker.startswith(_IDLE_DOT):
        tier = 'stale' if _looks_stale(marker) else 'idle'
        line.append(_IDLE_DOT, style=_IDLE_STYLES[tier])
        line.append(marker[len(_IDLE_DOT):].ljust(lead - len(_IDLE_DOT)))
    else:
        line.append(' ' * lead)
    line.append('  ')
    if r[1] in (_OFFLINE_SESSION, _START_SHELL_SESSION):
        # For these the machine is the identity: there is no session to name,
        # and the band above already says what the row is -- printing
        # `<offline>` under a heading that reads "not reachable" is the
        # repetition the bands were meant to remove.
        line.append(_node_label(r[0]).ljust(name + where + 2)[:name + where + 2],
                    style='dim')
    else:
        line.append(_session_cell(r[1], r[2]).ljust(name)[:name], style='bold')
        line.append('  ')
        line.append(_fit_node(_node_label(r[0]), where), style='dim')
    line.append('  ')
    # What the session was doing, which is the question the row raises and
    # the one no column of measurements answers. Already collected: the
    # daemon snapshots every pane on a timer for the preview.
    # The two numbers that were dropped when this became one line, back as
    # shapes and only when they have something to say. Both were columns of
    # figures the reader had to do arithmetic on -- `12.60/96` is a division,
    # and a walltime three days out is a number nobody acts on.
    rail = _row_rail(r)
    room = max(0, total - (lead + name + where + 6) - rail.cell_len)
    if tail:
        line.append(tail[:room] if len(tail) <= room else tail[:room - 1] + '…')
    if rail.cell_len:
        line.append(' ' * max(1, room - _visible_len(tail, room)))
        line.append_text(rail)
    return line


def _visible_len(text: str, room: int) -> int:
    return min(len(text or ''), room)


# Under this much walltime the number stops being background and starts
# being the thing you act on. Above it, a session that ends in two days is
# not news and the column it would cost is better spent on the output.
_WALL_URGENT = 2 * 3600
_WALL_CRITICAL = 30 * 60
# And a machine is only worth remarking on when it is actually loaded.
_LOAD_BUSY = 0.75


def _row_rail(r) -> rich.text.Text:
    """The right-hand rail: what is abnormal about this row, if anything.

    Silence is the healthy state. A rail that always shows a load bar and a
    walltime is six columns of "everything is fine" on every row, which is
    the reading the bands already give for free.
    """
    rail = rich.text.Text()
    if r[1] in (_OFFLINE_SESSION, _START_SHELL_SESSION):
        # Neither number means anything here. An unreachable node's walltime
        # is whatever it last said before it went, and a login node runs at
        # five times its core count all day -- drawing that in red on every
        # one of them is a warning about the normal state of the machine.
        return rail
    seconds = keepalive.parse_time_left(str(r[3] or '').strip())
    if (seconds is not None and math.isfinite(seconds)
            and seconds <= _WALL_URGENT):
        style = tone('danger') if seconds <= _WALL_CRITICAL else tone('warn')
        rail.append(_time_left_label(r[3]).rjust(6), style=style)
    share = load_fraction(r[6], r[5])
    if share is not None and share >= _LOAD_BUSY:
        if rail.cell_len:
            rail.append(' ')
        rail.append(bar(share, 6),
                    style=tone('danger') if share >= 1.0 else tone('warn'))
    return rail


def _heading(title: str, note: str = '', key: str = '') -> rich.text.Text:
    """The one heading this app draws, wherever it draws one.

    There were three shapes of the same idea and no two agreed: the bands
    wrote `── just stopped 3`, the preview wrote
    `── node:session ─ live  [2: scroll] ──` with dashes at both ends, and
    the queue wrote `── ALL JOBS (squeue -l)  [j: switch · 3: scroll] ──
    4s old` -- caps, brackets, parentheses and a trailing rule, all in one
    line. Three panes that are peers should not each invent their own
    furniture.

    One rule at the front, the title, then what is true of it, then the key
    that acts on it. Nothing at the end: a trailing rule has to be measured
    against a width, and none of these three know theirs.
    """
    line = rich.text.Text('── ', style='dim')
    line.append(title, style='dim')
    if note:
        line.append('  ' + note, style='dim')
    if key:
        line.append('  ' + key, style='dim')
    return line


def _band_cells(title: str, count: int) -> tuple:
    """A band heading, as the six cells a DataTable row is made of.

    Dim and lower-case, so it reads as furniture rather than as another
    session -- the whole point is that the rows under it stand out, not that
    the heading does. The count goes in the heading because "just stopped 4"
    is the sentence somebody came to the screen for, and reading it should
    not require counting rows.
    """
    return _heading(title, str(count))

class ActionCommands(Provider):
    """Every binding this app has, by name.

    Twenty-one bindings and five in the footer means sixteen that work and
    are advertised nowhere but `?`. `?` is a list you read; this is a list
    you search -- and searching is what you want when you know the name of
    the thing and not the letter somebody assigned it.

    It also takes the pressure off the letters. Every new action was a
    scramble for a free key, which is how `j` came to mean the jobs panel
    and `k` the keep-alive.

    Fuzzy here, unlike the `/` filter: this searches a set you cannot see, so
    a near miss is a help. That filter narrows a list in front of you, where
    a match you cannot explain looks like a bug.
    """

    def _commands(self):
        app = self.app
        for binding in getattr(app, 'BINDINGS', ()):
            action = getattr(binding, 'action', '')
            description = getattr(binding, 'description', '')
            if not action or not description:
                continue
            key = getattr(binding, 'key', '')
            yield description, key, action

    async def discover(self):
        """What the palette lists before anything is typed."""
        for description, key, action in self._commands():
            yield DiscoveryHit(
                description,
                functools.partial(self.app.run_action, action),
                text=description,
                help=f'key: {key}' if key else None,
            )

    async def search(self, query: str):
        matcher = self.matcher(query)
        for description, key, action in self._commands():
            # The key is searchable too: somebody who half-remembers `k`
            # should find it by typing `k` as readily as by typing renew.
            score = max(matcher.match(description), matcher.match(key or ''))
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(description),
                    functools.partial(self.app.run_action, action),
                    text=description,
                    help=f'key: {key}' if key else None,
                )


class FilterInput(Input):
    """The filter box, with its own way out.

    Escape has to leave here and put the rows back, and an Input swallows it
    -- so the binding lives on the widget, where the focused widget's
    bindings win. It was briefly a priority binding on the app instead,
    which won everywhere including over the help screen's own escape: `?`
    opened and could not be closed. A key bound globally to solve a local
    problem takes the key away from everywhere else.
    """

    BINDINGS = [Binding('escape', 'leave_filter', 'Back to list', show=False)]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Closed, and therefore not a place the keyboard can be. Textual
        # focuses the first focusable widget, and this one is first in the
        # column -- so on startup it took the keyboard, which meant typing
        # went into the filter and the phone was published this widget's
        # editing keys (`Delete character left`, `Cut selected text`) in
        # place of the session actions. `display: none` was not enough:
        # focusability is its own question.
        self.can_focus = False

    def action_leave_filter(self) -> None:
        self.app._close_filter(keep=False)


class StatusLine(Static):
    """The top line, replacing Textual's stock Header.

    The stock one draws a name, a clock space and a subtitle -- so the widest
    line on the screen said `atmux` and then, most of the time, nothing. It
    also reads as a Textual app rather than as this one, which is what stock
    chrome always reads as.

    What belongs there is the answer to the question the screen exists for:
    is anything wrong. The bands below already sort by it; this counts them,
    so "four things stopped" arrives before any row is read.
    """

    DEFAULT_CSS = """
    StatusLine {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    """

    def show(self, counts, warning: str = '') -> None:
        line = rich.text.Text()
        line.append('atmux', style='bold')
        for title, count, token in counts:
            line.append('   ')
            line.append(f'{count} ', style=tone(token))
            line.append(title, style=tone('quiet'))
        if warning:
            line.append('   ')
            line.append(warning, style=tone(
                'danger' if '⚠' in warning else 'quiet'))
        self.update(line)


class ClickToAttachDataTable(DataTable):
    """A DataTable where a *single* mouse click selects the clicked row.

    Upstream Textual (8.x) only emits ``RowSelected`` when you click the
    cell already under the cursor — a first click on a *different* row just
    moves the cursor and is otherwise swallowed. With our single-click-to-
    attach UX that made clicks intermittently "do nothing": whether a click
    attached depended on it landing on the exact cell already under the
    cursor.

    Textual invokes *every* ``_on_click`` along the MRO, most-derived first,
    so we can't simply post the selection here (upstream's handler would
    then see the cursor already on the cell and post a *second* one). Instead
    we just pre-position the cursor onto the clicked cell; the upstream
    handler that runs next then treats it as a redundant click and emits
    exactly one ``RowSelected`` — on the very first click. Header / row-label
    clicks (row/column index -1) are left untouched for upstream to handle.
    """

    # Upstream binds enter as a hidden "Select". The focused widget's bindings
    # win, so an App-level entry for the same key never reaches the footer --
    # attaching, the whole point of the table, went undocumented. Re-declare it
    # with a name that says what it does.
    BINDINGS = [Binding("enter", "select_cursor", "Attach", show=True)]

    # ── band headings the cursor walks over ──────────────────────────────
    # The table draws its attention bands as rows, because a DataTable has no
    # other way to put a heading between rows. A heading is not somewhere the
    # cursor may rest: landing on one leaves `selected_session` pointing at
    # nothing, so attach, preview and `k` would all act on the row before it.
    #
    # Skipping is done here rather than by correcting afterwards in
    # RowHighlighted, because by then the cursor has already stopped and the
    # direction it was travelling is gone. Whoever owns the rows sets this.
    def is_heading(self, row: int) -> bool:
        check = getattr(self, 'heading_rows', None)
        return bool(check) and row in check

    def _step_over_headings(self, came_from: int, step: int) -> None:
        """Carry on past a heading, or go back to where the move started.

        Going back matters at the ends. The first row of the table is a
        heading, so `up` from the row below it lands on one with nothing
        above -- and leaving the cursor there is exactly the state this
        exists to prevent: no selection, and `k`/attach/preview all acting on
        nothing. Measured before the fix: seven presses of `up` all rested on
        row 0.
        """
        row = self.cursor_row
        while self.is_heading(row):
            row += step
            if not 0 <= row < self.row_count:
                self.move_cursor(row=came_from)
                return
        if row != self.cursor_row:
            self.move_cursor(row=row)

    def action_cursor_down(self) -> None:
        came_from = self.cursor_row
        super().action_cursor_down()
        self._step_over_headings(came_from, 1)

    def action_cursor_up(self) -> None:
        came_from = self.cursor_row
        super().action_cursor_up()
        # Up, and then up again: a heading belongs to what is *below* it, so
        # arriving on one from below means the reader wanted the band above.
        self._step_over_headings(came_from, -1)

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        row_index = meta["row"]
        column_index = meta["column"]
        if row_index < 0 or column_index < 0:
            return
        # A heading is not a row you can attach to, and this class exists to
        # make a single click attach. Swallowed rather than redirected: a
        # click that jumped somewhere the finger was not is worse than one
        # that did nothing.
        if self.is_heading(row_index):
            event.stop()
            return
        if self.show_cursor and self.cursor_type != "none":
            self.cursor_coordinate = Coordinate(row_index, column_index)


def _make_no_motion_driver(base_cls):
    """Subclass the platform driver to enable mouse CLICK tracking but NOT
    any-motion tracking (1003h).

    1003h ("any event mouse") reports an escape sequence for every mouse
    movement. Over a slow/remote SSH terminal that becomes a torrent of input
    that buries keystrokes — the classic "arrow keys feel dead / the TUI looks
    frozen" symptom. atmux only needs clicks (for attach), never hover/motion,
    so we drop 1003h while keeping 1000h/1006h.
    """
    class _NoMotionDriver(base_cls):
        def _enable_mouse_support(self) -> None:
            if not getattr(self, '_mouse', True):
                return
            write = self.write
            write("\x1b[?1000h")  # SET_VT200_MOUSE — button press/release
            write("\x1b[?1015h")  # urxvt extended coordinates
            write("\x1b[?1006h")  # SGR extended coordinates
            # Deliberately NOT 1003h (any-motion) — see docstring.
            self.flush()
    return _NoMotionDriver


class NoteScreen(ModalScreen[str | None]):
    """Ask what a session is for.

    Session names are chosen for typing, not for reading -- `tu_debug` and
    `tu_improve` say nothing about which run matters right now. The note fills
    the STATUS column, which is blank whenever a row is healthy, so it costs
    no width and occupies space that was otherwise dead.
    """

    BINDINGS = [
        Binding("escape", "cancel_note", "Cancel"),
    ]

    CSS = """
    NoteScreen {
        align: center middle;
        background: $background 60%;
    }
    #note_dialog {
        width: 64;
        max-width: 90%;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #note_title { text-style: bold; color: $accent; }
    #note_hint { color: $text-muted; margin-bottom: 1; }
    """

    def __init__(self, session: str, current: str, *,
                 prompt: str = 'what is this run for?',
                 hint: str = 'Enter saves · empty clears · Esc cancels') -> None:
        super().__init__()
        self._session = str(session)
        self._current = str(current or '')
        self._prompt = str(prompt)
        self._hint = str(hint)

    def compose(self) -> ComposeResult:
        with Vertical(id='note_dialog'):
            yield Static(self._session if ' ' in self._session
                         else f'Note for {self._session}', id='note_title')
            yield Static(self._hint, id='note_hint')
            yield Input(value=self._current, placeholder=self._prompt,
                        max_length=config.NOTE_LIMIT, id='note_input')

    def on_mount(self) -> None:
        self.query_one('#note_input', Input).focus()

    def on_input_submitted(self, event) -> None:
        self.dismiss(event.value)

    def action_cancel_note(self) -> None:
        self.dismiss(None)


class PaneScreen(ModalScreen[None]):
    """A session's output with its scrollback, without attaching to it.

    The dashboard preview is one screen, which is what fits beside the table
    and all a poll should ever pay for. But working out why something died
    means reading further back than the last screenful, and attaching to look
    resizes the session to this terminal and disturbs whatever is still
    running in it.
    """

    BINDINGS = [
        Binding("escape", "dismiss_pane", "Close"),
        Binding("q", "dismiss_pane", "Close"),
        Binding("v", "dismiss_pane", "Close"),
    ]

    CSS = """
    PaneScreen { align: center middle; background: $background 70%; }
    #pane_dialog {
        width: 96%; height: 92%;
        border: round $primary; background: $surface; padding: 0 1;
    }
    #pane_title { text-style: bold; color: $accent; height: 1; }
    #pane_body { height: 1fr; overflow-y: scroll; }
    """

    def __init__(self, title: str, content) -> None:
        super().__init__()
        self._title = str(title)
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id='pane_dialog'):
            yield Static(f'{self._title}  ·  Esc / q / v closes', id='pane_title')
            with VerticalScroll(id='pane_body'):
                yield Static(self._content, id='pane_text')

    def on_mount(self) -> None:
        body = self.query_one('#pane_body', VerticalScroll)
        body.focus()
        # Land at the end: the interesting part of a dead run is its last
        # words, not how it started.
        body.scroll_end(animate=False)

    def action_dismiss_pane(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no that defaults to no.

    Killing a session throws away work that cannot be recovered -- the whole
    point of these sessions is that they outlive the connection -- so the
    destructive answer is never the one a stray keypress lands on.
    """

    BINDINGS = [
        Binding("escape", "refuse", "Cancel"),
        Binding("n", "refuse", "Cancel"),
        Binding("y", "accept", "Confirm"),
    ]

    CSS = """
    ConfirmScreen { align: center middle; background: $background 60%; }
    #confirm_dialog {
        width: 62; max-width: 90%; height: auto;
        border: round $error; background: $surface; padding: 1 2;
    }
    #confirm_title { text-style: bold; color: $error; }
    #confirm_hint { color: $text-muted; }
    """

    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        self._title = str(title)
        self._detail = str(detail)

    def compose(self) -> ComposeResult:
        with Vertical(id='confirm_dialog'):
            yield Static(self._title, id='confirm_title')
            yield Static(self._detail)
            yield Static('y = yes · Esc / n = no', id='confirm_hint')

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_refuse(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    """Full key list, including the ones the footer has no room for."""

    BINDINGS = [
        Binding("escape", "dismiss_help", "Close"),
        Binding("q", "dismiss_help", "Close"),
        Binding("question_mark", "dismiss_help", "Close"),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
        background: $background 60%;
    }
    #help_dialog {
        /* Wide enough that ACTS ON -- the column the footer cannot show at
           all, and the one users actually need -- is not itself truncated. */
        width: 88;
        max-width: 94%;
        height: auto;
        max-height: 90%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #help_title {
        text-style: bold;
        color: $accent;
    }
    #help_intro {
        color: $text-muted;
        margin-bottom: 1;
    }
    #help_table {
        height: auto;
    }
    #help_footer {
        color: $text-muted;
    }
    """

    def __init__(self, intro, sections, columns) -> None:
        super().__init__()
        self._intro = str(intro)
        self._sections = list(sections)
        self._columns = list(columns)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help_dialog"):
            yield Label("What this does", id="help_title", markup=False)
            yield Static(self._intro, id="help_intro", markup=False)
            table = DataTable(id="help_table", cursor_type="none")
            table.show_header = True
            table.add_columns("KEY", "DOES", "NOTE")
            for title, rows in self._sections:
                # A blank key cell turns the row into a heading, which keeps
                # one aligned grid instead of a stack of separate tables.
                table.add_row(*(_literal_cell(v)
                                for v in ('', f'— {title} —', '')))
                for key, does, note in rows:
                    table.add_row(
                        *(_literal_cell(v) for v in (key, does, note)))
            table.add_row(*(_literal_cell(v)
                            for v in ('', '— Columns —', '')))
            for name, meaning in self._columns:
                table.add_row(*(_literal_cell(v) for v in (name, meaning, '')))
            yield table
            yield Label("Esc or ? to close", id="help_footer", markup=False)

    def action_dismiss_help(self) -> None:
        self.dismiss(None)


class WebDashboardScreen(ModalScreen[None]):
    """Where the browser dashboard is, and whether it is up.

    It exists because the alternative was remembering four commands across two
    machines, and a tool you have to look up is a tool you stop reaching for.
    Everything it shows is about *this* machine -- the one running this atmux,
    which is also the machine serving the browser when you are looking at it
    through one.
    """

    CSS = """
    WebDashboardScreen {
        align: center middle;
        background: $surface 60%;
    }
    #web_dialog {
        width: 78;
        max-width: 94%;
        height: auto;
        max-height: 90%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #web_title { text-style: bold; color: $accent; }
    #web_body { height: auto; }
    #web_status { height: 2; max-height: 2; overflow-y: auto; }
    #web_buttons { height: 1; align-horizontal: right; }
    #web_buttons Button { width: 22%; min-width: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("w", "cancel", "Close"),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, state: dict) -> None:
        super().__init__()
        self._state = dict(state)

    def compose(self) -> ComposeResult:
        with Vertical(id="web_dialog"):
            yield Label("Browser dashboard", id="web_title", markup=False)
            yield Static(self._describe(), id="web_body", markup=False)
            yield Label(webcontrol.summary(self._state), id="web_status",
                        markup=False)
            with Horizontal(id="web_buttons"):
                yield Button("Start", id="web_start", compact=True)
                yield Button("Stop", id="web_stop", compact=True)
                yield Button("Restart", id="web_restart", compact=True)
                yield Button("Close", id="web_close", variant="primary",
                             compact=True)

    def _describe(self) -> str:
        state = self._state
        lines = []
        url = state.get('url')
        if url:
            lines.append('Open on your phone or tablet:')
            lines.append(f'  {url}')
        elif state.get('listening'):
            lines.append('Running, but not reachable from your tailnet yet:')
            lines.append(f'  tailscale serve --bg {state.get("port")}')
        else:
            lines.append('Not running. Start it below.')
        lines.append('')
        detail = ['listening   {}  (127.0.0.1:{})'.format(
            'yes' if state.get('listening') else 'no', state.get('port'))]
        if state.get('systemd'):
            detail.append('unit        {} - {} at boot'.format(
                state.get('unit') or '?', state.get('enabled') or '?'))
        if state.get('tailnet'):
            detail.append('this host   {}'.format(state.get('tailnet')))
        lines.extend(detail)
        lines.append('')
        lines.append('If you prefer the shell:')
        for what, command in webcontrol.commands(state):
            lines.append('  {:<11} {}'.format(what, command))
        return '\n'.join(lines)

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def action_reload(self) -> None:
        await self._refresh()

    async def _refresh(self) -> None:
        try:
            self._state = await _offload_for(8.0, webcontrol.describe)
        except Exception as error:
            self.query_one("#web_status", Label).update(
                'could not read status - {}'.format(error))
            return
        self.query_one("#web_body", Static).update(self._describe())
        self.query_one("#web_status", Label).update(
            webcontrol.summary(self._state))

    async def _control(self, verb: str) -> None:
        status = self.query_one("#web_status", Label)
        status.update('{}ing...'.format(verb))
        try:
            ok, message = await _offload_for(12.0, webcontrol.control, verb)
        except Exception as error:
            status.update('{} failed - {}'.format(verb, error))
            return
        if not ok:
            status.update(message)
            return
        await self._refresh()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button.id
        if button == 'web_close':
            self.action_cancel()
        elif button == 'web_start':
            await self._control('start')
        elif button == 'web_stop':
            await self._control('stop')
        elif button == 'web_restart':
            await self._control('restart')


class ConnectionManager(ModalScreen[dict | None]):
    """TUI-owned SSH alias picker; no hand editing of TOML is required."""

    CSS = """
    ConnectionManager {
        align: center middle;
        background: $background 55%;
    }
    #connection_dialog {
        width: 88;
        max-width: 94%;
        height: 36;
        max-height: 92%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #connection_title {
        text-style: bold;
        color: $accent;
    }
    #connection_help {
        /* Label defaults to width:auto, which lays the text out on one line
           and lets the container clip the rest -- so the explanation was
           silently cut at whatever fitted. Only a full-width box wraps. */
        width: 100%;
        height: auto;
    }
    #connection_status {
        height: 2;
        max-height: 2;
        overflow-y: auto;
    }
    #connection_aliases {
        height: 1fr;
        min-height: 4;
        border: solid $primary-darken-2;
    }
    #connection_cluster_row {
        height: 1;
        margin-bottom: 1;
    }
    #connection_cluster_label {
        width: 8;
    }
    #connection_cluster {
        width: 20;
    }
    #connection_new_cluster {
        width: 1fr;
    }
    #connection_cluster_row Button {
        width: 10;
        min-width: 1;
    }
    #connection_buttons {
        height: 1;
        align-horizontal: right;
    }
    #connection_buttons Button {
        width: 25%;
        min-width: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save & connect"),
    ]

    def __init__(self, settings: dict, aliases: list[str]) -> None:
        super().__init__()
        self._settings = dict(settings)
        # One editable record per cluster, primary included. The dialog edits
        # them one at a time; everything it did not touch -- control_path, in
        # particular -- has to survive the round trip or saving would quietly
        # delete it.
        self._clusters: dict[str, dict] = {
            config.PRIMARY_CLUSTER: {
                'gateways': list(settings.get('gateways') or ()),
                'agent_command': list(
                    settings.get('agent_command') or ['atmux-agent']),
                'control_path': None,
            }
        }
        for name, entry in (settings.get('clusters') or {}).items():
            if not isinstance(entry, dict):
                continue
            self._clusters[str(name)] = {
                'gateways': list(entry.get('gateways') or ()),
                'agent_command': list(entry.get('agent_command') or ()) or None,
                'control_path': entry.get('control_path'),
            }
        self._current = config.PRIMARY_CLUSTER
        known = [host for entry in self._clusters.values()
                 for host in entry['gateways']]
        self._aliases = list(dict.fromkeys([*known, *aliases]))

    def _cluster_options(self) -> list[tuple[str, str]]:
        return [(name, name) for name in self._clusters]

    def compose(self) -> ComposeResult:
        entry = self._clusters[self._current]
        selected = set(entry['gateways'])
        choices = [
            (alias, alias, alias in selected) for alias in self._aliases
        ]
        command = shlex.join(entry['agent_command'] or ['atmux-agent'])
        with Vertical(id="connection_dialog"):
            yield Label("Connections", id="connection_title", markup=False)
            yield Label(
                # Short on purpose: at 60x20 this dialog is the whole screen
                # and every line here is one the Save button does not get.
                "A cluster is several ways in to ONE place — AutoTmux races "
                "them. A machine somewhere else needs its own cluster. Space "
                "selects; clusters merge into one table.",
                id="connection_help", markup=False)
            with Horizontal(id="connection_cluster_row"):
                yield Label("Cluster ", id="connection_cluster_label",
                            markup=False)
                yield Select(self._cluster_options(), value=self._current,
                             allow_blank=False, id="connection_cluster",
                             compact=True)
                yield Input(placeholder="new cluster name",
                            id="connection_new_cluster", compact=True)
                yield Button("Add", id="connection_add", compact=True)
                yield Button("Remove", id="connection_remove", compact=True)
            yield SelectionList(*choices, id="connection_aliases", compact=True)
            yield Input(
                placeholder="Additional SSH aliases (space or comma separated)",
                id="connection_extra", compact=True)
            yield Input(
                value=command,
                placeholder="Remote agent command (advanced)",
                id="connection_agent", compact=True)
            yield Label("Ready", id="connection_status", markup=False)
            with Horizontal(id="connection_buttons"):
                yield Button("Test", id="connection_test", compact=True)
                yield Button("Local", id="connection_local", compact=True)
                yield Button("Cancel", id="connection_cancel", compact=True)
                yield Button(
                    "Save", id="connection_save", variant="primary",
                    compact=True)

    def on_mount(self) -> None:
        self._sync_remove_button()
        if self._aliases:
            self.query_one("#connection_aliases", SelectionList).focus()
        else:
            self.query_one("#connection_extra", Input).focus()

    # ── cluster switching ────────────────────────────────────────────────
    def _sync_remove_button(self) -> None:
        # The primary cluster is `gateways`; there is no file shape that can
        # express its absence, so it is the one that cannot be removed.
        self.query_one("#connection_remove", Button).disabled = (
            self._current == config.PRIMARY_CLUSTER)

    def _capture_current(self) -> None:
        """Fold the widgets back into the cluster being edited."""
        entry = self._clusters.get(self._current)
        if entry is None:
            return
        try:
            gateways, command = self._selection()
        except ValueError:
            # Keep whatever is valid; switching away must not lose the rest
            # because of a half-typed alias.
            gateways = list(
                self.query_one("#connection_aliases", SelectionList).selected)
            command = entry['agent_command']
        entry['gateways'] = gateways
        entry['agent_command'] = command

    def _load_cluster(self, name: str) -> None:
        entry = self._clusters[name]
        self._current = name
        aliases = self.query_one("#connection_aliases", SelectionList)
        selected = set(entry['gateways'])
        for alias in self._aliases:
            if alias in selected:
                aliases.select(alias)
            else:
                aliases.deselect(alias)
        self.query_one("#connection_extra", Input).value = ' '.join(
            host for host in entry['gateways'] if host not in self._aliases)
        self.query_one("#connection_agent", Input).value = shlex.join(
            entry['agent_command'] or ['atmux-agent'])
        self._sync_remove_button()

    def on_select_changed(self, event) -> None:
        event.stop()
        name = event.value
        if not isinstance(name, str) or name == self._current:
            return
        if name not in self._clusters:
            return
        self._capture_current()
        self._load_cluster(name)

    def action_add_cluster(self) -> None:
        field = self.query_one("#connection_new_cluster", Input)
        name = field.value.strip()
        if config.CLUSTER_NAME_RE.fullmatch(name) is None:
            self._show_error(
                'cluster name: letters, digits, - and _ (max 32)')
            return
        if name in self._clusters:
            self._show_error(f'cluster {name!r} already exists')
            return
        self._capture_current()
        self._clusters[name] = {
            'gateways': [], 'agent_command': None, 'control_path': None}
        field.value = ''
        select = self.query_one("#connection_cluster", Select)
        select.set_options(self._cluster_options())
        select.value = name
        self._load_cluster(name)
        self._show_error(f'select this cluster\'s login node(s) for {name}')

    def action_remove_cluster(self) -> None:
        if self._current == config.PRIMARY_CLUSTER:
            return
        removed = self._current
        self._clusters.pop(removed, None)
        select = self.query_one("#connection_cluster", Select)
        select.set_options(self._cluster_options())
        select.value = config.PRIMARY_CLUSTER
        self._load_cluster(config.PRIMARY_CLUSTER)
        self._show_error(f'removed cluster {removed}')

    def _selection(self) -> tuple[list[str], list[str]]:
        gateways = list(
            self.query_one("#connection_aliases", SelectionList).selected)
        extra = self.query_one("#connection_extra", Input).value.strip()
        if extra:
            try:
                values = shlex.split(extra.replace(',', ' '))
            except ValueError as error:
                raise ValueError(f'could not parse additional aliases: {error}')
            gateways.extend(values)
        gateways = list(dict.fromkeys(gateways))
        invalid = [value for value in gateways
                   if not config.valid_gateway(value)]
        if invalid:
            raise ValueError(f'invalid SSH alias: {invalid[0]!r}')
        command_text = self.query_one("#connection_agent", Input).value
        command = config._client_agent_command(command_text)
        if command is None:
            raise ValueError('invalid remote agent command')
        return gateways, command

    def _show_error(self, error: Exception | str) -> None:
        self.query_one("#connection_status", Label).update(str(error))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        try:
            self._capture_current()
            primary = self._clusters[config.PRIMARY_CLUSTER]
            gateways, command = self._selection() if (
                self._current == config.PRIMARY_CLUSTER) else (
                primary['gateways'], primary['agent_command'])
            if self._current == config.PRIMARY_CLUSTER:
                primary['gateways'], primary['agent_command'] = gateways, command
            gateways = list(primary['gateways'])
            command = primary['agent_command'] or ['atmux-agent']
            if not gateways:
                raise ValueError(
                    f'cluster {config.PRIMARY_CLUSTER} needs at least one '
                    'SSH alias')
        except ValueError as error:
            self._show_error(error)
            return
        # An emptied cluster is how you delete one; only the primary is
        # required to have members.
        extra = {
            name: {'gateways': entry['gateways'],
                   'agent_command': entry['agent_command'],
                   'control_path': entry['control_path']}
            for name, entry in self._clusters.items()
            if name != config.PRIMARY_CLUSTER and entry['gateways']
        }
        self.dismiss({
            'mode': 'gateway', 'gateways': gateways,
            'agent_command': command, 'clusters': extra,
        })

    async def _test_selection(self) -> None:
        try:
            gateways, command = self._selection()
            if not gateways:
                raise ValueError('select or enter at least one SSH alias')
        except ValueError as error:
            self._show_error(error)
            return
        status = self.query_one("#connection_status", Label)
        status.update(f"Testing {self._current}: {len(gateways)} gateway(s)…")
        settings = dict(config.CLIENT_DEFAULTS)
        settings.update(self._settings)
        settings['gateways'] = gateways
        settings['agent_command'] = command
        # This cluster's own control_path, not the global one: testing zgx
        # through an MFA helper's socket that only FASRC has would fail for a
        # reason that has nothing to do with zgx.
        control_path = self._clusters[self._current].get('control_path')
        if control_path is not None:
            settings['control_path'] = control_path
        try:
            pool = gateway_client.GatewayPool(settings)
            results = await _offload_for(
                float(settings['state_timeout']) + 2.0, pool.check_all)
        except Exception as error:
            status.update(f"Test failed: {error}")
            return
        parts = []
        for result in results:
            name = result.get('gateway', '?')
            if result.get('ok'):
                latency = result.get('latency_ms')
                detail = (f"{float(latency):.0f} ms"
                          if isinstance(latency, (int, float)) else 'ok')
                parts.append(f"✓ {name} {detail}")
            else:
                detail = " ".join(str(
                    result.get('error') or 'unavailable').split())[:64]
                parts.append(f"✗ {name} {detail}")
        visible = parts[:4]
        if len(parts) > len(visible):
            visible.append(f"+{len(parts) - len(visible)} more")
        status.update(" · ".join(visible))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == 'connection_test':
            await self._test_selection()
        elif button_id == 'connection_add':
            self.action_add_cluster()
        elif button_id == 'connection_remove':
            self.action_remove_cluster()
        elif button_id == 'connection_local':
            command = config._client_agent_command(
                self.query_one("#connection_agent", Input).value)
            self.dismiss({
                'mode': 'login', 'gateways': [],
                'agent_command': command or ['atmux-agent'],
            })
        elif button_id == 'connection_cancel':
            self.action_cancel()
        elif button_id == 'connection_save':
            self.action_save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()


# What each layout mode shows. `table` is whether the session table is on
# screen at all, `preview` the live pane beside it, `jobs` the queue below;
# `expand_jobs` gives the queue the whole body once nothing else needs it.
#
# The point of the cycle is room. A 24-line terminal spends 14 of them on the
# jobs panel and 44% of its width on the preview, which is the right default
# and the wrong thing when the answer is in the table.
_LAYOUT_SPECS = {
    'split': {'table': True, 'preview': True, 'jobs': True,
              'expand_jobs': False,
              'label': 'sessions + preview + jobs'},
    'wide': {'table': True, 'preview': False, 'jobs': True,
             'expand_jobs': False,
             'label': 'full-width sessions + jobs'},
    'table': {'table': True, 'preview': False, 'jobs': False,
              'expand_jobs': False,
              'label': 'sessions only'},
    'jobs': {'table': False, 'preview': False, 'jobs': True,
             'expand_jobs': True,
             'label': 'jobs only'},
}


# Below this the table alone needs the whole width. Defined in config beside
# the width the table needs on its own, because the browser client sizes its
# font to land on these same numbers -- a breakpoint only one side knows about
# is a breakpoint the other side lands just short of.
_MIN_SPLIT_WIDTH = config.LAYOUT_SPLIT_WIDTH


def layout_spec(mode) -> dict:
    """The pane visibility for a layout mode, defaulting on anything odd."""
    spec = _LAYOUT_SPECS.get(mode)
    return spec if spec is not None else _LAYOUT_SPECS[config.LAYOUT_DEFAULT]


class TouchBar(Vertical):
    """The live bindings as buttons big enough to hit with a thumb.

    For a touch client with no way to draw its own controls -- a phone ssh
    app, where the only surface is the character grid. The browser client
    gets the same list over OSC and draws real buttons outside the grid, so
    the two are never on screen at once: exactly one surface owns the
    controls, or the same action appears twice, differently labelled.

    It is the footer, made hittable. Textual's Footer packs its keys onto one
    line, which on a phone is a row of six-pixel targets and, past the fourth
    binding, is simply off the edge.
    """

    DEFAULT_CSS = """
    TouchBar { height: auto; dock: bottom; background: $panel; }
    TouchBar > Horizontal { height: 3; }
    TouchBar Button { width: 1fr; height: 3; min-width: 8; margin: 0 1 0 0; }
    """

    # Four across is what fits a phone at the width the layout aims for; three
    # rows deep is where it starts eating the table it exists to act on.
    PER_ROW = 4

    def __init__(self, per_row: int = PER_ROW, **kwargs) -> None:
        super().__init__(**kwargs)
        self._per_row = max(1, int(per_row))
        self._actions: dict = {}
        self._signature: tuple = ()

    async def rebuild(self, bindings) -> None:
        """Redraw only when the set actually changed.

        Called from the same poll that publishes to the browser, so it runs
        four times a second; tearing down and remounting a dozen widgets at
        that rate would be visible.
        """
        chosen = keypad.visible_bindings(bindings)
        signature = tuple(
            (e.binding.description, e.binding.action) for e in chosen)
        if signature == self._signature:
            return
        self._signature = signature
        self._actions.clear()
        await self.remove_children()
        rows: list[list] = []
        for index, entry in enumerate(chosen):
            if index % self._per_row == 0:
                rows.append([])
            name = f'tb{index}'
            # The node, not just the action: Attach is bound on the table, and
            # running it against the app looks for an action that is not there.
            self._actions[name] = (entry.node, entry.binding.action)
            button = Button(entry.binding.description, id=name)
            # A button that takes focus takes it from the table -- and Attach
            # is bound on the table, so it leaves the live set, so every
            # button after it shifts up one. Measured: a tap aimed at Layout
            # relabelled that same button Clusters before the press landed,
            # and opened the cluster manager. The controls must never be what
            # the controls are describing.
            button.can_focus = False
            rows[-1].append(button)
        for row in rows:
            await self.mount(Horizontal(*row))

    async def on_button_pressed(self, event) -> None:
        target = self._actions.get(event.button.id or '')
        event.stop()
        if target is None:
            return
        node, action = target
        await (node or self.app).run_action(action)


class AutotmuxApp(App):
    # Without this the header reads "AutotmuxApp", i.e. the class name.
    TITLE = 'atmux'

    # Textual's palette is always the rightmost thing in the footer, so its
    # "^p palette" was pushing the app's own keys off the end below ~125
    # columns -- "q Quit" rendered as "q Q". This app registers no commands of
    # its own, so the palette only ever offered Textual's built-ins, and it was
    # the one footer entry `?` did not explain. Dropping it buys back the width
    # that the documented keys need.
    ENABLE_COMMAND_PALETTE = False

    def get_driver_class(self):
        base = super().get_driver_class()
        try:
            return _make_no_motion_driver(base)
        except Exception:
            return base

    CSS = """
    Screen {
        layout: vertical;
    }
    #upper {
        height: 1fr;
    }
    /* The list and the queue, stacked. One box so the preview beside them
       runs the whole height instead of stopping on top of a queue that ran
       the full width -- which was a T rather than a grid. */
    #left_column {
        width: 56%;
        height: 100%;
    }
    #left_pane {
        width: 100%;
        height: 1fr;
    }
    #filter {
        display: none;
        height: 1;
        border: none;
        padding: 0 1;
        background: $panel;
    }
    #filter.-on { display: block; }
    #filter:focus { background: $boost; }
    /* Layout modes (`z`). The divider belongs to the preview, not the table:
       a rule down the right edge of a full-width table is a frame, not a
       separator, and reads as clipped content. Owned by the pane that only
       exists when there is something to divide, it simply leaves with it --
       and it is also where this pane says whether it has the keyboard, which
       is a thing it must say without costing a column to say it. */
    #left_column.-full {
        width: 100%;
    }
    #right_pane_scroll {
        width: 44%;
        height: 100%;
        background: $surface;
        padding: 0 1;
        /* Round, like every modal in this file already is. Sharp corners on
           the one surface people spend their time in, round everywhere else,
           was not a choice -- it was the main screen never being revisited. */
        border-left: round $primary;
    }
    /* Which pane the arrow keys are steering. Both rules are one cell wide,
       so lighting one up never reflows anything -- the alternative, a border
       that appears on focus, moves every column beside it at the moment the
       reader is looking for where they just went. */
    #right_pane_scroll:focus {
        border-left: thick $accent;
    }
    #jobs_scroll:focus {
        border-top: thick $accent;
    }
    #right_pane {
        width: 100%;
        height: auto;
    }
    /* Sized to what it holds, up to a cap. It was a fixed 35% -- fourteen
       lines standing over the three or four rows a queue usually has. */
    #jobs_scroll {
        height: auto;
        max-height: 14;
        border-top: round $primary;
        padding: 0 1;
        overflow-x: auto;
    }
    /* Expanded: the 14-line cap exists so the queue cannot crowd out the
       table. With the table gone there is nothing to protect, and the cap
       would leave most of the terminal blank. */
    #jobs_scroll.-full {
        height: 1fr;
        max-height: 100%;
        border-top: none;
    }
    #jobs_panel {
        width: auto;
        height: auto;
        /* squeue prints ~95 columns. Wrapping them onto a 58-column phone
           interleaves each row with its own continuation, which is harder to
           read than losing the tail: the leading columns -- JOBID, PARTITION,
           NAME, STATE -- are the ones worth seeing, and they stay aligned.
           The scroller below can reach the rest. */
        text-wrap: nowrap;
    }
    """

    # Labels name the object, not the verb alone: "Shell" and "Local Shell"
    # gave no clue which machine either one lands on. Enter leads because it
    # is the whole point of the table, and it was missing from the footer
    # entirely -- DataTable consumes the key and emits RowSelected, so the
    # binding below never fires, but it documents the key and `?` explains it.
    # Rarely-used keys are hidden from the footer and listed in help instead,
    # so the visible row stays readable on a narrow terminal.
    BINDINGS = [
        Binding("s", "open_shell", "SSH to node", show=False),
        Binding("o", "new_window", "New window"),
        Binding("k", "toggle_keepalive", "Auto-renew job"),
        # Hidden, not demoted: the jobs panel prints "[j: switch view]" in its
        # own title, so the key is advertised exactly where it applies. That
        # buys the footer the room for `z`, which has nowhere else to appear.
        # Shown, and then hidden by check_action everywhere it does not
        # apply. A key that is only advertised where it works is the whole
        # point; `show=False` made it invisible even in the one pane it acts
        # on, which is why the queue's own title had to advertise it.
        # Advertised in the queue's own title instead of the footer: it acts
        # on one pane, and a heading is nearer that pane than the footer is.
        Binding("j", "toggle_jobs_view", "Running / pending", show=False),
        Binding("z", "cycle_layout", "Layout", show=False),
        # Which pane the arrows steer. Tab is Textual's own, bound again here
        # only so the footer says so: a pane you cannot reach is the same as a
        # pane that is not there, and for the preview and the queue that was
        # literally true over SSH, where mouse reporting is off.
        # priority, because Screen binds tab -> app.focus_next itself with
        # show=False, and the screen is asked before the app -- so an
        # unprioritised copy here would never fire and, more to the point,
        # never appear in the footer. The action is the same one Textual's own
        # binding calls and it acts on the *active* screen, so a modal with an
        # Input still tabs between its own fields exactly as before.
        Binding("tab", "focus_next", "Panes", priority=True),
        Binding("shift+tab", "focus_previous", "Panes", show=False,
                priority=True),
        Binding("escape", "focus_table", "Back to list"),
        Binding("1", "focus_pane('table')", "Sessions pane", show=False),
        Binding("2", "focus_pane('jobs')", "Jobs pane", show=False),
        Binding("3", "focus_pane('preview')", "Preview pane", show=False),
        Binding("w", "web_dashboard", "Web", show=False),
        Binding("g", "manage_connections", "Clusters", show=False),
        Binding("e", "edit_note", "Note", show=False),
        Binding("n", "new_session", "New session", show=False),
        Binding("x", "kill_session", "Kill session", show=False),
        Binding("v", "view_pane", "View output", show=False),
        Binding("slash", "filter_sessions", "Filter"),
        Binding("colon", "command_palette", "Commands", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("q", "app.quit", "Quit"),
        Binding("r", "refresh_table", "Refresh now", show=False),
        Binding("t", "local_shell", "Local tmux", show=False),
    ]

    # One line of orientation: the keys only make sense once it is clear that
    # the sessions are somewhere else and outlive the connection to them.
    HELP_INTRO = (
        "Your tmux sessions run on Slurm compute nodes, not here. They keep "
        "running after you disconnect; this table finds them and connects you."
    )

    # Grouped by intent, because the confusing part is not any single key --
    # it is that four of them "connect" and differ only in where they land and
    # whether the thing survives disconnecting.
    HELP_SECTIONS = [
        ("Connect", [
            ("Enter", "Attach to an existing session", "survives"),
            # Named honestly: mouse reporting is off by default over SSH, so
            # on the connection most people read this over, this row was
            # advertising a way in that does nothing. `--mouse` turns it on.
            ("click", "Same as Enter (mouse is off over SSH)", "survives"),
            ("o", "Attach in a separate window, keeping this table", "survives"),
            ("s", "Plain SSH shell on that node", "dies on exit"),
            ("t", "Local tmux on this machine", "survives"),
        ]),
        ("Sessions", [
            ("n", "Create a named session on that node", "detached"),
            ("x", "Kill the selected session (asks first)", "cannot undo"),
            ("v", "Read its output, scrollback and all", "read-only"),
        ]),
        ("Allocation", [
            ("k", "Resubmit the batch script before walltime", "batch only"),
            ("j", "Bottom panel: running / queued jobs", "all jobs"),
        ]),
        # Its own section because it is its own idea, and the one people
        # arrive without: the panes beside the table hold more than they show,
        # and until now nothing but a mouse could reach the rest -- which over
        # SSH, where mouse reporting is off, meant nothing at all.
        ("Panes", [
            ("tab", "Move the keyboard to the next pane", "lights up"),
            ("shift+tab", "The same, backwards", "wraps"),
            ("1", "The session list", "arrows select"),
            ("2", "The job queue", "← → too"),
            ("3", "The preview", "arrows scroll"),
            ("escape", "Back to the session list", "from any pane"),
            ("slash", "Narrow the list by typing", "esc restores"),
            ("colon", "Every action, by name", "fuzzy"),
        ]),
        ("View", [
            ("z", "Cycle layout: split → wide → table → jobs", "remembered"),
            ("w", "Browser dashboard: address, start, stop", "this machine"),
            ("e", "Label this session with what it is for", "shows in STATUS"),
            ("g", "Add clusters and pick their login nodes", "whole session"),
            ("r", "Refresh now (it also refreshes itself)", "whole table"),
            ("↑ / ↓", "Move the selection", "table"),
            ("q", "Quit", "AutoTmux"),
        ]),
        ("Recovery", [
            # Bound in the outer tmux itself, not by AutoTmux, so it still
            # works when this process is gone -- exactly when it is needed.
            ("F12", "Restore the outer tmux after a killed client",
             "bound by tmux"),
        ]),
    ]

    # Flat view, for the tests and anything that just wants every key.
    HELP_ROWS = [row for _title, rows in HELP_SECTIONS for row in rows]

    # What the less obvious columns mean.
    HELP_COLUMNS = [
        ("IDLE", "Quiet time: yellow past 5m, red past 1h"),
        ("LEFT", "Time until Slurm ends the job"),
        ("·N", "That session has N windows (hidden when 1)"),
        ("STATUS", "Only fills in when something is wrong"),
        ("LOAD", "1-min load / cores; near cores means busy"),
    ]

    # Ours alongside Textual's own, which carries the theme and screen
    # commands. `:` opens it -- the palette key everything in this family
    # uses -- and ctrl+p keeps working because Textual binds it.
    COMMANDS = App.COMMANDS | {ActionCommands}

    title = reactive(f"AutoTmux v{__version__}")
    sub_title = reactive("")

    def __init__(self, *, offer_connection_setup: bool = False,
                 select: tuple[str, str] | None = None) -> None:
        super().__init__()
        self._connection_setup_pending = bool(offer_connection_setup)
        # (node, session) to open on, from --select. See on_mount.
        self._select = select
        self._connection_manager_open = False
        self._help_open = False
        # What the list is narrowed to, '' for everything, and how much that
        # hid -- the count is what stops a filter being invisible state.
        self._filter = ''
        self._match_count = (0, 0)
        self._restart_attempts = []   # time.monotonic() of recent daemon restarts
        self._crash_looping = False
        self._recovery_inflight = False
        # Job-expiry reminders shown on this machine. Config is read once:
        # a refresh must not stat the config file on every tick.
        try:
            self._notify_cfg = config.load_notify()
        except Exception:
            self._notify_cfg = dict(config.NOTIFY_DEFAULTS)
        self._warned_jobs = _load_warned_jobs()
        # Which panes are on screen. Read before the first frame so a
        # remembered layout is what gets painted, rather than the default
        # flashing up and being rearranged a moment later.
        try:
            self.layout_mode = config.load_layout()
        except Exception:
            self.layout_mode = config.LAYOUT_DEFAULT
        # Being asked to stand on a row, in a layout that shows no rows, is a
        # request that cannot be honoured -- and it is the saved layout that
        # decides, so someone who last left the queue on screen would tap a
        # session and arrive somewhere with no sessions in it. Overridden for
        # this run only: not saved, so a plain `atmux` still opens the way
        # they left it.
        if select and not layout_spec(self.layout_mode)['table']:
            self.layout_mode = config.LAYOUT_DEFAULT
        _apply_idle_thresholds()
        # A timed-out NFS registry read keeps running on its daemon thread.
        # Single-flight it so manual/timer refreshes cannot strand all eight
        # general I/O slots behind the same unavailable mount.
        self._ka_registry_read_lock = threading.Lock()

    def compose(self) -> ComposeResult:
        # No clock: HeaderClock repaints the header every second, which over a
        # remote/SSH terminal is a constant trickle of redraws even while the
        # app is idle. Keeping the header static makes an idle atmux silent on
        # the wire.
        yield StatusLine(id='statusline')
        # The queue belongs under the list, not under both panes.
        #
        # It used to be a child of the screen, so it ran the full width while
        # the preview was a column stopping on top of it -- a T, not a grid.
        # Nesting it with the list makes the preview one column to the bottom
        # and puts the queue under the thing it is about: the queue's rows and
        # the list's rows are the same machines in Slurm's vocabulary.
        with Horizontal(id="upper"):
            with Vertical(id="left_column"):
                # Hidden until `/`. It is one line, and one line of a list
                # you are trying to see less of is a line worth not spending
                # when you are not filtering.
                yield FilterInput(placeholder="filter sessions", id="filter")
                yield ClickToAttachDataTable(id="left_pane")
                # squeue output contains user-controlled job names and array
                # syntax; render it literally rather than treating ``[...]``
                # as Rich markup. The scroller is a container so the expanded
                # layout can hand the queue the arrow keys; a bare Static
                # scrolls by mouse only.
                with VerticalScroll(id="jobs_scroll"):
                    yield Static("(loading squeue...)", id="jobs_panel",
                                 markup=False)
            with VerticalScroll(id="right_pane_scroll"):
                yield Static("", id="right_pane", markup=False)
        # Whoever can draw the controls, draws them once. A browser client
        # renders the same bindings as real buttons outside the grid, so the
        # footer there is a second, smaller, less complete copy of what is
        # already on screen -- and a phone ssh client has no surface but this
        # one, so it gets buttons instead of a line of six-pixel targets.
        mode = keypad.touch_mode()
        if mode == 'local':
            yield TouchBar(id="touchbar")
        elif mode != 'web':
            yield Footer()

    async def on_mount(self) -> None:
        self.table = self.query_one(DataTable)
        self.table.cursor_type = "row"
        # One column, and the line inside it composed by hand.
        #
        # It was six, and a grid is the wrong shape for rows meant to read as
        # sentences: every column is shared by everything in it, so a band
        # heading had to be squeezed into one of them, and one wide value set
        # a column's width for every row. Composing the line means the last
        # field can take whatever width is left, which is where the output
        # goes -- and the output is the thing that says whether a stopped
        # session finished or broke.
        #
        # No header either. A header names columns, and there are none to
        # name; the bands say what each group is, which is the heading that
        # was actually missing.
        self.table.add_columns("SESSIONS")
        self.table.show_header = False

        self.log_view = self.query_one("#right_pane", Static)
        self.jobs_view = self.query_one("#jobs_panel", Static)

        # The preview pane is a focusable VerticalScroll by default. Take it
        # OUT of the focus chain so the DataTable is the ONLY focus target:
        # otherwise a stray click on the (large) right pane, or a Tab, moves
        # focus there and the arrow keys scroll the preview instead of moving
        # the session cursor — i.e. "can't move up/down". Mouse-wheel scroll
        # of the preview still works without focus.
        self.query_one("#right_pane_scroll").can_focus = False
        self.table.focus()
        self._apply_layout()

        # Polled rather than hooked to an event: bindings change when a modal
        # opens, when focus moves and when the layout changes panes, and one
        # cheap check that catches all three beats three hooks that between
        # them still miss the fourth. 20us a call, four times a second.
        self._touch_mode = keypad.touch_mode()
        self._published_keys = None
        if self._touch_mode == 'web':
            self._publish_keys()
            self.set_interval(0.25, self._publish_keys)
        elif self._touch_mode == 'local':
            self._touch_bar = self.query_one(TouchBar)
            await self._touch_bar.rebuild(self.active_bindings)
            self.set_interval(0.25, self._sync_touch_bar)

        self.all_sessions: list = []
        # Index-for-index with the *table*, which the session list is not:
        # a band heading takes a row of its own and holds None here. Kept
        # separate rather than threading Nones through all_sessions, because
        # `all_sessions` means the sessions and everything reading it expects
        # exactly that -- quietly changing what a name means is how a rename
        # becomes a bug somewhere it was never edited.
        self.row_targets: list = []
        # The row the cursor should land on. _refresh_table restores the
        # cursor by (node, session) on every rebuild, so seeding it here is
        # the whole of --select: the first paint already has the right row
        # highlighted, and every action acts on the highlighted row.
        preselect = getattr(self, '_select', None)
        self.selected_node = preselect[0] if preselect else ""
        self.selected_session = preselect[1] if preselect else ""
        # Latest daemon state (kept so the keep-alive toggle can look up the
        # highlighted row's job id/name without another read).
        self._last_state: dict = {}
        # (stat-sig, names) cache so we don't re-parse the registry every 5s
        # refresh (it sits on NFS ~/.config). None sig = file absent.
        self._ka_reg_cache = (None, tuple())
        # Enabled keep-alive entries, refreshed OFF the event loop (the file is
        # on NFS). Current entries carry JobID+UUID; name-only entries remain
        # supported for migration from older releases.
        self._ka_entries: list[dict] = []
        # Compatibility/diagnostic view used nowhere for identity decisions.
        self._ka_names: set = set()
        # Job identities whose keep-alive enable is mid-flight (scontrol running),
        # so a double 'k' press can't spawn two conflicting toggles.
        self._ka_inflight: set = set()
        # Two views: 'long' (squeue -l) and 'pending' (squeue -l --start).
        self.jobs_view_mode = 'long'
        self.snapshots: dict = {}
        # Per-(node, session, ts) cache of parsed Rich Text — repeating
        # the same row is essentially free.
        self._rendered_cache: dict = {}
        # What each session is for, keyed by session name. Read once here and
        # after an edit; a note changes only when the user changes it.
        self._notes: dict = config.load_notes()
        self._last_rows_sig: tuple | None = None
        # Identity of the displayed rows (node, session) in order. When only
        # this is unchanged we update cells in place instead of rebuilding.
        self._last_structural_sig: tuple | None = None
        # Lightweight debounce so a fast burst of ↑↓ doesn't queue up
        # a render per keystroke.
        self._preview_render_timer = None
        self._selection_changed_at = 0.0
        # Retained only so a live deployment switch can shut down legacy warm
        # children created by an older frontend. Normal attaches never call
        # this pool; terminal bytes always stay in native OpenSSH.
        self._warm_pool = WarmSlavePool()
        self._interactive_prewarm_retry: dict[str, float] = {}
        self._interactive_prewarming: set[str] = set()
        self._interactive_prewarm_lock = threading.Lock()

        # Paint an immediately-responsive empty shell, then populate from
        # bounded background reads. Even a wedged runtime filesystem can no
        # longer freeze atmux before its first frame appears.
        self._refresh_table({})
        self._refresh_jobs({})
        self.set_interval(5, self._refresh_async)
        # Snapshot reload runs in a worker thread to avoid blocking the
        # event loop on filesystem hiccups.
        self.set_interval(30, self._reload_snapshots_async)
        self.run_worker(self._preview_loop(), exclusive=True)
        # Do not hide persisted keep-alive markers for the first five seconds.
        # This remains a bounded background read, so a sick NFS home never
        # delays initial rendering.
        self.run_worker(self._refresh_async(), exclusive=True,
                        group='initial-refresh')
        self.run_worker(self._reload_snapshots_async(render_loading=True), exclusive=True,
                        group='initial-snapshots')
        if self._connection_setup_pending:
            self.call_after_refresh(self.action_manage_connections)

    async def action_show_help(self) -> None:
        """Show every key, including those the footer cannot fit."""
        if self._help_open:
            return
        self._help_open = True
        try:
            await self.push_screen_wait(HelpScreen(
                self.HELP_INTRO, self.HELP_SECTIONS, self.HELP_COLUMNS))
        except Exception:
            pass
        finally:
            self._help_open = False

    async def action_manage_connections(self) -> None:
        """Open the local SSH-alias picker without blocking the render loop."""
        if self._connection_manager_open:
            return
        self._connection_manager_open = True
        if _GATEWAY_POOL is not None:
            settings = dict(_GATEWAY_POOL.settings)
        else:
            ok, loaded = _load_client_config_bounded()
            settings = (dict(loaded) if ok and isinstance(loaded, dict)
                        else dict(config.CLIENT_DEFAULTS))
        try:
            aliases = await _offload_for(
                _CLIENT_CONFIG_TIMEOUT, config.discover_ssh_aliases)
        except Exception:
            aliases = []
        try:
            await self.push_screen(
                ConnectionManager(settings, aliases),
                callback=self._connection_manager_closed)
        except Exception as error:
            self._connection_manager_open = False
            self._connection_setup_pending = False
            self.notify(f'could not open Connections · {error}',
                        severity='error', timeout=7, markup=False)

    async def _connection_manager_closed(self, result: dict | None) -> None:
        self._connection_manager_open = False
        # What the list is narrowed to, '' for everything.
        self._filter = ''
        if result is None:
            self._connection_setup_pending = False
            # First-run Cancel means "not now".  Resume the ordinary local
            # daemon path, but ask again on a future launch because no choice
            # was persisted.
            self._refresh_table(self._last_state)
            return
        self._connection_setup_pending = True
        try:
            settings, pool = await _offload_for(
                5.0, self._persist_connection_choice, result)
        except Exception as error:
            self._connection_setup_pending = False
            self.notify(f'could not save Connections · {error}',
                        severity='error', timeout=8, markup=False)
            self._refresh_table(self._last_state)
            return
        self._connection_setup_pending = False
        global _GATEWAY_POOL, _RUNTIME_DISCOVERY_ENABLED
        _GATEWAY_POOL = pool
        _RUNTIME_DISCOVERY_ENABLED = pool is None
        if pool is None:
            _sync_active_runtime_paths()
        # Native warm slaves point at a different host namespace and must not
        # survive a live deployment switch.  Shutdown is bounded off-loop.
        old_warm_pool = self._warm_pool
        self._warm_pool = WarmSlavePool()
        try:
            await _offload_for(1.0, old_warm_pool.shutdown)
        except Exception:
            pass
        self._last_state = {}
        self._last_rows_sig = None
        self._last_structural_sig = None
        self._ka_reg_cache = (None, tuple())
        self._ka_entries = []
        self._ka_names = set()
        self.snapshots = {}
        self._rendered_cache.clear()
        self._refresh_table({})
        mode = ('gateway pool ' + ', '.join(settings['gateways'])
                if pool is not None else 'local/login-node mode')
        self.notify(f'Connections saved · using {mode}', timeout=6,
                    markup=False)
        await self.action_refresh_table()

    @staticmethod
    def _persist_connection_choice(result: dict) -> tuple[dict, object | None]:
        mode = result.get('mode')
        gateways = list(result.get('gateways') or ())
        command = result.get('agent_command') or ['atmux-agent']
        config.save_client_state(mode, gateways, command,
                                 result.get('clusters'))
        settings = config.load_client()
        # ClusterPool, not GatewayPool: rebuilding a single-cluster pool here
        # would silently drop every other cluster until the next restart.
        pool = (gateway_client.ClusterPool(
                    config.client_clusters(settings), settings)
                if mode == 'gateway' else None)
        return settings, pool

    # ── table refresh ─────────────────────────────────────────────────────────

    async def _refresh_async(self) -> None:
        """Timer entry point: read daemon state OFF the event loop, then
        render. Keeps the 5s tick from freezing the UI on a slow / NFS /
        mid-write state-file read."""
        source_pool = _GATEWAY_POOL
        if source_pool is None:
            _sync_active_runtime_paths()
        try:
            timeout = (_UI_FILE_READ_TIMEOUT if source_pool is None else
                       float(source_pool.settings['state_timeout']) + 1.0)
            state_ok, state = await _offload_for(
                timeout, _read_state_checked)
        except Exception:
            state_ok, state = False, {}
        if source_pool is not _GATEWAY_POOL:
            # The user changed deployment in Connections while this bounded
            # read was in flight.  Never paint the old host namespace over the
            # freshly-selected one.
            return
        if not state_ok:
            # A daemon restart briefly removes the file. Keep the last complete
            # view instead of clearing rows and moving the user's cursor.
            state = self._last_state
        elif self._state_is_older(state, self._last_state):
            # A slow reader may have opened the previous atomic-write inode,
            # then completed after a newer manual/timer refresh. Never let that
            # late result roll sessions, job times, or selection backward.
            state = self._last_state
        # State is runtime-local and the primary UI payload. Render it before
        # touching the NFS-backed registry so a slow home directory cannot
        # hold back fresh sessions/jobs.
        self._refresh_table(state)
        self._refresh_jobs(state)
        # Refresh the keep-alive name stash OFF the loop too — the registry file
        # is on NFS ~/.config and _decorate_keepalive must not stat/read it on
        # the event-loop thread.
        try:
            new_entries = await _offload_for(
                _operation_timeout(_UI_FILE_READ_TIMEOUT),
                self._ka_registry_entries)
        except Exception:
            return
        if source_pool is not _GATEWAY_POOL:
            return
        if new_entries != self._ka_entries:
            self._ka_entries = new_entries
            self._ka_names = {
                entry.get('job_name') for entry in self._ka_entries
                if isinstance(entry.get('job_name'), str)
            }
            # Only the STATUS marker changes; the structural in-place path
            # keeps selection and preview stable.
            self._refresh_table(state)

    def _status_or_note(self, session: str, status: str) -> str:
        """STATUS, falling back to the session's note when it has nothing to say.

        A warning always wins. The note is filling space that is otherwise
        blank, so it must never be the reason a DEGRADED or ESC warning went
        unseen.
        """
        if status:
            return status
        return self._notes.get(str(session), '')

    def _row_width(self) -> int:
        """How wide a row may be. The table is one column now, so nothing
        else decides this for us."""
        try:
            width = int(self.table.size.width)
        except Exception:
            width = 0
        return max(48, width - 2 if width else 78)

    def _row_tail(self, r) -> str:
        """What this session was last doing.

        Free: the daemon already snapshots every pane on a timer so the
        preview has something to show instantly, and the notice path already
        knows how to pick the one line out of a screen that says something.
        Nothing here is fetched.
        """
        session = r[1]
        # A warning outranks the output. DEGRADED, an escape-time or a
        # network state is about whether this row can be reached at all, and
        # the last line of a pane you cannot reach is not the news. Dropping
        # it here made every warning the table had invisible.
        _marker, status = _split_idle_marker(r[4])
        note = self._status_or_note(session, _status_text(status))
        if note:
            return note
        if session in (_OFFLINE_SESSION, _START_SHELL_SESSION):
            return ''
        snap = self.snapshots.get(f'{r[0]}:{session}')
        if not isinstance(snap, dict):
            return ''
        try:
            return notify.last_output_line(snap.get('lines') or '')
        except Exception:
            return ''

    def _update_row_cells(self, i: int, r) -> bool:
        """Rewrite row i in place, without rebuilding the table.

        One cell now: the row is a composed line rather than six cells, so
        there is nothing to update selectively. It still exists because the
        reason it existed has not changed -- clear()+add_row resets the
        cursor, and the load average ticks every five seconds.

        Returns False if the write raised, so the caller can avoid caching a
        signature that does not match what is on screen.
        """
        try:
            widths = _row_widths(
                [x for x in self.row_targets if x is not None],
                self._row_width())
            line = _compose_session(r, widths, self._row_tail(r),
                                    self._row_width())
            coord = Coordinate(i, 0)
            if str(self.table.get_cell_at(coord)) != str(line):
                self.table.update_cell_at(coord, line)
            return True
        except Exception:
            return False

    # Cap background handshakes.  These are ordinary no-PTY `ssh ... true`
    # channels that leave only a short-lived ControlMaster behind; bounding the
    # set avoids an authentication burst for very large allocations.
    _MAX_WARM = 4

    def _dispatch_warm(self, rows) -> None:
        # Historical versions pre-opened a remote shell on a private PTY and
        # relayed every terminal byte through Python.  That saved one remote
        # fork at attach time, but under backpressure it accumulated old screen
        # output and queued input -- the user-visible "input drift".  Warm only
        # native OpenSSH masters now; no PTY or terminal byte is retained here.
        state_nodes = self._last_state.get('nodes', {}) if isinstance(
            self._last_state, dict) else {}
        nodes = []
        for row in rows:
            node = row[0]
            if node == 'localhost' or node in nodes:
                continue
            node_state = state_nodes.get(node, {}) if isinstance(
                state_nodes, dict) else {}
            network_state = node_state.get('network', {}) if isinstance(
                node_state, dict) else {}
            if (isinstance(network_state, dict)
                    and network_state.get('state') in {
                        'suspect', 'offline', 'half-open'}):
                continue
            nodes.append(node)
        if self.selected_node in nodes:
            nodes.remove(self.selected_node)
            nodes.insert(0, self.selected_node)
        if not nodes:
            return
        source_pool = _GATEWAY_POOL
        self.run_worker(
            partial(
                self._prewarm_interactive_async,
                tuple(nodes[:self._MAX_WARM]), source_pool,
            ),
            exclusive=True, group='interactive-prewarm',
        )

    def _maybe_warn_expiring_jobs(self, state) -> None:
        """Pop a desktop notification for a job nearing its time limit.

        The dashboard already holds every field the reminder needs, so the
        machine running the TUI can warn on its own -- useful when the user is
        at the laptop rather than reading a chat webhook.  Announcements are
        remembered on disk so restarting the TUI does not re-announce a job.
        """
        cfg = self._notify_cfg
        if not cfg.get('enabled'):
            return
        try:
            jobs = notify.jobs_from_state(state)
            due = notify.due_jobs(
                jobs, float(cfg['lead_time']), self._warned_jobs)
        except Exception:
            return
        if not due:
            return
        live = {str(job.get('job_id')) for job in jobs}
        # Drop jobs that have left the queue so the record cannot grow forever.
        self._warned_jobs = {j for j in self._warned_jobs if j in live}
        for job in due:
            job_id = str(job.get('job_id') or '')
            text = notify.build_message(job, job['remaining'])
            self._warned_jobs.add(job_id)
            # The in-app banner is this dashboard's own UI, so it shows
            # whenever reminders are on; `desktop` governs only the OS popup,
            # which is what a user silences when they find it intrusive.
            self.notify(text, severity='warning', timeout=10, markup=False)
            if cfg.get('desktop'):
                self.run_worker(
                    _offload(notify.local_notify, 'AutoTmux', text),
                    exclusive=False, group='job-warning')
        _save_warned_jobs(self._warned_jobs)

    def _refresh_table(self, state=None) -> None:
        self._maybe_recover_daemon()
        # Cheap and idempotent, and the backstop for the resize hook: whether
        # there is room for the preview depends only on the width, so if that
        # hook ever stops firing the view still corrects itself on the next
        # tick rather than staying wrong until a keypress.
        if getattr(self, 'table', None) is not None:
            try:
                self._apply_layout()
            except Exception:
                pass
        if state is None:
            state = read_state()
        if not isinstance(state, dict):
            state = {}
        _apply_daemon_ssh_settings(state)
        self._last_state = state
        self._maybe_warn_expiring_jobs(state)
        rows = build_session_rows(state)
        rows = self._decorate_keepalive(rows, state)
        showing = len(rows)
        if self._filter:
            rows = filter_rows(rows, self._filter)
        self._match_count = (len(rows), showing)
        updated = state.get('updated', '?')

        sig = tuple(rows)
        if sig == self._last_rows_sig:
            # Nothing changed at all — just refresh the subtitle.
            self.sub_title = self._status_subtitle(state, rows, updated)
            self._paint_status(rows, state, updated)
            # Warm slaves can die without any daemon-state change. Recheck so
            # the fast attach path is eventually replenished.
            if rows:
                self._dispatch_warm(rows)
            return

        # The bands are part of the structure: a session moving from "working"
        # to "just stopped" changes no (node, session) pair but does change
        # which heading it sits under, and the in-place path cannot move a row
        # between bands. Including the rank forces the rebuild that can.
        structural = tuple((r[0], r[1], session_rank(r)) for r in rows)
        if (structural == self._last_structural_sig
                and len(self.row_targets) == self.table.row_count):
            # Same (node, session) rows, in the same order and the same bands;
            # only volatile cells (time/cpu/load/win/status) changed. Update
            # them in place instead of clear()+add_row — the latter resets the
            # cursor and churns RowHighlighted every 5s because the load
            # average ticks constantly.
            #
            # Walked against the placed list rather than by enumerating rows:
            # the table row index and the session index stopped being the same
            # number the moment headings took slots of their own.
            fresh = iter(rows)
            placed = list(self.row_targets)
            ok = True
            for i, slot in enumerate(placed):
                if slot is None:
                    continue            # a heading has nothing volatile in it
                r = next(fresh, None)
                if r is None:
                    ok = False
                    break
                placed[i] = r
                ok = self._update_row_cells(i, r) and ok
            self.row_targets = placed
            self.all_sessions = rows
            # Only cache the sig if the screen actually matches it — otherwise a
            # swallowed cell-write failure would stick a stale cell until the
            # next structural change.
            self._last_rows_sig = sig if ok else None
            self.sub_title = self._status_subtitle(state, rows, updated)
            self._paint_status(rows, state, updated)
            self._dispatch_warm(rows)
            return
        self._last_rows_sig = sig
        self._last_structural_sig = structural

        # Structural change — full rebuild. Restore the cursor by
        # (node, session) so it sticks even if rows reorder.
        previous = (self.selected_node, self.selected_session)
        # A row asked for with --select is a wish, not a selection: the first
        # paint runs against an empty state, and the rule that drops a
        # selection whose row has vanished would drop this one before its row
        # ever arrives. Held separately until it does.
        if getattr(self, '_select', None):
            previous = self._select
        # The bands. A heading is a table row of its own, so the table stops
        # being index-for-index with the session list; `row_targets` is the
        # one that stays aligned with the table, and holds None where a
        # heading sits.
        plan = plan_rows(rows)
        placed = []
        headings = set()
        widths = _row_widths(rows, self._row_width())
        self.table.clear()
        for entry in plan:
            if entry[0] == 'band':
                headings.add(len(placed))
                placed.append(None)
                self.table.add_row(_band_cells(entry[1], entry[2]))
                continue
            r = entry[1]
            placed.append(r)
            self.table.add_row(_compose_session(
                r, widths, self._row_tail(r), self._row_width()))
        self.table.heading_rows = headings
        self.row_targets = placed
        self.all_sessions = [r for r in placed if r is not None]
        rows = self.all_sessions

        if rows:
            new_idx = next((i for i, r in enumerate(placed) if r is not None), 0)
            for i, r in enumerate(placed):
                if r is not None and (r[0], r[1]) == previous:
                    new_idx = i
                    # Granted. From here it is an ordinary selection and
                    # moves like one.
                    self._select = None
                    break
            self.table.move_cursor(row=new_idx)
            # Reconcile the tracked selection with the row the cursor actually
            # landed on. move_cursor() emits no RowHighlighted when the numeric
            # row is unchanged (e.g. the selected session vanished and the
            # cursor falls back to row 0), which would otherwise leave
            # selected_node/session pointing at a gone row — so `k`/attach/preview
            # would act on the wrong job.
            # `placed`, not `rows`: new_idx is a table row, and the table has
            # headings in it that the session list does not.
            landed = placed[new_idx]
            self.selected_node, self.selected_session = landed[0], landed[1]
            if (self.selected_node, self.selected_session) != previous:
                self._selection_changed_at = time.monotonic()
                if self._preview_render_timer is not None:
                    self._preview_render_timer.stop()
                    self._preview_render_timer = None
                # The old target may have vanished while its numeric row stayed
                # the same, in which case Textual emits no highlight event.
                # Replace its preview now instead of showing the wrong session
                # until the next one-second live capture.
                self._render_preview_now()
        else:
            # Never leave actions pointed at a row which vanished. A stale
            # selection could make `s`, `k`, or delayed Enter act on a target
            # which is no longer visible.
            self.selected_node = ""
            self.selected_session = ""
            self._selection_changed_at = time.monotonic()
            if self._preview_render_timer is not None:
                self._preview_render_timer.stop()
                self._preview_render_timer = None
            self.log_view.update("")

        self.sub_title = self._status_subtitle(state, rows, updated)
        self._paint_status(rows, state, updated)
        self._dispatch_warm(rows)

    # ── keep-alive display ───────────────────────────────────────────────────

    def _ka_registry_entries(self, require_fresh: bool = False) -> list[dict]:
        """Enabled registry entries with a single-flight NFS read.

        A filesystem timeout cancels only the awaiting coroutine; the kernel
        read may remain stuck.  Holding this dedicated lock in that worker
        makes later timer refreshes return the last-good cache immediately
        instead of consuming every general offload slot.  Interactive toggles
        request a fresh result and receive a useful busy error instead.
        """
        acquired = self._ka_registry_read_lock.acquire(
            timeout=0.25 if require_fresh else 0)
        if not acquired:
            if require_fresh:
                raise RuntimeError('keep-alive registry read is still in progress')
            cached = self._ka_reg_cache[1]
            return [dict(entry) for entry in cached if isinstance(entry, dict)]
        try:
            if _GATEWAY_POOL is not None:
                try:
                    entries = _GATEWAY_POOL.keepalive_entries(require_fresh)
                except Exception:
                    if require_fresh:
                        raise
                    cached = self._ka_reg_cache[1]
                    return [dict(entry) for entry in cached
                            if isinstance(entry, dict)]
                enabled = tuple(
                    dict(entry) for entry in entries
                    if isinstance(entry, dict) and entry.get('enabled'))
                self._ka_reg_cache = (
                    ('gateway', _GATEWAY_POOL.active_gateway), enabled)
                return [dict(entry) for entry in enabled]
            cached_sig, cached_entries = self._ka_reg_cache
            try:
                st = os.stat(config.KEEPALIVE_PATH)
                sig = (getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9)),
                       st.st_size, st.st_ino)
            except FileNotFoundError:
                sig = None
            except OSError:
                if require_fresh:
                    raise
                return [dict(entry) for entry in cached_entries
                        if isinstance(entry, dict)]
            if sig == cached_sig:
                return [dict(entry) for entry in cached_entries
                        if isinstance(entry, dict)]
            ok, entries = keepalive._load_registry_checked(config.KEEPALIVE_PATH)
            if not ok:
                # A transient NFS read must neither hide enabled markers nor
                # cache the failed signature forever. Retry on next refresh.
                if require_fresh:
                    raise OSError('could not safely read keep-alive registry')
                return [dict(entry) for entry in cached_entries
                        if isinstance(entry, dict)]
            enabled = tuple(
                dict(entry) for entry in entries
                if isinstance(entry, dict) and entry.get('enabled')
            )
            self._ka_reg_cache = (sig, enabled)
            return [dict(entry) for entry in enabled]
        finally:
            self._ka_registry_read_lock.release()

    def _ka_registry_names(self) -> set:
        """Compatibility view for tests/diagnostics; identity is JobID-based."""
        return {
            entry.get('job_name') for entry in self._ka_registry_entries()
            if isinstance(entry.get('job_name'), str) and entry.get('job_name')
        }

    @staticmethod
    def _ka_entry_matches(entry: dict, job_id, job_name) -> bool:
        return model.entry_matches(entry, job_id, job_name)

    def _ka_find_entry(self, job_id, job_name) -> dict | None:
        return model.find_entry(self._ka_entries, job_id, job_name)

    @staticmethod
    def _ka_status_for_entry(ka_status: dict, entry: dict) -> dict:
        return model.status_for_entry(ka_status, entry)

    @staticmethod
    def _ka_suffix(ka_state: dict) -> str:
        """Status text appended to a registered row's STATUS cell."""
        return model.keepalive_suffix(ka_state)

    def _decorate_keepalive(self, rows, state):
        """Fold the keep-alive marker into registered rows.

        The browser dashboard shows the same marker from the same function:
        two answers to "is this job being renewed" is one of them wrong.
        """
        return model.decorate_keepalive(rows, state, self._ka_entries)

    @staticmethod
    def _daemon_age_seconds(updated: str, updated_monotonic=None,
                            clock_id: str | None = None):
        """How many seconds ago did the daemon last write? Returns None
        if neither the monotonic nor wall-clock timestamp can be parsed.

        Daemon and frontend run on the same host, so monotonic time avoids an
        NTP/administrator wall-clock adjustment making a frozen daemon look
        permanently fresh (or a healthy one spuriously ancient).
        """
        try:
            if (clock_id is None or clock_id == _CLOCK_ID) \
                    and isinstance(updated_monotonic, (int, float)) \
                    and not isinstance(updated_monotonic, bool):
                age = time.monotonic() - float(updated_monotonic)
                if age >= 0 and math.isfinite(age):
                    return age
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            from datetime import datetime
            t = datetime.strptime(updated, '%Y-%m-%d %H:%M:%S')
            return (datetime.now() - t).total_seconds()
        except Exception:
            return None

    @staticmethod
    def _state_is_older(incoming: dict, current: dict) -> bool:
        """Whether an asynchronously-read state predates the displayed one."""
        if not isinstance(incoming, dict) or not isinstance(current, dict):
            return False

        incoming_sequence = incoming.get('gateway_sequence')
        current_sequence = current.get('gateway_sequence')
        if (isinstance(incoming_sequence, int)
                and not isinstance(incoming_sequence, bool)
                and isinstance(current_sequence, int)
                and not isinstance(current_sequence, bool)):
            return incoming_sequence < current_sequence

        def monotonic_value(state):
            value = state.get('updated_monotonic')
            if (isinstance(value, (int, float))
                    and not isinstance(value, bool)):
                try:
                    value = float(value)
                    return value if value >= 0 and math.isfinite(value) else None
                except (TypeError, ValueError, OverflowError):
                    pass
            return None

        incoming_clock = incoming.get('monotonic_clock_id')
        current_clock = current.get('monotonic_clock_id')
        comparable_clocks = not (
            isinstance(incoming_clock, str)
            and isinstance(current_clock, str)
            and incoming_clock != current_clock)
        if comparable_clocks:
            incoming_mono = monotonic_value(incoming)
            current_mono = monotonic_value(current)
            if current_mono is not None:
                return incoming_mono is None or incoming_mono < current_mono
            if incoming_mono is not None:
                return False

        try:
            from datetime import datetime
            incoming_wall = datetime.strptime(
                incoming.get('updated', ''), '%Y-%m-%d %H:%M:%S')
            current_wall = datetime.strptime(
                current.get('updated', ''), '%Y-%m-%d %H:%M:%S')
            return incoming_wall < current_wall
        except (TypeError, ValueError):
            return False

    def _maybe_recover_daemon(self) -> None:
        """Auto-restart a PID-dead daemon (with loop guard); banner-only for a
        hung-but-alive one (the existing stale subtitle covers that, and we
        never auto-kill a daemon that's merely slow)."""
        if self._connection_setup_pending:
            return
        if _gateway_mode() or _daemon_running():
            self._crash_looping = False
            self._recovery_inflight = False
            return
        if self._recovery_inflight:
            return
        now = time.monotonic()
        # Drop attempts older than the guard window so the list can't grow
        # unbounded over a long-lived session.
        self._restart_attempts = [t for t in self._restart_attempts
                                  if now - t < _RESTART_WINDOW]
        if not _should_restart(self._restart_attempts, now):
            self._crash_looping = True
            return
        self._crash_looping = False
        self._restart_attempts.append(now)
        self._recovery_inflight = True
        self.notify('daemon down — restarting…', severity='warning', timeout=4)
        self._dispatch_restart()

    def _dispatch_restart(self) -> None:
        self.run_worker(self._restart_daemon_async(),
                        exclusive=True, group='recovery')

    async def _restart_daemon_async(self) -> None:
        try:
            ok, error = await _offload(_launch_daemon)
            if not ok:
                self.notify(
                    f'daemon restart failed · {error or "unknown error"}',
                    severity='error', timeout=7, markup=False)
        except Exception as error:
            # Recovery is retried by the next refresh and must never terminate
            # the Textual app merely because all bounded I/O slots are busy.
            self.notify(f'daemon restart failed · {error}', severity='error',
                        timeout=7, markup=False)
        finally:
            self._recovery_inflight = False

    # Which token each band is counted in. The bands already say how much a
    # row wants a decision; this is the same judgement, coloured.
    _BAND_TONES = {'not reachable': 'danger', 'just stopped': 'warn',
                   'working': 'ok', 'quiet a while': 'quiet'}

    def _paint_status(self, rows, state, updated) -> None:
        """The top line, from the same plan the table is built from.

        Counted off the plan rather than recomputed, so the header and the
        bands under it cannot disagree about how many things stopped.
        """
        try:
            line = self.query_one('#statusline', StatusLine)
        except Exception:
            return
        counts = []
        for entry in plan_rows(rows):
            if entry[0] != 'band' or entry[1] not in self._BAND_TONES:
                continue
            counts.append((entry[1], entry[2], self._BAND_TONES[entry[1]]))
        warning = self._status_subtitle(state, rows, updated)
        # The healthy subtitle is a count of sessions, which the bands now
        # say better. Only what is wrong earns a place up here.
        if '⚠' not in warning and 'daemon' not in warning.lower():
            warning = ''
        # A filter is the one piece of state that hides things, so it is the
        # one that must never be hidden itself: with the bar closed, missing
        # sessions read as sessions having gone missing.
        shown, total = self._match_count
        if self._filter:
            note = f'/{self._filter}  {shown} of {total}'
            warning = f'{note}   {warning}' if warning else note
        line.show(counts, warning)

    def _status_subtitle(self, state, rows, updated) -> str:
        gateway_info = state.get('gateway') if isinstance(state, dict) else None
        gateway_active = (gateway_info.get('active')
                          if isinstance(gateway_info, dict) else '')
        # Whether this is a gateway client is decided by how the process was
        # configured, not by whether a fetch has landed yet.  State carries no
        # gateway marker until the first RPC returns, so trusting the marker
        # alone made a gateway client fall through to the native branch below
        # and advertise a local daemon it never runs.
        gateway_mode = ((isinstance(gateway_info, dict)
                         and gateway_info.get('mode') == 'gateway')
                        or _gateway_mode())
        if self._crash_looping:
            return "⚠ daemon crash-looping — run `atd status`"
        if not state.get('nodes'):
            if gateway_mode:
                if not isinstance(gateway_info, dict):
                    # First fetch still in flight; nothing has failed yet.
                    return "connecting to login gateway…"
                reason = ' '.join(str(
                    gateway_info.get('last_error') or
                    'no login gateway is reachable').split())[:100]
                return f"⚠ gateway unavailable · {reason}"
            if self._recovery_inflight:
                return "starting daemon…"
            return "waiting for daemon… (run `atd status` to inspect)"
        stale = self._daemon_age_seconds(
            updated, state.get('updated_monotonic'),
            state.get('monotonic_clock_id'))
        if stale is None:
            if gateway_mode:
                return "⚠ remote daemon state timestamp unavailable"
            return "⚠ daemon state timestamp unavailable · run `atd status`"
        if stale is not None and stale > 30:
            if gateway_mode:
                cached = ' · cached' if gateway_info.get('cached') else ''
                via = f' via {gateway_active}' if gateway_active else ''
                return f"⚠ remote daemon stale ({fmt_age(stale)}){via}{cached}"
            return f"⚠ daemon stale ({fmt_age(stale)} old) · run `atd status`"
        # Count real sessions, not offline/start-shell placeholder rows.
        real = sum(1 for r in rows if r[1] not in (
            _OFFLINE_SESSION, _START_SHELL_SESSION))
        # Surface keep-alive renew/pause here too — during the renewal gap the
        # job has no node row, so the per-row marker isn't visible.
        ka = state.get('keepalive', {}) or {}
        if not isinstance(ka, dict):
            ka = {}
        def status_label(identity, status):
            name = status.get('job_name') if isinstance(status, dict) else None
            job_id = (keepalive.job_family_id(status.get('job_id'))
                      if isinstance(status, dict) else None)
            label = name if isinstance(name, str) and name else str(identity)
            return f'{label}#{job_id}' if job_id else label

        paused = [status_label(identity, status)
                  for identity, status in ka.items()
                  if isinstance(status, dict) and status.get('state') == 'paused']
        renewing = [status_label(identity, status)
                    for identity, status in ka.items()
                    if isinstance(status, dict) and status.get('state') == 'renewing']
        extra = ''
        health_warning = ''
        health = state.get('keepalive_health', {}) or {}
        if getattr(self, '_ka_entries', None) and isinstance(health, dict):
            if health.get('enabled') is False:
                health_warning = '⚠ keep-alive disabled in config'
            else:
                error = ' '.join(str(health.get('last_error') or '').split())[:80]
                interval = health.get('interval', 30)
                if not isinstance(interval, (int, float)) or isinstance(interval, bool):
                    interval = 30
                stale_after = max(90.0, float(interval) * 3)
                success_age = self._daemon_age_seconds(
                    '', health.get('last_success_monotonic'),
                    state.get('monotonic_clock_id'))
                attempt_age = self._daemon_age_seconds(
                    '', health.get('last_attempt_monotonic'),
                    state.get('monotonic_clock_id'))
                if error:
                    health_warning = f'⚠ keep-alive check failed: {error}'
                elif (health.get('in_progress')
                      and attempt_age is not None and attempt_age > stale_after):
                    health_warning = (
                        f'⚠ keep-alive check stalled ({fmt_age(attempt_age)})')
                elif success_age is not None and success_age > stale_after:
                    health_warning = (
                        f'⚠ keep-alive checks stale ({fmt_age(success_age)})')
        if health_warning:
            extra = f' · {health_warning}'
        elif paused:
            extra = f" · ⚠ keep-alive PAUSED: {', '.join(sorted(paused))}"
        elif renewing:
            extra = f" · ⟳ renewing: {', '.join(sorted(renewing))}"
        recovering = []
        for node, item in (state.get('nodes', {}) or {}).items():
            network_info = item.get('network') if isinstance(item, dict) else None
            if (isinstance(network_info, dict)
                    and network_info.get('state') in {
                        'suspect', 'offline', 'half-open'}):
                retry = network_info.get('retry_in', 0)
                if not isinstance(retry, (int, float)) or isinstance(retry, bool):
                    retry = 0
                recovering.append(f'{node}({max(0, int(math.ceil(retry)))}s)')
        if recovering:
            extra += f" · ⚠ network recovery: {', '.join(sorted(recovering))}"
        if gateway_mode:
            via = gateway_active or 'selecting…'
            gateway_items = gateway_info.get('items', [])
            if not isinstance(gateway_items, list):
                gateway_items = []
            item = next((entry for entry in gateway_items
                         if isinstance(entry, dict)
                         and entry.get('name') == gateway_active), {})
            latency = item.get('latency_ms') if isinstance(item, dict) else None
            latency_text = (f' {float(latency):.0f}ms'
                            if isinstance(latency, (int, float)) else '')
            extra += f" · gateway {via}{latency_text}"
            extra += _gateway_health_note(gateway_items)
            extra += _cluster_health_note(gateway_info.get('clusters'))
            agent_version = gateway_info.get('agent_version')
            if (isinstance(agent_version, str) and agent_version
                    and agent_version != __version__):
                extra += (f" · ⚠ agent {agent_version} != client "
                          f"{__version__}")
            if gateway_info.get('cached'):
                reason = ' '.join(str(
                    gateway_info.get('last_error') or 'transport unavailable'
                ).split())[:80]
                extra += f" · ⚠ cached: {reason}"
        return f"{real} sessions · updated {updated}{extra}"

    async def _prewarm_interactive_async(
            self, nodes: tuple[str, ...], source_pool) -> None:
        """Pre-establish native SSH transports without allocating a PTY."""
        def warm() -> None:
            if source_pool is not None:
                for node in nodes:
                    if source_pool is not _GATEWAY_POOL:
                        return
                    source_pool.prewarm_interactive(node)
                return

            for node in nodes:
                if _GATEWAY_POOL is not None:
                    return
                now = time.monotonic()
                # Cancelling a Textual worker cannot stop a subprocess already
                # running in its executor thread. A slow handshake may outlive
                # several UI refreshes, so guard it independently and never
                # launch duplicate SSH attempts for the same node.
                with self._interactive_prewarm_lock:
                    if (node in self._interactive_prewarming
                            or self._interactive_prewarm_retry.get(
                                node, 0.0) > now):
                        continue
                    if os.path.exists(_interactive_ctl_path(node)):
                        self._interactive_prewarm_retry.pop(node, None)
                        continue
                    self._interactive_prewarming.add(node)
                with _SSH_SETTINGS_LOCK:
                    timeout = int(_SSH_SETTINGS['connect_timeout']) + 5
                argv = [
                    'ssh', *_get_ssh_args(node, interactive=True),
                    '-o', 'StrictHostKeyChecking=accept-new',
                    '-T', node, 'true',
                ]
                ok = False
                try:
                    result = subprocess.run(
                        argv, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=timeout)
                    ok = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    ok = False
                finally:
                    with self._interactive_prewarm_lock:
                        self._interactive_prewarming.discard(node)
                        if ok:
                            self._interactive_prewarm_retry.pop(node, None)
                        else:
                            self._interactive_prewarm_retry[node] = (
                                time.monotonic() + 300.0)

        try:
            await _offload(warm)
        except Exception:
            return

    async def action_refresh_table(self) -> None:
        await self._refresh_async()
        await self._reload_snapshots_async()

    # ── jobs panel (bottom) ──────────────────────────────────────────────────

    def _refresh_jobs(self, state=None) -> None:
        if state is None:
            state = read_state()
        if not isinstance(state, dict):
            state = {}
        if self.jobs_view_mode == 'pending':
            text = str(state.get('squeue_pending', '') or '')
            title = ('pending jobs', 'squeue --start')
        else:
            text = str(state.get('squeue_long', '') or '')
            title = ('queue', 'squeue -l')
        text = _dedent_block(text)
        if not text.strip():
            text = '(no squeue data yet — daemon may still be starting)'
        updated = state.get('squeue_updated', '?')
        age = self._daemon_age_seconds(
            updated, state.get('squeue_updated_monotonic'),
            state.get('monotonic_clock_id'))
        # How old, not when. An absolute timestamp and a relative age said the
        # same thing twice in a title that is already the widest line in the
        # panel -- and "updated 2026-01-01 00:00:00" is 19 columns spent on
        # the form of the answer nobody was asking for. The question here is
        # only ever "is this current", which is an age.
        if age is None:
            when = '  timestamp unavailable' if updated != '?' else ''
        elif age > 90:
            when = f'  ⚠ {fmt_age(age)} old'
        else:
            when = f'  {fmt_age(age)} old'
        head = _heading(title[0], (title[1] + when).strip(), 'j: switch')
        self.jobs_view.update(head + rich.text.Text('\n' + text))

    async def action_toggle_jobs_view(self) -> None:
        self.jobs_view_mode = 'pending' if self.jobs_view_mode == 'long' else 'long'
        # The timer maintains a last-good in-memory state. Switching views must
        # stay instant even if the runtime filesystem is temporarily stuck.
        self._refresh_jobs(self._last_state)

    # ── layout modes (z) ─────────────────────────────────────────────────────

    def _preview_visible(self) -> bool:
        """Whether the live pane is on screen under the current layout."""
        return bool(layout_spec(getattr(self, 'layout_mode', None))['preview'])

    def _room_for_preview(self) -> bool:
        """Whether the screen can afford the live pane beside the table.

        Measured on a phone: at 58 columns the table gets 56% of them, which
        is 32 -- so ``tu_improve`` renders as ``tu_impr`` and LEFT, LOAD and
        STATUS vanish entirely, while the 25 columns spent on the preview are
        too few to read anything in. The split view is a desktop layout, and
        below this width it costs the table everything and buys nothing.
        """
        return self.size.width >= _MIN_SPLIT_WIDTH

    # ── publishing what you can do (touch clients) ───────────────────────

    def _publish_keys(self, mode: str = 'app') -> None:
        """Hand a browser client the bindings that are live right now.

        ``active_bindings`` already knows them per screen and per focus, so a
        modal changes the buttons on the phone without anything here listing
        what a modal offers. That is the whole point: the copy of this list
        that used to live in javascript could only ever be right on the day
        it was written.

        Silent unless a client said it can draw them -- see keypad.touch_mode.
        """
        if getattr(self, '_touch_mode', '') != 'web':
            return
        keys = (keypad.EXTERNAL_KEYS if mode == 'external'
                else keypad.keys_for(self.active_bindings))
        # Sent in both modes, not only at the handover. The client builds its
        # tmux chords from it, and building them right only once you have
        # already attached is one frame too late.
        payload = keypad.encode(mode, keys, keypad.tmux_prefix())
        if payload == getattr(self, '_published_keys', None):
            return
        driver = getattr(self, '_driver', None)
        if driver is None:
            return
        try:
            driver.write(payload)
            driver.flush()
        except Exception:
            # A dashboard that dies because a phone could not be told about a
            # button is a worse outcome than a phone with stale buttons.
            return
        self._published_keys = payload

    async def _sync_touch_bar(self) -> None:
        """Same poll, same reason: bindings change and the bar has to follow."""
        bar = getattr(self, '_touch_bar', None)
        if bar is not None:
            try:
                await bar.rebuild(self.active_bindings)
            except Exception:
                # A dashboard that dies because a button could not be redrawn
                # is worse than a bar showing the previous screen's buttons.
                return

    @contextmanager
    def suspend(self):
        """Hand the screen to a raw program, and say so.

        Past this point the terminal belongs to tmux or a shell. Neither can
        draw a button or answer a question about its own keys, so the client
        gets the one static set that situation actually calls for -- and gets
        the app's own back the moment the app is drawing again.
        """
        self._publish_keys('external')
        try:
            with super().suspend():
                yield
        finally:
            # Force a re-send: the app's payload is very likely the one that
            # was published before the handover, and unchanged payloads are
            # suppressed.
            self._published_keys = None
            self._publish_keys()

    def _apply_layout(self) -> None:
        """Show the panes this mode calls for. Idempotent."""
        spec = layout_spec(self.layout_mode)
        upper = self.query_one('#upper')
        table = self.query_one('#left_pane')
        preview = self.query_one('#right_pane_scroll')
        jobs = self.query_one('#jobs_scroll')
        was_previewing = bool(preview.display)
        # Captured before anything is shown or hidden: "the table arrived" is
        # a transition, and after the assignments below there is nothing left
        # to compare against.
        was_table = bool(table.display)
        # Not a mode of its own: `z` still cycles the same four, and split
        # simply looks like wide until there is room. A narrow terminal is a
        # property of the screen, not a choice to be remembered.
        show_preview = spec['preview'] and self._room_for_preview()

        # #upper is 1fr high: leaving it displayed with both children hidden
        # would reserve the whole body to draw nothing in. The queue lives in
        # the left column now, so it counts towards that column being worth
        # showing.
        column = self.query_one('#left_column')
        show_left = spec['table'] or spec['jobs']
        upper.display = show_left or show_preview
        column.display = show_left
        table.display = spec['table']
        preview.display = show_preview
        column.set_class(not show_preview, '-full')
        jobs.display = spec['jobs']
        jobs.set_class(spec['expand_jobs'], '-full')
        table_arrived = bool(spec['table']) and not was_table

        # Focus follows what is visible, or the arrow keys steer a widget
        # nobody can see.
        #
        # Both scrollers were taken out of the focus chain to stop a stray Tab
        # or click landing there and leaving the arrow keys scrolling a
        # preview while the reader was trying to move the session cursor. That
        # fixed the wrong half: it made the panes unreachable instead of
        # making the focus visible. Over SSH mouse reporting is off by default
        # -- which is what this tool is for -- so the wheel was not a fallback
        # either, and the preview and the queue simply could not be scrolled
        # by any means. Measured: fourteen different keys, 575 hidden rows of
        # preview, nothing moved.
        #
        # So they are focusable whenever they are on screen, and which one has
        # the keyboard is drawn (see the :focus rules) rather than left to be
        # inferred from what the arrow keys just did.
        preview.can_focus = bool(show_preview)
        jobs.can_focus = bool(spec['jobs'])
        # Never leave the keyboard pointed at a pane that just left the
        # screen: the keys would go nowhere and the way back is a key nobody
        # would think to press.
        focused = self.focused
        if focused is not None and not getattr(focused, 'display', True):
            focused = None
        elif focused in (preview, jobs) and not focused.can_focus:
            focused = None
        # Otherwise leave it where it is. Moving the keyboard under someone
        # who did not ask is the same fault as not letting them move it, and
        # `z` fires this on every resize as well as every layout change.
        #
        # The exception is the table arriving: it was not on screen, now it
        # is, and it is what a person opens this to read. Coming back from
        # the queue-only view should not leave the arrows in the queue.
        if table_arrived or focused is None:
            if spec['table'] and getattr(self, 'table', None) is not None:
                self.table.focus()
            elif spec['expand_jobs']:
                jobs.focus()

        # Coming back from a mode that suppressed live fetches, the pane still
        # holds whatever it last drew, with nothing to say how old that is.
        # The cached snapshot is stale too, but it says so.
        if show_preview and not was_previewing:
            self._show_cached_snapshot(self.selected_node, self.selected_session)

    async def _on_resize(self, event) -> None:
        """Re-apply the layout when the screen changes shape.

        A phone rotating, or a software keyboard opening, changes what fits,
        and landscape is wide enough for the preview that portrait is not.
        Overriding the private handler because Textual's own calls
        ``event.stop()`` before any public ``on_resize`` could see it; the
        refresh tick re-applies the layout too, so if this ever stops being
        called the view corrects itself within one tick rather than not at
        all.
        """
        await super()._on_resize(event)
        try:
            self._apply_layout()
        except Exception:
            pass

    async def action_web_dashboard(self) -> None:
        """Show where the browser dashboard is, without anyone memorising it."""
        try:
            state = await _offload_for(8.0, webcontrol.describe)
        except Exception as error:
            self.notify(f'could not read the web dashboard state · {error}',
                        severity='error', timeout=6, markup=False)
            return
        try:
            await self.push_screen(WebDashboardScreen(state))
        except Exception as error:
            self.notify(f'could not open the web dashboard · {error}',
                        severity='error', timeout=6, markup=False)

    # ── what the footer shows ────────────────────────────────────────────
    # Five, not eight. The current writing settles on three to five in the
    # footer and everything in `?`, and eight of twenty-one was neither.
    #
    # Done by demoting keys rather than by check_action. False there means
    # disabled *and* hidden -- there is no "works but is not advertised" --
    # so filtering the footer by focus took `z`, `s` and `g` out of service
    # whenever the keyboard was on the list. A key that stops working
    # because you looked at another pane is a worse bug than a long footer.
    #
    # Demoted keys lose nothing but the advertisement: they still work from
    # anywhere, `?` lists all twenty-one, and the panes that own a key still
    # get it in their own footer through check_action below.

    def check_action(self, action: str, parameters) -> bool | None:
        """Whether a binding applies where the keyboard currently is.

        Only ever used to retire a key that would do nothing here -- never to
        tidy the footer, because False disables as well as hides.
        """
        if action == 'focus_table':
            # `esc` goes back to the list, and there is no back from the
            # list. Everywhere else it is the way out.
            return getattr(self.focused, 'id', None) != 'left_pane'
        return True

    # ── which pane the keyboard is pointed at ────────────────────────────
    # Tab cycles; the digits are for going straight there, which is what you
    # want the second time. Both only ever land on a pane that is on screen,
    # because the focus chain is rebuilt from the layout (see _apply_layout).
    # Numbered in the order Tab walks them, which is the order the grid
    # reads: the left column top to bottom, then the column beside it. They
    # disagreed for one commit -- Tab went list, queue, preview while the
    # digits went list, preview, queue -- and a key that lands somewhere Tab
    # would not is worse than no key.
    _PANES = {'table': '#left_pane', 'jobs': '#jobs_scroll',
              'preview': '#right_pane_scroll'}

    def action_focus_pane(self, name: str) -> None:
        selector = self._PANES.get(name)
        if selector is None:
            return
        try:
            pane = self.query_one(selector)
        except Exception:
            return
        # Saying why is the point. Silence here reads as a dead key, which is
        # the complaint this whole section exists to answer.
        if not pane.display:
            self.notify(f'the {name} pane is not in this layout · z to cycle',
                        timeout=2, markup=False)
            return
        if not pane.can_focus:
            return
        pane.focus()

    # ── narrowing the list ───────────────────────────────────────────────
    def action_filter_sessions(self) -> None:
        """Open the filter, or put the cursor back in it if it is open."""
        try:
            box = self.query_one('#filter', FilterInput)
        except Exception:
            return
        box.can_focus = True
        box.add_class('-on')
        box.focus()

    def _close_filter(self, keep: bool) -> None:
        """Put the keyboard back on the list.

        `keep` is the difference between Enter and Escape: one is "that is
        the list I wanted", the other is "put them all back". A filter that
        survived Escape would be invisible state -- the bar is gone and the
        sessions are missing, which reads as sessions having gone missing.
        """
        try:
            box = self.query_one('#filter', FilterInput)
        except Exception:
            return
        if not keep:
            box.value = ''
            self._filter = ''
            self._invalidate_rows()
            self._refresh_table(self._last_state)
        box.remove_class('-on')
        box.can_focus = False
        self.action_focus_table()

    def _invalidate_rows(self) -> None:
        """Force the next refresh to rebuild rather than update in place."""
        self._last_rows_sig = None
        self._last_structural_sig = None

    async def on_input_changed(self, event) -> None:
        if getattr(event.input, 'id', '') != 'filter':
            return
        self._filter = event.value
        self._invalidate_rows()
        self._refresh_table(self._last_state)

    async def on_input_submitted(self, event) -> None:
        if getattr(event.input, 'id', '') == 'filter':
            self._close_filter(keep=True)

    def action_focus_table(self) -> None:
        """The way back, from anywhere.

        Every pane you can steer into needs one key that returns the arrows to
        the list without your having to remember which direction you came
        from -- otherwise focus is a maze, which is what made it worth
        removing the first time.

        The filter has its own escape, on the widget: see FilterInput.
        """
        table = getattr(self, 'table', None)
        if table is not None and table.display:
            table.focus()

    async def action_cycle_layout(self) -> None:
        self.layout_mode = config.next_layout(self.layout_mode)
        self._apply_layout()
        # Remembering it is the point: the mode is chosen to fit a terminal,
        # and that terminal is usually the same one tomorrow.
        config.save_layout(self.layout_mode)
        label = layout_spec(self.layout_mode)['label']
        if layout_spec(self.layout_mode)['preview'] and not self._room_for_preview():
            label += ' · too narrow for the preview'
        self.notify(f'Layout: {label}', timeout=2, markup=False)

    # ── persistent snapshot cache ────────────────────────────────────────────

    def _reload_snapshots(self) -> None:
        ok, snapshots = _read_snapshots_checked()
        if ok:
            self.snapshots = snapshots

    async def _reload_snapshots_async(self, render_loading: bool = False) -> None:
        # Filesystem could be slow / NFS-y; never block the event loop on it.
        source_pool = _GATEWAY_POOL
        try:
            ok, snapshots = await _offload_for(
                _UI_FILE_READ_TIMEOUT, _read_snapshots_checked)
        except Exception:
            return
        if source_pool is not _GATEWAY_POOL:
            return
        if ok:
            self.snapshots = snapshots
            if (render_loading and self.selected_node and self.selected_session
                    and str(self.log_view.render()).startswith('Loading preview')):
                self._render_preview_now()

    def _preview_title(self, node: str, session: str,
                       note: str = 'live') -> 'rich.text.Text':
        """What this pane is showing, and how to steer it.

        It showed a screenful of someone else's output with nothing at all to
        say whose it was -- the only clue was the table cursor beside it, and
        that clue got weaker the moment the keyboard could move into this
        pane and the cursor dimmed. The queue below has carried its own
        title, and its own key, all along; this is the same thing, so the two
        secondary panes read as siblings rather than as one titled panel and
        one anonymous slab.

        `2: scroll` for the same reason the queue prints `j: switch`: a key
        belongs next to the thing it acts on -- but only while it fits. This
        pane is the narrow one, and a heading that wraps onto a second line
        pushes the output down and stops looking like a heading at all. The
        hint is the part worth losing: the name is what the pane is for.
        """
        title = f'{node}:{session}'
        try:
            room = int(self.query_one('#right_pane_scroll').size.width) - 2
        except Exception:
            room = 0
        key = '3: scroll'
        if room and len(title) + len(note) + len(key) + 9 > room:
            key = ''
        return _heading(title, note, key) + rich.text.Text('\n')

    def _show_cached_snapshot(self, node: str, session: str) -> bool:
        snap = self.snapshots.get(f'{node}:{session}')
        if not isinstance(snap, dict):
            return False
        text = snap.get('lines') or ''
        ts = snap.get('ts', '?')
        if not isinstance(text, str) or not text:
            return False
        if not isinstance(ts, (str, int, float)):
            ts = '?'
        ts = str(ts)
        age = _snapshot_age_seconds(snap)
        age_bucket = None if age is None else int(age // 10)
        cache_key = (node, session, ts, age_bucket)
        # LRU: pop on hit and reinsert so it moves to the end of insertion order.
        rendered = self._rendered_cache.pop(cache_key, None)
        if rendered is None:
            base = rich.text.Text.from_ansi(text)
            warn = '⚠ ' if age is not None and age >= 60 else ''
            note = 'age unknown' if age is None else f'{warn}{fmt_age(age)} old'
            rendered = self._preview_title(
                node, session, f'cached · {note}') + base
        self._rendered_cache[cache_key] = rendered
        # Keep cache bounded — evict the LEAST recently used (head of dict).
        if len(self._rendered_cache) > 64:
            first_key = next(iter(self._rendered_cache))
            del self._rendered_cache[first_key]
        self.log_view.update(rendered)
        return True

    # ── row selection ────────────────────────────────────────────────────────

    def _row_target(self, row_key):
        """(node, session) for a DataTable row_key, or None if it can't be
        resolved (stale key mid-refresh, header, out-of-range, etc.)."""
        if row_key is None:
            return None
        try:
            idx = self.table.get_row_index(row_key)
        except Exception:
            return None
        if not (0 <= idx < len(self.row_targets)):
            return None
        row = self.row_targets[idx]
        # A band heading holds a slot so the list stays index-for-index with
        # the table, and it is not a target: attach, preview and `k` must all
        # decline rather than act on whatever row happens to be adjacent.
        if row is None:
            return None
        return row[0], row[1]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        target = self._row_target(event.row_key)
        if target is None:
            return
        new_node, new_sess = target
        # Skip when the highlight event is just from a refresh that landed
        # on the same row — clearing here is what caused the 5s preview flicker.
        if (new_node, new_sess) == (self.selected_node, self.selected_session):
            return
        self.selected_node = new_node
        self.selected_session = new_sess
        self._selection_changed_at = time.monotonic()
        # Coalesce bursts of ↑/↓ into a single repaint. The cursor moves
        # immediately (a tiny redraw); the right-pane preview — a whole
        # screenful of text, expensive to ship over a remote terminal — is
        # deferred until navigation settles, so holding ↓ doesn't repaint the
        # preview pane on every step.
        if self._preview_render_timer is not None:
            self._preview_render_timer.stop()
        self._preview_render_timer = self.set_timer(0.2, self._render_preview_now)

    def _render_preview_now(self) -> None:
        self._preview_render_timer = None
        node, sess = self.selected_node, self.selected_session
        if not node or not sess:
            return
        if sess in (_START_SHELL_SESSION, _OFFLINE_SESSION):
            self.log_view.update("")
            return
        if not self._show_cached_snapshot(node, sess):
            # Titled like every other state of this pane, so the heading does
            # not appear and disappear as the first snapshot lands.
            self.log_view.update(self._preview_title(node, sess, 'connecting…'))

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Emitted by Enter and by a single mouse click (see
        # ClickToAttachDataTable). Resolve the target straight from the event
        # so the attach can't race a not-yet-processed RowHighlighted.
        target = self._row_target(event.row_key)
        if target is None:
            return
        self.selected_node, self.selected_session = target
        await self.action_attach_session()

    # ── live preview (the ONLY network call in the frontend) ─────────────────

    async def _spawn_preview_capture(self, node: str, sess: str):
        """Request a preview from the daemon's node-level SSH coordinator."""
        if not _valid_node(node):
            raise ValueError(f'invalid preview node: {node!r}')
        if _GATEWAY_POOL is not None:
            return await _offload_for(
                float(_GATEWAY_POOL.settings['state_timeout']) + 1.0,
                _GATEWAY_POOL.preview, node, sess)
        return await _offload_for(
            _PREVIEW_CAPTURE_TIMEOUT + 4.0,
            ipc.request, PREVIEW_SOCKET,
            {'action': 'preview', 'node': node, 'session': sess},
            _PREVIEW_CAPTURE_TIMEOUT + 3.0,
        )

    @staticmethod
    async def _stop_async_process(proc) -> None:
        """Kill and reap an asyncio subprocess without an unbounded wait."""
        if proc is None:
            return
        try:
            if getattr(proc, 'returncode', None) is None:
                try:
                    proc.kill()
                except OSError:
                    # It may have exited between returncode and kill; wait()
                    # still owns the reap and must run below.
                    pass
            await asyncio.wait_for(proc.wait(), timeout=2)
        except (Exception, asyncio.CancelledError):
            # Cleanup is best-effort even while the containing worker is being
            # cancelled; the caller still re-raises cancellation afterwards.
            return

    async def _preview_loop(self) -> None:
        """Request daemon-coordinated previews for the highlighted row.

        Navigation is debounced and unchanged panes back off exponentially.
        The daemon owns SSH process cleanup and the shared per-node channel
        budget, so multiple frontends cannot independently flood one master.
        """
        last_key = ""
        last_hash = ""
        active_key = ""
        unchanged_streak = 0
        next_probe_at = 0.0
        # Local IPC failures are backed off per node. Remote failures use the
        # daemon's shared node-level circuit, so switching sessions or opening
        # another frontend cannot reset it.
        timeout_counts: dict = {}
        backoff_until: dict = {}
        import time as _time
        while True:
            # Outer guard: one bad iteration (anything outside the fetch
            # block too) must never kill the preview worker for the rest of
            # the session — Textual does not restart a dead worker.
            try:
                await asyncio.sleep(_PREVIEW_LOOP_TICK)
                # Drop expired backoff so the dicts can't grow without bound over
                # a long session AND a reused (node,sess) name doesn't inherit a
                # stale 60s backoff from a since-departed job.
                _now = _time.monotonic()
                for k in [k for k, t in backoff_until.items() if t <= _now]:
                    backoff_until.pop(k, None)
                    timeout_counts.pop(k, None)
                # A layout that hides the pane should also stop paying for it:
                # no capture, no SSH round trip per tick. Clearing active_key
                # makes the first tick after it reappears fetch immediately
                # instead of resuming a backed-off cadence.
                if not self._preview_visible():
                    active_key = ""
                    unchanged_streak = 0
                    continue
                node = self.selected_node
                sess = self.selected_session
                if (not node or not sess
                        or sess in (_START_SHELL_SESSION, _OFFLINE_SESSION)):
                    active_key = ""
                    unchanged_streak = 0
                    continue
                key = f"{node}:{sess}"
                if key != active_key:
                    active_key = key
                    unchanged_streak = 0
                    next_probe_at = 0.0
                # Skip if the user is still navigating quickly.
                if _time.monotonic() - getattr(self, '_selection_changed_at', 0) < 0.5:
                    continue
                if backoff_until.get(node, 0) > _time.monotonic():
                    continue
                if _time.monotonic() < next_probe_at:
                    continue

                try:
                    source_pool = _GATEWAY_POOL
                    response = await self._spawn_preview_capture(node, sess)
                    if source_pool is not _GATEWAY_POOL:
                        continue
                    if not isinstance(response, dict):
                        raise RuntimeError('invalid preview service response')
                    if not response.get('ok'):
                        reason = ' '.join(str(
                            response.get('reason') or
                            'preview temporarily unavailable').split())[:160]
                        retry_after = max(
                            0.25, min(60.0, float(
                                response.get('retry_after') or 1.0)))
                        next_probe_at = _time.monotonic() + retry_after
                        if response.get('kind') in {'backoff', 'unavailable'}:
                            backoff_until[node] = next_probe_at
                            self._report_preview_backoff(
                                node, sess, reason, retry_after=retry_after)
                        elif not self._show_cached_snapshot(node, sess):
                            self.log_view.update(
                                self._preview_title(node, sess, 'waiting')
                                + rich.text.Text(reason))
                        continue
                    content = response.get('content')
                    if not isinstance(content, str):
                        raise RuntimeError('preview response has no text')
                    timeout_counts.pop(node, None)
                    backoff_until.pop(node, None)

                    if (node, sess) != (self.selected_node, self.selected_session):
                        continue
                    # This is only a repaint fingerprint, but SHA-256 avoids
                    # normalising an insecure hash primitive in security scans.
                    h = hashlib.sha256(content.encode()).hexdigest()
                    if key == last_key and h == last_hash:
                        unchanged_streak, delay = _next_preview_cadence(
                            True, unchanged_streak)
                        next_probe_at = _time.monotonic() + delay
                        continue
                    unchanged_streak, delay = _next_preview_cadence(
                        False, unchanged_streak)
                    next_probe_at = _time.monotonic() + delay
                    last_key, last_hash = key, h
                    self.log_view.update(
                        self._preview_title(node, sess)
                        + rich.text.Text.from_ansi(content))
                except asyncio.TimeoutError:
                    n = timeout_counts.get(node, 0) + 1
                    timeout_counts[node] = n
                    next_probe_at = (
                        _time.monotonic() + _PREVIEW_CHANGED_DELAY)
                    # After 2 consecutive timeouts, stop probing this session
                    # for 60s — the remote is too overloaded to be useful.
                    if n >= 2:
                        backoff_until[node] = _time.monotonic() + 60
                        self._report_preview_backoff(
                            node, sess, 'preview command timed out')
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    n = timeout_counts.get(node, 0) + 1
                    timeout_counts[node] = n
                    next_probe_at = (
                        _time.monotonic() + _PREVIEW_CHANGED_DELAY)
                    if n >= 2:
                        backoff_until[node] = _time.monotonic() + 60
                        detail = ' '.join(str(error).split())[:160]
                        self._report_preview_backoff(
                            node, sess, detail or 'preview command failed')
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    # ── interactive actions ──────────────────────────────────────────────────

    def _report_preview_backoff(self, node: str, sess: str,
                                reason: str,
                                retry_after: float = 60.0) -> None:
        """Replace a stuck loading message when live preview backs off."""
        if (node, sess) != (self.selected_node, self.selected_session):
            return
        seconds = max(1, int(math.ceil(retry_after)))
        self.notify(
            f'live preview unavailable for {node}:{sess} · retrying in {seconds}s · {reason}',
            severity='warning', timeout=7, markup=False)
        # Preserve a useful cached preview when one exists. Otherwise make
        # the backoff visible instead of leaving "Loading…" for a full minute.
        if not self._show_cached_snapshot(node, sess):
            self.log_view.update(
                f'Live preview unavailable ({reason}). Retrying in {seconds}s.')

    def _report_command_result(self, label: str, returncode: int,
                               error: str = '') -> None:
        """Keep an interactive command failure visible after screen redraw."""
        if error:
            self.notify(f'{label} failed · {error}', severity='error',
                        timeout=7, markup=False)
        elif returncode not in (0, 130, -signal.SIGINT):
            self.notify(f'{label} exited with status {returncode}',
                        severity='warning', timeout=6, markup=False)

    async def action_attach_session(self) -> None:
        node, sess = self.selected_node, self.selected_session
        if not node or not sess:
            return
        if sess == _OFFLINE_SESSION:
            self.notify(f'{node} is offline · press s to retry a shell or r to refresh',
                        severity='warning', timeout=6, markup=False)
            return
        if node != 'localhost' and not _valid_node(node):
            self.notify(f'invalid node name: {node!r}', severity='error',
                        timeout=5, markup=False)
            return
        if node != 'localhost' and os.environ.get('TMUX'):
            helper_args = (
                ['--shell', node]
                if sess == _START_SHELL_SESSION
                else ['--attach', f'{node}:{sess}']
            )
            if _handoff_outer_tmux_client(helper_args):
                return
        network_outcome = None
        network_reason = ''
        direct_preferred = (False if _gateway_mode() else
                            _node_network_degraded(self._last_state, node))
        # If we're inside tmux and about to nest another tmux (any real session,
        # local or remote), step the outer tmux aside for the duration of the
        # attach so the INNER session receives the C-b prefix instead of the
        # outer one swallowing it (see _tmux_step_aside). F12 is the emergency
        # restore key. The restore lives in `finally` so we always
        # hand the prefix back even if the attach raises or the proxy dies.
        nest = _will_nest_tmux(sess)
        returncode = 0
        command_error = ''
        step_ok = True
        restore_ok = True
        with self.suspend():
            # Say what is about to own the terminal, and how to get back.
            # Handing it over silently makes a finished session
            # indistinguishable from a hung dashboard: the table vanishes, a
            # run that ended hours ago paints one static screen, and nothing
            # on it names what you are looking at or which key returns.
            print(_handover_banner(node, sess), flush=True)
            if nest:
                step_ok = _tmux_step_aside()
                if not step_ok:
                    print("\n[atmux] could not enable outer-tmux passthrough; "
                          "use the outer prefix twice for the inner tmux.")
            try:
                if node == 'localhost':
                    if sess == _START_SHELL_SESSION:
                        returncode, command_error = _run_user_command(
                            [os.environ.get('SHELL') or '/bin/bash'])
                    else:
                        with urlhandler.attached(node, sess):
                            returncode, command_error = _run_user_command(
                                _local_attach_argv(sess))
                else:
                    if direct_preferred:
                        print(f"\n[atmux] {node} is in network recovery; "
                              "opening a fresh SSH connection.", flush=True)
                    remote_args = None if sess == _START_SHELL_SESSION else [
                        'tmux', 'attach', '-t', shlex.quote(sess)]
                    with urlhandler.attached(node, sess):
                        returncode, command_error, _used_direct = (
                            _run_remote_user_command(
                                node, remote_args, direct=direct_preferred))
                    if returncode == 255:
                        network_outcome = 'failure'
                        network_reason = command_error or 'interactive SSH failed'
                    elif returncode != 127 or not command_error:
                        # Any remote exit status other than SSH's transport
                        # status 255 proves the network path worked.
                        network_outcome = 'success'
            finally:
                if nest:
                    restore_ok = _tmux_restore()
        self._report_command_result(
            f'attach {node}:{_session_label(sess)}', returncode, command_error)
        if network_outcome:
            _report_network_event(
                node, network_outcome, network_reason, 'interactive-attach')
        if nest and not restore_ok:
            self.notify('outer tmux restore failed · press F12 to recover it',
                        severity='error', timeout=10, markup=False)
        elif nest and not step_ok:
            self.notify('outer tmux passthrough was unavailable',
                        severity='warning', timeout=6, markup=False)

    async def action_view_pane(self) -> None:
        node, sess = self.selected_node, self.selected_session
        if not node or not sess or sess in (_OFFLINE_SESSION,
                                            _START_SHELL_SESSION):
            self.notify('select a real session to read',
                        severity='warning', timeout=4, markup=False)
            return
        self.notify(f'reading {_session_label(sess)} …', timeout=3,
                    markup=False)
        try:
            response = await self._fetch_pane(
                node, sess, config.PREVIEW_HISTORY_MAX)
        except Exception as error:
            response = {'ok': False,
                        'reason': ' '.join(str(error).split())[:160]}
        if not isinstance(response, dict) or not response.get('ok'):
            reason = ' '.join(str(
                (response or {}).get('reason') or 'unavailable').split())[:160]
            self.notify(f'could not read {_session_label(sess)}: {reason}',
                        severity='error', timeout=9, markup=False)
            return
        content = response.get('content') or ''
        self.push_screen(PaneScreen(
            f'{node}:{_session_label(sess)}',
            rich.text.Text.from_ansi(str(content))))

    async def _fetch_pane(self, node: str, session: str, history: int):
        if _GATEWAY_POOL is not None:
            return await _offload_for(
                float(_GATEWAY_POOL.settings['state_timeout']) + 4.0,
                _GATEWAY_POOL.preview, node, session, history)
        return await _offload_for(
            _PREVIEW_CAPTURE_TIMEOUT + 6.0, ipc.request, PREVIEW_SOCKET,
            {'action': 'preview', 'node': node, 'session': session,
             'history': int(history)}, _PREVIEW_CAPTURE_TIMEOUT + 4.0)

    async def _session_command(self, node: str, session: str,
                               verb: str) -> tuple[bool, str]:
        """Ask whoever owns the node to kill or create a session."""
        if _GATEWAY_POOL is not None:
            response = await _offload_for(
                float(_GATEWAY_POOL.settings['state_timeout']) + 2.0,
                _GATEWAY_POOL.session_command, node, session, verb)
        else:
            response = await _offload_for(
                12.0, ipc.request, PREVIEW_SOCKET,
                {'action': 'session', 'node': node,
                 'session': session, 'verb': verb}, 10.0)
        if not isinstance(response, dict):
            return False, 'malformed reply'
        if response.get('ok'):
            return True, ''
        return False, ' '.join(
            str(response.get('reason') or 'command refused').split())[:160]

    async def action_kill_session(self) -> None:
        node, sess = self.selected_node, self.selected_session
        if not node or not sess or sess in (_OFFLINE_SESSION,
                                            _START_SHELL_SESSION):
            self.notify('select a real session to kill', severity='warning',
                        timeout=4, markup=False)
            return

        async def finish(confirmed) -> None:
            if not confirmed:
                return
            try:
                ok, why = await self._session_command(node, sess, 'kill')
            except Exception as error:
                ok, why = False, ' '.join(str(error).split())[:160]
            if ok:
                self.notify(f'killed {_session_label(sess)} on {node}',
                            timeout=5, markup=False)
                await self.action_refresh_table()
            else:
                self.notify(f'could not kill {_session_label(sess)}: {why}',
                            severity='error', timeout=9, markup=False)

        self.push_screen(
            ConfirmScreen(
                f'Kill {_session_label(sess)} on {node}?',
                'Everything running inside it is lost. This cannot be undone.'),
            lambda confirmed: self.run_worker(finish(confirmed)))

    async def action_new_session(self) -> None:
        node = self.selected_node
        if not node:
            return
        if node != 'localhost' and not _valid_node(node):
            self.notify(f'invalid node name: {node!r}', severity='error',
                        timeout=5, markup=False)
            return

        async def finish(name) -> None:
            if not name or not str(name).strip():
                return
            wanted = str(name).strip()
            try:
                ok, why = await self._session_command(node, wanted, 'new')
            except Exception as error:
                ok, why = False, ' '.join(str(error).split())[:160]
            if ok:
                self.notify(f'created {wanted} on {node}', timeout=5,
                            markup=False)
                await self.action_refresh_table()
            else:
                self.notify(f'could not create {wanted}: {why}',
                            severity='error', timeout=9, markup=False)

        self.push_screen(
            NoteScreen(f'new session on {node}', '',
                       prompt='session name', hint='letters, digits, _ @ + -'),
            lambda name: self.run_worker(finish(name)))

    async def action_edit_note(self) -> None:
        sess = self.selected_session
        if not sess or sess in (_OFFLINE_SESSION, _START_SHELL_SESSION):
            self.notify('notes attach to a real session',
                        severity='warning', timeout=4, markup=False)
            return

        def store(text) -> None:
            if text is None:                      # Esc
                return
            if config.save_note(sess, text):
                self._notes = config.load_notes()
                # The note is drawn into STATUS, so the cached row signature
                # no longer matches what should be on screen.
                self._last_rows_sig = None
                self._rendered_cache.clear()
                self._refresh_table(self._last_state)
            else:
                self.notify('could not save the note', severity='error',
                            timeout=5, markup=False)

        self.push_screen(NoteScreen(sess, self._notes.get(sess, '')), store)

    async def action_open_shell(self) -> None:
        node = self.selected_node
        if not node:
            return
        if node != 'localhost' and not _valid_node(node):
            self.notify(f'invalid node name: {node!r}', severity='error',
                        timeout=5, markup=False)
            return
        if (node != 'localhost' and os.environ.get('TMUX')
                and _handoff_outer_tmux_client(['--shell', node])):
            return
        returncode = 0
        command_error = ''
        network_outcome = None
        network_reason = ''
        direct_preferred = (False if _gateway_mode() else
                            _node_network_degraded(self._last_state, node))
        with self.suspend():
            if node == 'localhost':
                returncode, command_error = _run_user_command(
                    [os.environ.get('SHELL') or '/bin/bash'])
            else:
                returncode, command_error, _used_direct = (
                    _run_remote_user_command(
                        node, None, direct=direct_preferred))
                if returncode == 255:
                    network_outcome = 'failure'
                    network_reason = command_error or 'interactive SSH failed'
                elif returncode != 127 or not command_error:
                    network_outcome = 'success'
        self._report_command_result(
            f'shell on {node}', returncode, command_error)
        if network_outcome:
            _report_network_event(
                node, network_outcome, network_reason, 'interactive-shell')

    async def action_local_shell(self) -> None:
        # 'autotmux_local' is a tmux session, so inside tmux this nests too.
        nest = bool(os.environ.get('TMUX'))
        returncode = 0
        command_error = ''
        step_ok = True
        restore_ok = True
        with self.suspend():
            if nest:
                step_ok = _tmux_step_aside()
                if not step_ok:
                    print("\n[atmux] could not enable outer-tmux passthrough; "
                          "use the outer prefix twice for the inner tmux.")
            try:
                returncode, command_error = _run_user_command(
                    ['tmux', 'new-session', '-A', '-s', 'autotmux_local'])
            finally:
                if nest:
                    restore_ok = _tmux_restore()
        self._report_command_result('local tmux', returncode, command_error)
        if nest and not restore_ok:
            self.notify('outer tmux restore failed · press F12 to recover it',
                        severity='error', timeout=10, markup=False)
        elif nest and not step_ok:
            self.notify('outer tmux passthrough was unavailable',
                        severity='warning', timeout=6, markup=False)

    async def action_toggle_keepalive(self) -> None:
        """Toggle keep-alive auto-renew on the highlighted job. One keystroke,
        zero input: the launch script is auto-detected from `scontrol show job`.
        Toggling an already-registered job off just removes it."""
        node = self.selected_node
        if not node or node == 'localhost':
            self.notify('keep-alive needs a remote SLURM job',
                        severity='warning', timeout=4)
            return
        nodes = self._last_state.get('nodes', {}) if isinstance(self._last_state, dict) else {}
        nd = nodes.get(node, {}) if isinstance(nodes, dict) else {}
        info = nd.get('info', {}) if isinstance(nd, dict) else {}
        if not isinstance(info, dict):
            info = {}
        job_name = info.get('job_name')
        job_id = info.get('job_id')
        if (not isinstance(job_name, str) or not job_name
                or not isinstance(job_id, (str, int)) or str(job_id) == '-'):
            self.notify('no SLURM job found for this row', severity='warning', timeout=4)
            return
        raw_job_id = str(job_id)
        job_id = keepalive.job_family_id(raw_job_id)
        if job_id is None:
            self.notify(f'invalid SLURM job id: {raw_job_id!r}',
                        severity='warning', timeout=5, markup=False)
            return
        # A detection is already running for this job — ignore the repeat press
        # so two workers can't race to add-then-remove the same entry.
        inflight_key = f'job:{job_id}'
        if inflight_key in self._ka_inflight:
            self.notify('keep-alive update already in progress',
                        severity='information', timeout=3)
            return
        self._ka_inflight.add(inflight_key)
        # Read membership fresh off the loop (the stash may still be empty in the
        # first few seconds after launch, before the timer fills it).
        try:
            self._ka_entries = await _offload_for(
                _operation_timeout(8), self._ka_registry_entries, True)
            self._ka_names = {
                entry.get('job_name') for entry in self._ka_entries
                if isinstance(entry.get('job_name'), str)
            }
        except Exception as error:
            self._ka_inflight.discard(inflight_key)
            self.notify(f'could not read keep-alive registry: {error}',
                        severity='warning', timeout=5, markup=False)
            return
        # Already registered → toggle off (no scontrol needed). Registry write
        # is on NFS, so keep it off the event loop.
        existing = self._ka_find_entry(job_id, job_name)
        if existing is not None:
            try:
                existing_id = existing.get('entry_id')
                existing_job_id = keepalive.job_family_id(existing.get('job_id'))
                kwargs = ({'job_id': existing_job_id, 'entry_id': existing_id}
                          if existing_job_id else {})
                await _offload_for(
                    _operation_timeout(8), _set_keepalive_enabled,
                    config.KEEPALIVE_PATH,
                    str(existing.get('job_name') or job_name), False,
                    node=node, **kwargs)
            except Exception as error:
                self.notify(f'could not update keep-alive registry: {error}',
                            severity='error', timeout=6, markup=False)
                return
            finally:
                self._ka_inflight.discard(inflight_key)
            # The atomic setter is authoritative. Do not turn a successful NFS
            # write into a false failure by immediately rereading the file.
            existing_identity = keepalive._entry_identity(existing)
            self._ka_entries = [
                entry for entry in self._ka_entries
                if keepalive._entry_identity(entry) != existing_identity
            ]
            self._ka_names = {
                entry.get('job_name') for entry in self._ka_entries
                if isinstance(entry.get('job_name'), str)
            }
            self.notify(f'keep-alive OFF · {job_name} #{job_id}', timeout=4,
                        markup=False)
            self._refresh_table(self._last_state)
            return
        # Detect the launch script off the event loop (scontrol can be slow).
        entry_id = uuid.uuid4().hex
        try:
            self.run_worker(self._enable_keepalive_async(
                                node, job_id, job_name, inflight_key, entry_id),
                            exclusive=False, group='keepalive')
        except BaseException:
            self._ka_inflight.discard(inflight_key)
            raise

    async def _enable_keepalive_async(
            self, node: str, job_id: str, job_name: str,
            inflight_key: str | None = None,
            entry_id: str | None = None) -> None:
        normalized_job_id = keepalive.job_family_id(job_id)
        inflight_key = inflight_key or (
            f'job:{normalized_job_id}' if normalized_job_id else job_name)
        entry_id = entry_id or uuid.uuid4().hex
        try:
            info = await _offload_for(
                _operation_timeout(10), self._scontrol_job, job_id, node)
            if info is None:
                self.notify(f'could not read job {job_id} (scontrol)',
                            severity='warning', timeout=4, markup=False)
                return
            cmd = (info.get('command') or '').strip()
            # '(null)' is what scontrol reports for `sbatch --wrap` jobs (no
            # script to resubmit); reject rather than register something that
            # can only ever fail and then pause.
            if not info.get('batch') or not cmd or cmd == '(null)':
                self.notify('no batch script for this job — can’t keep alive',
                            severity='warning', timeout=5)
                return
            authoritative_name = info.get('job_name') or job_name
            if not isinstance(authoritative_name, str) or not authoritative_name:
                authoritative_name = job_name
            await _offload_for(
                _operation_timeout(8), _set_keepalive_enabled,
                config.KEEPALIVE_PATH,
                authoritative_name, True, info['command'],
                info.get('workdir') or '', job_id=job_id, entry_id=entry_id,
                node=node)
            new_entry = {
                'entry_id': entry_id,
                'job_id': keepalive.job_family_id(job_id),
                'job_name': authoritative_name,
                'command': info['command'],
                'workdir': info.get('workdir') or '',
                'enabled': True,
            }
            self._ka_entries = [
                entry for entry in self._ka_entries
                if not self._ka_entry_matches(entry, job_id, authoritative_name)
            ]
            self._ka_entries.append(new_entry)
            self._ka_names = {
                entry.get('job_name') for entry in self._ka_entries
                if isinstance(entry.get('job_name'), str)
            }
            command = str(info['command'])
            self.notify(
                        f'✓ keep-alive ON · {authoritative_name} #{job_id} · '
                        f'will re-run {command[:80]}',
                        timeout=6, markup=False)
            self._refresh_table(self._last_state)
        except Exception as error:
            self.notify(f'could not enable keep-alive: {error}',
                        severity='error', timeout=6, markup=False)
        finally:
            self._ka_inflight.discard(inflight_key)

    @staticmethod
    def _scontrol_job(job_id: str, node: str | None = None):
        """Run `scontrol show job <id>` and parse it. None on failure."""
        # Only real SLURM job ids: '123', array '123_4', array-range '123_[5-9]'.
        # Guards against a crafted state value like '-Q' being taken by scontrol
        # as an option (argv injection) rather than a job id.
        if not re.match(r'^\d+(_\d+|_\[[0-9,\-]+\])?$', str(job_id)):
            return None
        if _GATEWAY_POOL is not None:
            return _GATEWAY_POOL.scontrol_job(str(job_id), node=node)
        try:
            result = _hard_subprocess_run(
                ['scontrol', 'show', 'job', str(job_id)],
                timeout=8, slots=_SLURM_COMMAND_SLOTS,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return keepalive.parse_scontrol(result.stdout or '')

    async def on_unmount(self) -> None:
        # Tear down warm slaves so we don't leave orphan ssh processes.
        try:
            self._warm_pool.shutdown()
        except Exception:
            pass

    async def action_new_window(self) -> None:
        node, sess = self.selected_node, self.selected_session
        if not node or not sess:
            return
        if sess == _OFFLINE_SESSION:
            self.notify(f'{node} is offline · press s to retry a shell or r to refresh',
                        severity='warning', timeout=6, markup=False)
            return
        if node != 'localhost' and not _valid_node(node):
            self.notify(f'invalid node name: {node!r}', severity='error',
                        timeout=5, markup=False)
            return
        if node == 'localhost':
            cmd = ([os.environ.get('SHELL') or '/bin/bash']
                   if sess == _START_SHELL_SESSION
                   else _local_attach_argv(sess))
        else:
            base = ['ssh'] + _get_ssh_args(node) + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node]
            cmd = (base if sess == _START_SHELL_SESSION
                   else base + ['tmux', 'attach-session', '-d',
                                '-t', shlex.quote(sess)])

        if os.environ.get('TMUX'):
            wname = (f"{node}-{sess}" if sess != _START_SHELL_SESSION
                     else f"{node}-shell")
            # A real tmux target must enter through the direct-attach helper in
            # the new pane. That child owns a shared passthrough lease for its
            # whole lifetime, so ordinary inner prefixes work and concurrent
            # windows cannot restore the outer prefix out from under each other.
            if node != 'localhost' and sess == _START_SHELL_SESSION:
                cmd = [
                    sys.executable, '-m', 'autotmux.cli', '--shell', node,
                ]
            elif sess != _START_SHELL_SESSION:
                cmd = [
                    sys.executable, '-m', 'autotmux.cli', '--attach',
                    f'{node}:{sess}',
                ]
            # tmux 2.7 accepts exactly one optional shell-command here. Pass a
            # safely quoted command string instead of treating every argv
            # element as another new-window argument.
            if not _tmux('new-window', '-n', wname, shlex.join(cmd)):
                self.notify('could not create tmux window', severity='error',
                            timeout=7, markup=False)
        else:
            # Outside tmux there is no tmux window to make, but on macOS there
            # is a terminal window: the atmux:// handler opens one and attaches.
            # Only then does `o` differ from Enter, which is the whole point of
            # the key.
            if sess != _START_SHELL_SESSION:
                try:
                    opened, why = await _offload_for(
                        20.0, _open_new_terminal_window, node, sess)
                except Exception as error:
                    opened, why = False, ' '.join(str(error).split())[:120] or (
                        'opening a window timed out')
                if opened:
                    self.notify(f'{_session_label(sess)} opened in a new window',
                                timeout=4, markup=False)
                    return
                self.notify(f'no new window: {why}', severity='warning',
                            timeout=8, markup=False)
            returncode = 0
            command_error = ''
            with self.suspend():
                print("\n[AutoTmux] Attaching here instead of in a new window.")
                if node == 'localhost':
                    returncode, command_error = _run_user_command(cmd)
                else:
                    remote_args = (
                        None if sess == _START_SHELL_SESSION else
                        ['tmux', 'attach', '-t', shlex.quote(sess)])
                    returncode, command_error, _used_direct = (
                        _run_remote_user_command(
                            node, remote_args,
                            direct=_node_network_degraded(
                                self._last_state, node)))
                    _publish_remote_command_result(
                        node, returncode, command_error, 'new-window')
            self._report_command_result(
                f'open {node}:{_session_label(sess)}',
                returncode, command_error)


def _daemon_running() -> bool:
    """Is a daemon alive?

    Authoritative check first: the daemon holds an exclusive flock on
    PID_FILE+'.lock' for its whole lifetime. The pid file itself is only
    advisory and can vanish while the daemon still runs — systemd wipes
    XDG_RUNTIME_DIR when the last login session ends, but the daemon is
    reparented to init and survives. Trusting only the pid file made the
    frontend declare a live daemon dead and enter an unwinnable restart loop
    (the singleton lock blocks the new start), stalling the UI. If we cannot
    take the lock, a daemon holds it → alive."""
    # The stable guard survives XDG_RUNTIME_DIR cleanup; the runtime lock keeps
    # compatibility with already-running pre-guard daemons.
    if (lifecycle.lock_is_held(GUARD_FILE)
            or lifecycle.lock_is_held(LOCK_FILE)):
        return True
    # Lock free or absent — fall back to the advisory pid file.
    try:
        pid = int(lifecycle.read_owned_regular_file(PID_FILE, 4096).strip())
        return lifecycle.pid_running(pid) and lifecycle.is_autotmux_daemon(pid)
    except (OSError, ValueError, FileNotFoundError):
        return False


_RESTART_WINDOW = 60.0   # seconds — loop-guard window for daemon restarts
_RESTART_LIMIT = 3       # max restarts allowed within the window


def _should_restart(attempts, now: float, window: float = _RESTART_WINDOW,
                    limit: int = _RESTART_LIMIT) -> bool:
    """Loop guard: allow a daemon restart only if fewer than `limit`
    restarts happened in the last `window` seconds. `attempts` is a list of
    time.monotonic() timestamps of prior restarts."""
    recent = [t for t in attempts if now - t < window]
    return len(recent) < limit


def _launch_daemon() -> tuple[bool, str]:
    """Start the daemon if it isn't already running.

    Run the module with this frontend's interpreter.  Looking up the generic
    name ``atd`` on PATH can resolve the unrelated system at-job daemon when
    AutoTmux's console wrapper is absent or the environment was not activated.
    """
    if _gateway_mode():
        return True, ''
    if _daemon_running():
        return True, ''
    cmd = [sys.executable, '-m', 'autotmux.daemon', 'start']
    try:
        # Startup waits for the detached child's ready/error handshake.  The
        # timeout is a safety net so a wedged start can never hang the recovery
        # worker (and with it the UI); failed launches retry on the next tick.
        result = _hard_subprocess_run(
            cmd, timeout=15, slots=_DAEMON_LAUNCH_SLOTS,
            capture_output=True, text=True)
        if result.returncode == 0:
            # New daemons return zero only after the detached child reports
            # readiness.  Still verify the stable guard metadata: it protects
            # mixed-version upgrades and catches an immediately dying child
            # before the UI consumes one of its bounded recovery attempts.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if lifecycle.active_runtime_base(GUARD_FILE) is not None:
                    _sync_active_runtime_paths()
                    return True, ''
                if not _daemon_running():
                    break
                time.sleep(0.05)
            return False, 'daemon detached but did not become ready within 5s'
        detail = ' '.join(
            (result.stderr or result.stdout or f'exit {result.returncode}').split())
        return False, detail[:240]
    except subprocess.TimeoutExpired:
        return False, 'daemon start timed out after 15s'
    except OSError as error:
        return False, f'{os.path.basename(str(cmd[0]))}: {error.strerror or error}'
    except Exception as error:
        return False, str(error)[:240]


def _request_daemon_start() -> None:
    """Best-effort daemon start for direct attach without delaying attach."""
    if _gateway_mode():
        return
    try:
        threading.Thread(target=_launch_daemon, daemon=True,
                         name='atmux-daemon-start').start()
    except (RuntimeError, OSError):
        pass


def _published_direct_preference(node: str) -> bool:
    """Read the daemon's local state for a non-TUI attach helper."""
    if _gateway_mode():
        return False
    try:
        valid, state = _read_json_dict_checked(STATE_FILE, _STATE_FILE_LIMIT)
    except Exception:
        return False
    if not valid:
        return False
    _apply_daemon_ssh_settings(state)
    return _node_network_degraded(state, node)


def _valid_select(target) -> tuple[str, str] | None:
    """`NODE:SESSION` for --select, or None.

    Same shape and the same scepticism as --attach: this reaches the process
    from a browser URL by way of the web server, and a value that is not a
    target should open the dashboard rather than something surprising.
    """
    if not isinstance(target, str) or ':' not in target:
        return None
    node, _, session = target.partition(':')
    if not node or not session:
        return None
    if node != 'localhost' and not _valid_node(node):
        return None
    return node, session


def _announce_external() -> None:
    """Tell a browser client the screen is about to belong to tmux.

    The dashboard says this in ``suspend()``, but a direct attach never
    builds a dashboard: it hands the terminal to tmux and exits.  Without
    this, the phone that tapped a session row — the single most common way
    to reach tmux from a browser — is the one client that is never told what
    is on the other end, and both its keys and its swipe have to guess.

    Silent unless a client said it draws its own controls, so a terminal
    never sees this: ``touch_mode`` is 'web' only when the web server set it.
    """
    if keypad.touch_mode() != 'web':
        return
    try:
        sys.stdout.write(keypad.encode(
            'external', keypad.EXTERNAL_KEYS, keypad.tmux_prefix()))
        sys.stdout.flush()
    except Exception:
        # An attach that fails because a phone could not be told about a
        # button is a worse outcome than a phone with the wrong buttons.
        return


def _direct_attach(target: str) -> int:
    """`atmux -a NODE:SESSION` — skip the TUI and attach directly.

    Useful for scripting (`atmux -a holygpu1:train`) and as a hotkey-bind
    target.  Returns the desired process exit code.
    """
    if ':' not in target:
        sys.stderr.write(f'atmux: --attach expects NODE:SESSION, got {target!r}\n')
        return 2
    node, _, session = target.partition(':')
    if not node or not session:
        sys.stderr.write(f'atmux: --attach expects NODE:SESSION, got {target!r}\n')
        return 2
    if node != 'localhost' and not _valid_node(node):
        sys.stderr.write(f'atmux: invalid node name {node!r}\n')
        return 2
    # A direct attach can always use a cold SSH connection; daemon startup is
    # only a future-latency optimization and must not add a 15-second blank
    # wait before handing over the terminal.
    _request_daemon_start()
    nest = _will_nest_tmux(session)
    step_ok = True
    restore_ok = True
    if nest:
        step_ok = _tmux_step_aside()
        if not step_ok:
            sys.stderr.write(
                'atmux: outer-tmux passthrough unavailable; use the outer '
                'prefix twice for the inner tmux\n')
    # After validation, before the handover: a target that was rejected above
    # never reaches tmux, and announcing one that did not happen would leave
    # the client holding tmux's keys in front of an error message.
    _announce_external()
    try:
        with urlhandler.attached(node, session):
            if node == 'localhost':
                returncode, error = _run_user_command(
                    _local_attach_argv(session))
            else:
                returncode, error, _used_direct = _run_remote_user_command(
                    node, ['tmux', 'attach', '-t', shlex.quote(session)],
                    direct=_published_direct_preference(node))
                _publish_remote_command_result(
                    node, returncode, error, 'direct-attach')
    finally:
        if nest:
            restore_ok = _tmux_restore()
    if error:
        sys.stderr.write(f'atmux: {error}\n')
    if nest and not restore_ok:
        sys.stderr.write(
            'atmux: failed to restore outer tmux; press F12 to recover it\n')
        if returncode == 0:
            returncode = 1
    return returncode


def _direct_shell(node: str) -> int:
    """Open a shell from a new tmux window with the same weak-net fallback."""
    if node != 'localhost' and not _valid_node(node):
        sys.stderr.write(f'atmux: invalid node name {node!r}\n')
        return 2
    _request_daemon_start()
    if node == 'localhost':
        returncode, error = _run_user_command(
            [os.environ.get('SHELL') or '/bin/bash'])
    else:
        returncode, error, _used_direct = _run_remote_user_command(
            node, None, direct=_published_direct_preference(node))
        _publish_remote_command_result(
            node, returncode, error, 'direct-shell')
    if error:
        sys.stderr.write(f'atmux: {error}\n')
    return returncode


def _handler_atmux_path(env=None, argv0=None) -> str:
    """The atmux the URL handler applet should run.

    ``ATMUX_BIN`` wins so the installer can point the applet at the launcher the
    user actually invokes -- typically ``~/.local/bin/atmux``, which survives a
    rebuilt virtualenv, whereas this process may already have been exec'd
    through it into the venv's own entry point.
    """
    env = os.environ if env is None else env
    override = (env.get('ATMUX_BIN') or '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.realpath((sys.argv[0] if argv0 is None else argv0) or 'atmux')


def _build_argparser():
    import argparse
    p = argparse.ArgumentParser(
        prog='atmux',
        description='AutoTmux — terminal dashboard for tmux sessions across slurm nodes.',
    )
    p.add_argument('--version', action='version', version=f'AutoTmux {__version__}')
    target = p.add_mutually_exclusive_group()
    target.add_argument('-a', '--attach', metavar='NODE:SESSION',
                        help='Skip the TUI and attach directly to NODE:SESSION.')
    target.add_argument('--shell', dest='shell_node', metavar='NODE',
                        help='Skip the TUI and open a shell directly on NODE.')
    target.add_argument(
        '--select', dest='select', metavar='NODE:SESSION',
        help='Open the TUI with this row already under the cursor. One flag '
             'rather than one per verb: every action the dashboard has acts '
             'on the highlighted row, so landing on the right row is what '
             'makes all of them reachable from somewhere that is not a '
             'keyboard.')
    target.add_argument(
        '--open-url', dest='open_url', metavar='URL',
        help='Attach from an atmux://attach/NODE/SESSION link. The URL is '
             'untrusted input: it is validated here and dispatched as argv, '
             'never through a shell.')
    target.add_argument(
        '--print-url-handler', dest='print_url_handler', nargs='?',
        const='', choices=('',) + urlhandler.TERMINALS, metavar='TERMINAL',
        help='Print the AppleScript for the macOS atmux:// handler and exit. '
             'Used by contrib/install-url-handler-macos.sh; TERMINAL defaults '
             'to iTerm when it is installed, else Terminal.')
    target.add_argument(
        '--connections', action='store_true',
        help='Open the TUI connection manager on startup.')
    target.add_argument(
        '--gateway-login', action='store_true',
        help='Interactively authenticate and pre-warm every configured login '
             'gateway, then exit.')
    target.add_argument(
        '--gateway-check', action='store_true',
        help='Probe every configured login gateway and agent, then exit.')
    mouse = p.add_mutually_exclusive_group()
    mouse.add_argument('--no-mouse', action='store_true',
                       help='Force keyboard-only (no mouse tracking). This is the '
                            'default over SSH.')
    mouse.add_argument('--mouse', action='store_true',
                       help='Force-enable mouse (click-to-attach) even over SSH. '
                            'Off by default over SSH because mouse-report traffic '
                            'competes with keystrokes and can make arrow keys feel '
                            'dead on a remote/loaded terminal.')
    deployment = p.add_mutually_exclusive_group()
    deployment.add_argument(
        '--gateway-mode', action='store_true',
        help='Run the TUI locally and reach Slurm nodes through the login '
             'gateways configured in [client].')
    deployment.add_argument(
        '--login-mode', action='store_true',
        help='Force the original native login-node mode for this invocation.')
    p.add_argument(
        '--gateway', action='append', default=[], metavar='SSH_HOST',
        help='Use this SSH login gateway for local mode (repeatable; overrides '
             '[client].gateways).')
    # Deliberately separate from --gateway: repeating --gateway adds another
    # way in to the *same* place, and AutoTmux races those. A machine that is
    # somewhere else has to say so, or it would win the race and replace the
    # table with its own single row.
    p.add_argument(
        '--cluster', action='append', default=[], metavar='NAME=HOST[,HOST]',
        help='Add an independent cluster or standalone machine, shown in the '
             'same table (repeatable). A lone workstation is just NAME=HOST.')
    return p


def parse_cluster_args(values) -> tuple[dict, str]:
    """``['lab=ws', 'other=o1,o2']`` -> ``({'lab': ['ws'], ...}, error)``."""
    clusters: dict = {}
    for value in values or []:
        name, separator, hosts = str(value).partition('=')
        name = name.strip()
        if not separator or not hosts.strip():
            return {}, f'--cluster needs NAME=HOST, got {value!r}'
        if config.CLUSTER_NAME_RE.fullmatch(name) is None:
            return {}, f'invalid cluster name: {name!r}'
        if name == config.PRIMARY_CLUSTER:
            return {}, (f'{config.PRIMARY_CLUSTER!r} is the name of the '
                        'cluster in --gateway/[client].gateways')
        members = [host.strip() for host in hosts.split(',') if host.strip()]
        bad = [host for host in members if not config.valid_gateway(host)]
        if bad:
            return {}, f'invalid SSH alias in --cluster {name}: {bad[0]!r}'
        if name in clusters:
            return {}, f'--cluster {name} given twice'
        clusters[name] = members
    return clusters, ''


def _has_local_slurm() -> bool:
    """Whether this machine can answer for Slurm on its own.

    The discriminator between a login node -- where the local daemon is the
    source of truth and native mode is right -- and a workstation that only
    ever reaches a cluster through a gateway, where native mode can only ever
    show an empty table.
    """
    return shutil.which('squeue') is not None


def _is_remote_session() -> bool:
    """True when we're on the far end of an SSH connection. Mouse tracking makes
    the terminal stream a burst of report bytes on every move/scroll, which over
    SSH (especially into a loaded login node) buries keystrokes and makes arrow
    keys feel dead — so we default mouse OFF here."""
    return any(os.environ.get(name) for name in (
        'SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT',
        'MOSH_CONNECTION', 'MOSH_IP',
    ))


def _want_mouse(args) -> bool:
    """Whether to report mouse events to the app.

    Reporting is what makes click-to-attach work, and also what stops the
    terminal doing its own text selection -- so this has to be settable
    without retyping a flag every launch. Order: explicit flag, saved
    preference, then the default of on locally and off over SSH (where the
    report traffic competes with keystrokes).
    """
    if args.no_mouse:
        return False
    if args.mouse:
        return True
    ok, settings = _load_client_config_bounded()
    if ok and isinstance(settings, dict):
        preference = str(settings.get('mouse') or 'auto').lower()
        if preference == 'off':
            return False
        if preference == 'on':
            return True
    return not _is_remote_session()


def _configure_gateway_mode(args) -> str:
    """Configure the optional local gateway pool; return an error or ``''``."""
    global _GATEWAY_POOL
    _GATEWAY_POOL = None
    cli_gateways = list(getattr(args, 'gateway', None) or [])
    if getattr(args, 'login_mode', False):
        # Preserve native login-node startup even if ~/.config is on a wedged
        # shared filesystem.
        return ''
    explicit_gateway = bool(
        cli_gateways or getattr(args, 'gateway_mode', False)
        or getattr(args, 'cluster', None)
        or getattr(args, 'gateway_login', False)
        or getattr(args, 'gateway_check', False))
    if (_is_remote_session() and not explicit_gateway
            and _has_local_slurm()):
        # Native login-node mode is the compatibility contract -- but only
        # where there is a login node. The contract is that ordinary SSH use
        # does not change, and does not touch an NFS-backed client config on
        # a possibly wedged shared filesystem; on a machine that answers for
        # Slurm itself, returning here honours both.
        #
        # A machine with no Slurm at all answers for nothing. Returning here
        # meant a saved `mode = "gateway"` was never read, because the read
        # is below this line -- so sshing to such a host and running atmux
        # showed zero sessions and an empty queue, while the same binary on
        # the same host reached every cluster when it was started by
        # something that was not an SSH session.
        return ''
    config_ok, settings = _load_client_config_bounded()
    if not config_ok or not isinstance(settings, dict):
        if cli_gateways:
            settings = dict(config.CLIENT_DEFAULTS)
            settings['gateways'] = []
            settings['agent_command'] = list(
                config.CLIENT_DEFAULTS['agent_command'])
        else:
            return ('timed out reading [client] config; use --login-mode or '
                    'pass --gateway explicitly')
    if cli_gateways:
        invalid = [value for value in cli_gateways
                   if not config.valid_gateway(value)]
        if invalid:
            return f'invalid gateway SSH destination: {invalid[0]!r}'
        settings['gateways'] = list(dict.fromkeys(cli_gateways))

    cli_clusters, cluster_error = parse_cluster_args(
        getattr(args, 'cluster', None))
    if cluster_error:
        return cluster_error
    if cli_clusters:
        # Additive: --cluster names extra places, it does not restate the
        # ones already configured.
        merged = dict(settings.get('clusters') or {})
        merged.update(cli_clusters)
        settings['clusters'] = config.clean_clusters(
            merged, exclude=(config.PRIMARY_CLUSTER,))

    forced = bool(
        getattr(args, 'gateway_mode', False) or cli_gateways
        or cli_clusters
        or getattr(args, 'gateway_login', False)
        or getattr(args, 'gateway_check', False))
    mode = settings.get('mode', 'auto')
    # Same discriminator as the guard above, for the same reason: SSH implies
    # "native" only where the machine can actually be native. On a host with
    # no Slurm, refusing here leaves a configured gateway client showing an
    # empty table over SSH and a full one over anything else.
    enabled = forced or mode == 'gateway' or (
        mode == 'auto' and bool(settings.get('gateways'))
        and not (_is_remote_session() and _has_local_slurm()))
    if not enabled:
        return ''
    if not settings.get('gateways'):
        return 'gateway mode needs at least one [client].gateways entry'
    try:
        # Always the multi-cluster pool, even for one cluster: a single code
        # path downstream is worth more than the indirection costs.
        _GATEWAY_POOL = gateway_client.ClusterPool(
            config.client_clusters(settings), settings)
    except Exception as error:
        return f'could not initialize gateway mode: {error}'
    return ''


def _should_offer_connection_setup(args) -> bool:
    """Whether a local first run should open the SSH-alias picker."""
    if getattr(args, 'connections', False):
        return True
    if (_is_remote_session() or getattr(args, 'login_mode', False)
            or getattr(args, 'gateway', None)
            or getattr(args, 'gateway_mode', False)):
        return False
    ok, settings = _load_client_config_bounded()
    return bool(ok and isinstance(settings, dict)
                and settings.get('mode') != 'login'
                and not settings.get('gateways'))


def main():
    global _RUNTIME_DISCOVERY_ENABLED
    parser = _build_argparser()
    args = parser.parse_args()
    if args.print_url_handler is not None:
        # Pure text generation: nothing about the runtime, the config or the
        # gateways is needed, so answer before any of it is touched.
        sys.stdout.write(urlhandler.applescript(
            _handler_atmux_path(),
            args.print_url_handler or urlhandler.default_terminal()))
        sys.exit(0)
    deployment_error = _configure_gateway_mode(args)
    if deployment_error:
        parser.error(deployment_error)
    _RUNTIME_DISCOVERY_ENABLED = not _gateway_mode()
    if not _gateway_mode():
        _sync_active_runtime_paths()
    if args.gateway_login or args.gateway_check:
        if _GATEWAY_POOL is None:
            parser.error('this command needs configured login gateways')
        results = (_GATEWAY_POOL.authenticate() if args.gateway_login
                   else _GATEWAY_POOL.check_all())
        for result in results:
            name = result.get('gateway', '?')
            if result.get('ok'):
                if args.gateway_login:
                    detail = ('already active' if result.get('existing')
                              else 'authenticated')
                else:
                    latency = result.get('latency_ms')
                    detail = (f"{float(latency):.0f} ms"
                              if isinstance(latency, (int, float)) else 'ok')
                    remote = result.get('host') or ''
                    version = result.get('version') or ''
                    if remote:
                        detail += f' · {remote}'
                    if version:
                        detail += f' · AutoTmux {version}'
                print(f'✓ {name}: {detail}')
            else:
                print(f"✗ {name}: {result.get('error') or 'unavailable'}")
            # A reachable gateway can still hang for a long time if the master
            # we share is slow to notice a dead peer, so say so here rather
            # than leaving the user to guess during the next outage.
            try:
                warning = _GATEWAY_POOL.keepalive_warning(name)
            except Exception:
                warning = ''
            if warning:
                print(f'  ⚠ {warning}')
        sys.exit(0 if all(result.get('ok') for result in results) else 1)
    if args.open_url:
        parsed = notify.parse_attach_url(args.open_url)
        if parsed is None:
            sys.stderr.write(
                'atmux: refusing to open an unrecognised or unsafe URL\n')
            sys.exit(2)
        node, session = parsed
        sys.exit(_direct_attach(f'{node}:{session}'))
    if args.attach:
        sys.exit(_direct_attach(args.attach))
    if args.shell_node:
        sys.exit(_direct_shell(args.shell_node))
    # The first frame should never wait behind a slow/failed daemon start.
    # on_mount() paints immediately and dispatches the existing guarded
    # recovery worker when no singleton lock is held.
    AutotmuxApp(
        offer_connection_setup=_should_offer_connection_setup(args),
        select=_valid_select(getattr(args, 'select', None)),
    ).run(mouse=_want_mouse(args))


if __name__ == "__main__":
    main()
