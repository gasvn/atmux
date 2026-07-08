#!/usr/bin/env python3
"""AutoTmux – Textual frontend.

The frontend is 100% passive: it reads /tmp/autotmux_daemon_<uid>.json
written by the atd backend and renders it.  It NEVER runs squeue, ssh,
or any slow command for the purpose of listing nodes or sessions.

The only network calls the frontend makes are:
  1) tmux capture-pane via SSH for the live preview (async, non-blocking).
  2) ssh/tmux attach when the user explicitly presses Enter/s/o.
"""
import asyncio
import fcntl
import hashlib
import json
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static
import rich.text

from autotmux import __version__

from autotmux import paths

STATE_FILE = paths.STATE_FILE
CTL_DIR = paths.CTL_DIR
PID_FILE = paths.PID_FILE
SNAPSHOT_FILE = paths.SNAPSHOT_FILE


def _ctl_path(node: str) -> str:
    safe = node.replace('/', '_').replace(':', '_')
    return os.path.join(CTL_DIR, f'cm_{safe}')


def _get_ssh_args(node: str) -> list:
    """Return ControlPath + keepalive flags so an idle slave channel
    doesn't get silently dropped by NAT/firewalls (which is what kicks
    `<Start Shell>` users out — plain bash has no keepalive of its own,
    unlike tmux which talks to its server periodically)."""
    args = ['-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3']
    ctl = _ctl_path(node)
    if os.path.exists(ctl):
        args += ['-o', f'ControlPath={ctl}']
    return args


def read_state() -> dict:
    """Read the daemon state file.  Returns {} on any error."""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def read_snapshots() -> dict:
    """Read the persistent preview snapshot cache. Returns {} on any error."""
    try:
        with open(SNAPSHOT_FILE, 'r') as f:
            return json.load(f) or {}
    except Exception:
        return {}


class WarmSlavePool:
    """Maintains an idle interactive ssh slave per remote node, so that
    `tmux attach` is near-instant.

    The slow part of an attach over a high-load compute node is not the SSH
    handshake itself (the master takes care of that) — it's the remote sshd
    forking a fresh shell process that has to wait its turn on a busy CPU.
    By keeping a long-lived `ssh -tt` running in the background, that fork
    has already happened. When the user presses Enter we just send
    `exec tmux attach -t SESS\\n` into the warm shell and proxy bytes
    between the local terminal and the pty until the user detaches.

    Slaves are spawned with `pty.fork()`, so we hold the pty master fd in
    this process and can read/write to the remote shell. Failure to spawn
    or attach silently falls back to the cold subprocess-based path.
    """

    def __init__(self) -> None:
        # node -> (pid, master_fd)
        self._slaves: dict = {}
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
        if node == 'localhost':
            return
        ctl = _ctl_path(node)
        if not os.path.exists(ctl):
            return
        with self._lock:
            if self._closed:
                return
            if self._still_alive_locked(node):
                return
            try:
                pid, master_fd = pty.fork()
            except OSError:
                return
            if pid == 0:
                # child — replace with ssh
                try:
                    os.execvp('ssh', [
                        'ssh',
                        '-o', f'ControlPath={ctl}',
                        '-o', 'StrictHostKeyChecking=accept-new',
                        '-o', 'ServerAliveInterval=30',
                        '-o', 'ServerAliveCountMax=3',
                        '-tt', node,
                    ])
                except BaseException:
                    pass
                # Belt-and-suspenders: the child must NEVER fall through into
                # parent (Textual) code, whatever execvp raises.
                os._exit(127)
            # parent — make the master fd close-on-exec so child processes don't
            # accidentally inherit it.
            try:
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFD)
                fcntl.fcntl(master_fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
            except Exception:
                pass
            self._slaves[node] = (pid, master_fd)

    def warm_all(self, nodes) -> None:
        """Spawn warm slaves for `nodes` and tear down slaves for any node
        that's no longer in the set — so departing slurm allocations don't
        leave orphan ssh processes for hours."""
        wanted = set(nodes)
        with self._lock:
            current = set(self._slaves)
        # Kill slaves for nodes that left the view.
        for departed in (current - wanted):
            self._cleanup(departed)
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
        for node in nodes:
            self._cleanup(node)

    # ── attach path ─────────────────────────────────────────────────────────

    # A proxy that returns faster than this almost certainly means the warm
    # slave was stale (local ssh alive, but its channel / remote shell had
    # quietly died) — not a real attach the user interacted with. Treat it as
    # a failure so the caller falls back to a fresh cold attach instead of
    # popping the user straight back out.
    _MIN_PROXY_SECONDS = 0.5

    def attach(self, node: str, session: str) -> bool:
        """Try to attach using a warm slave. Returns True if the proxy ran a
        real session (the user's terminal was handed over and they've now
        detached); False if no warm slave was available OR the slave turned
        out to be stale — caller should fall back to a cold attach."""
        slave = self._take(node)
        if not slave:
            return False
        pid, master_fd = slave
        ran_real_session = False
        try:
            # Drain whatever the remote bash already printed (welcome
            # banner, prompt, etc.) so the user sees a clean tmux paint.
            self._drain(master_fd)
            cmd = f'exec tmux attach -t {shlex.quote(session)}\n'
            try:
                os.write(master_fd, cmd.encode())
            except OSError:
                return False
            start = time.monotonic()
            self._proxy(master_fd, pid)
            ran_real_session = (time.monotonic() - start) >= self._MIN_PROXY_SECONDS
        finally:
            self._reap_child(pid)
            try:
                os.close(master_fd)
            except OSError:
                pass
        return ran_real_session

    @staticmethod
    def _reap_child(pid: int) -> None:
        """Send SIGTERM, give it a beat to wind down, escalate to SIGKILL,
        then waitpid so we don't leak zombies."""
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except OSError:
                return
            if wpid == pid:
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass

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
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == 0:
                return True
        except OSError:
            pass
        # Process gone — drop it AND close its pty master fd. Forgetting the
        # close here leaks one fd per silently-dead slave until EMFILE.
        self._slaves.pop(node, None)
        try:
            os.close(fd)
        except OSError:
            pass
        return False

    def _take(self, node: str):
        with self._lock:
            if not self._still_alive_locked(node):
                return None
            return self._slaves.pop(node, None)

    def _cleanup(self, node: str) -> None:
        with self._lock:
            slave = self._slaves.pop(node, None)
        if not slave:
            return
        pid, fd = slave
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        # Reap so we don't leak zombies. SIGTERM should be enough; escalate
        # to SIGKILL if the child is stuck.
        for _ in range(20):
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except OSError:
                return
            if wpid == pid:
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass

    @staticmethod
    def _drain(master_fd: int) -> None:
        """Read and discard whatever the slave already produced."""
        try:
            while True:
                r, _, _ = select.select([master_fd], [], [], 0.05)
                if master_fd not in r:
                    return
                try:
                    if not os.read(master_fd, 8192):
                        return
                except OSError:
                    return
        except (OSError, ValueError):
            return

    @staticmethod
    def _proxy(master_fd: int, child_pid: int) -> None:
        """Bridge fd 0/1 to/from master_fd until the child exits.

        Mirrors what `ssh` itself does between user terminal and remote pty,
        but we're in front of the ssh client this time.
        """
        try:
            old_attr = termios.tcgetattr(0)
        except termios.error:
            old_attr = None

        def on_winch(_sig=None, _frame=None):
            try:
                cols, rows = os.get_terminal_size(0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                            struct.pack('HHHH', rows, cols, 0, 0))
            except Exception:
                pass

        old_winch = signal.signal(signal.SIGWINCH, on_winch)
        on_winch()
        if old_attr is not None:
            tty.setraw(0)
        try:
            while True:
                try:
                    r, _, _ = select.select([0, master_fd], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if 0 in r:
                    try:
                        data = os.read(0, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        break
                if master_fd in r:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        # pty EOF — the ssh slave (and its tmux) have exited.
                        break
                    try:
                        os.write(1, data)
                    except OSError:
                        break
                # NOTE: we deliberately do NOT waitpid(child_pid) here. The
                # caller's finally → _reap_child is the single owner of the
                # reap; reaping in two places let a recycled PID get signalled.
                # The pty EOF above is our reliable exit signal.
        finally:
            try:
                signal.signal(signal.SIGWINCH, old_winch)
            except Exception:
                pass
            if old_attr is not None:
                try:
                    termios.tcsetattr(0, termios.TCSADRAIN, old_attr)
                except Exception:
                    pass


def build_session_rows(state: dict) -> list:
    """Turn daemon state into a flat list of display rows.

    Each row: (node, session, wins, time_left, status, cpu, load)
    `cpu` is the nproc the slurm allocation gave us; `load` is the 1-min
    load average. Together they tell the user whether attaching will be
    snappy or sluggish.
    """
    rows = []
    for node, nd in state.get('nodes', {}).items():
        info = nd.get('info', {})
        time_left = info.get('time', '')
        cpu = info.get('nproc', '')
        load = info.get('load', '')
        last_error = nd.get('last_error') or ''
        if not nd.get('alive'):
            status = f'OFFLINE: {last_error[:30]}' if last_error else 'OFFLINE'
            rows.append((node, '<offline>', '-', time_left, status, cpu, load))
            continue
        sessions = nd.get('sessions', [])
        if sessions:
            for s in sessions:
                name = s[0] if isinstance(s, list) else str(s)
                wins = s[1] if isinstance(s, list) and len(s) > 1 else '?'
                rows.append((node, name, wins, time_left, 'Active', cpu, load))
        else:
            rows.append((node, '<Start Shell>', '-', time_left, 'No sessions', cpu, load))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


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

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        row_index = meta["row"]
        column_index = meta["column"]
        if row_index < 0 or column_index < 0:
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


class AutotmuxApp(App):
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
    #left_pane {
        width: 45%;
        border-right: solid $primary;
    }
    #right_pane_scroll {
        width: 55%;
        background: $surface;
        padding: 0 1;
    }
    #right_pane {
        width: 100%;
        height: auto;
    }
    #jobs_panel {
        height: 14;
        border-top: solid $primary;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("q", "app.quit", "Quit"),
        Binding("r", "refresh_table", "Refresh"),
        # Enter is handled by on_data_table_row_selected (DataTable consumes
        # the key itself and emits RowSelected, so an App-level Binding
        # for "enter" would never fire).
        Binding("s", "open_shell", "Shell"),
        Binding("t", "local_shell", "Local Shell"),
        Binding("o", "new_window", "New Window"),
        Binding("j", "toggle_jobs_view", "Jobs view"),
    ]

    title = reactive(f"AutoTmux v{__version__}")
    sub_title = reactive("")

    def __init__(self) -> None:
        super().__init__()
        self._restart_attempts = []   # time.monotonic() of recent daemon restarts
        self._crash_looping = False

    def compose(self) -> ComposeResult:
        # No clock: HeaderClock repaints the header every second, which over a
        # remote/SSH terminal is a constant trickle of redraws even while the
        # app is idle. Keeping the header static makes an idle atmux silent on
        # the wire.
        yield Header()
        with Horizontal(id="upper"):
            yield ClickToAttachDataTable(id="left_pane")
            with VerticalScroll(id="right_pane_scroll"):
                yield Static("", id="right_pane")
        yield Static("(loading squeue...)", id="jobs_panel")
        yield Footer()

    async def on_mount(self) -> None:
        self.table = self.query_one(DataTable)
        self.table.cursor_type = "row"
        self.table.add_columns("NODE", "SESSION", "WIN", "TIME", "CPU", "LOAD", "STATUS")

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

        self.all_sessions: list = []
        self.selected_node = ""
        self.selected_session = ""
        # Two views: 'long' (squeue -l) and 'pending' (squeue -l --start).
        self.jobs_view_mode = 'long'
        self.snapshots: dict = read_snapshots()
        # Per-(node, session, ts) cache of parsed Rich Text — repeating
        # the same row is essentially free.
        self._rendered_cache: dict = {}
        self._last_rows_sig: tuple | None = None
        # Identity of the displayed rows (node, session) in order. When only
        # this is unchanged we update cells in place instead of rebuilding.
        self._last_structural_sig: tuple | None = None
        # Lightweight debounce so a fast burst of ↑↓ doesn't queue up
        # a render per keystroke.
        self._preview_render_timer = None
        self._selection_changed_at = 0.0
        # Pool of pre-warmed ssh slaves — see WarmSlavePool docstring.
        self._warm_pool = WarmSlavePool()

        # Populate immediately (one synchronous read at startup), then keep
        # refreshing on a timer that reads state OFF the event loop.
        initial = read_state()
        self._refresh_table(initial)
        self._refresh_jobs(initial)
        self.set_interval(5, self._refresh_async)
        # Snapshot reload runs in a worker thread to avoid blocking the
        # event loop on filesystem hiccups.
        self.set_interval(30, self._reload_snapshots_async)
        self.run_worker(self._preview_loop(), exclusive=True)

    # ── table refresh ─────────────────────────────────────────────────────────

    async def _refresh_async(self) -> None:
        """Timer entry point: read daemon state OFF the event loop, then
        render. Keeps the 5s tick from freezing the UI on a slow / NFS /
        mid-write state-file read."""
        try:
            state = await asyncio.to_thread(read_state)
        except Exception:
            state = {}
        self._refresh_table(state)
        self._refresh_jobs(state)

    def _update_row_cells(self, i: int, r) -> None:
        """Update only the volatile cells of row i in place. Display columns
        are NODE0 SESSION1 WIN2 TIME3 CPU4 LOAD5 STATUS6, mapped from the row
        tuple (node, session, wins, time, status, cpu, load)."""
        for col, val in ((2, r[2]), (3, r[3]), (4, r[5]), (5, r[6]), (6, r[4])):
            coord = Coordinate(i, col)
            try:
                if self.table.get_cell_at(coord) != val:
                    self.table.update_cell_at(coord, val)
            except Exception:
                pass

    def _dispatch_warm(self, rows) -> None:
        # Keep an idle ssh slave warm for every node in view — pty.fork off
        # the main thread so it never blocks the UI. exclusive=True stops
        # back-to-back refreshes dispatching parallel warm-alls.
        nodes_in_view = {r[0] for r in rows} - {'localhost'}
        self.run_worker(
            self._warm_pool_warm_all_async(nodes_in_view),
            exclusive=True, group='warm-pool',
        )

    def _refresh_table(self, state=None) -> None:
        self._maybe_recover_daemon()
        if state is None:
            state = read_state()
        rows = build_session_rows(state)
        updated = state.get('updated', '?')

        sig = tuple(rows)
        if sig == self._last_rows_sig:
            # Nothing changed at all — just refresh the subtitle.
            self.sub_title = self._status_subtitle(state, rows, updated)
            return
        self._last_rows_sig = sig

        structural = tuple((r[0], r[1]) for r in rows)
        if (structural == self._last_structural_sig
                and len(rows) == self.table.row_count):
            # Same (node, session) rows in the same order; only volatile cells
            # (time/cpu/load/win/status) changed. Update them in place instead
            # of clear()+add_row — the latter resets the cursor and churns
            # RowHighlighted every 5s because the load average ticks constantly.
            self.all_sessions = rows
            for i, r in enumerate(rows):
                self._update_row_cells(i, r)
            self.sub_title = self._status_subtitle(state, rows, updated)
            self._dispatch_warm(rows)
            return
        self._last_structural_sig = structural

        # Structural change — full rebuild. Restore the cursor by
        # (node, session) so it sticks even if rows reorder.
        previous = (self.selected_node, self.selected_session)
        self.all_sessions = rows
        self.table.clear()
        for r in rows:
            # row layout: (node, session, wins, time, status, cpu, load)
            self.table.add_row(r[0], r[1], r[2], r[3], r[5], r[6], r[4])

        if rows:
            new_idx = 0
            for i, r in enumerate(rows):
                if (r[0], r[1]) == previous:
                    new_idx = i
                    break
            self.table.move_cursor(row=new_idx)

        self.sub_title = self._status_subtitle(state, rows, updated)
        self._dispatch_warm(rows)

    @staticmethod
    def _daemon_age_seconds(updated: str):
        """How many seconds ago did the daemon last write? Returns None
        if the timestamp can't be parsed."""
        try:
            from datetime import datetime
            t = datetime.strptime(updated, '%Y-%m-%d %H:%M:%S')
            return (datetime.now() - t).total_seconds()
        except Exception:
            return None

    def _maybe_recover_daemon(self) -> None:
        """Auto-restart a PID-dead daemon (with loop guard); banner-only for a
        hung-but-alive one (the existing stale subtitle covers that, and we
        never auto-kill a daemon that's merely slow)."""
        if _daemon_running():
            self._crash_looping = False
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
        self.notify('daemon down — restarting…', severity='warning', timeout=4)
        self._dispatch_restart()

    def _dispatch_restart(self) -> None:
        self.run_worker(self._restart_daemon_async(),
                        exclusive=True, group='recovery')

    async def _restart_daemon_async(self) -> None:
        await asyncio.to_thread(_launch_daemon)

    def _status_subtitle(self, state, rows, updated) -> str:
        if self._crash_looping:
            return "⚠ daemon crash-looping — run `atd status`"
        if not state.get('nodes'):
            return "waiting for daemon… (run `atd status` to inspect)"
        stale = self._daemon_age_seconds(updated)
        if stale is not None and stale > 30:
            return f"⚠ daemon stale ({stale:.0f}s old) · run `atd status`"
        return f"{len(rows)} sessions · updated {updated}"

    async def _warm_pool_warm_all_async(self, nodes) -> None:
        """pty.fork can be ~10–50ms on a busy login node; off the event loop."""
        await asyncio.to_thread(self._warm_pool.warm_all, nodes)

    async def action_refresh_table(self) -> None:
        await self._refresh_async()
        self._reload_snapshots()

    # ── jobs panel (bottom) ──────────────────────────────────────────────────

    def _refresh_jobs(self, state=None) -> None:
        if state is None:
            state = read_state()
        if self.jobs_view_mode == 'pending':
            text = state.get('squeue_pending', '')
            title = '── PENDING JOBS (squeue --start)  [j: switch view] ──'
        else:
            text = state.get('squeue_long', '')
            title = '── ALL JOBS (squeue -l)  [j: switch view] ──'
        if not text.strip():
            text = '(no squeue data yet — daemon may still be starting)'
        updated = state.get('squeue_updated', '?')
        self.jobs_view.update(f'{title}  updated {updated}\n{text}')

    async def action_toggle_jobs_view(self) -> None:
        self.jobs_view_mode = 'pending' if self.jobs_view_mode == 'long' else 'long'
        self._refresh_jobs()

    # ── persistent snapshot cache ────────────────────────────────────────────

    def _reload_snapshots(self) -> None:
        self.snapshots = read_snapshots()

    async def _reload_snapshots_async(self) -> None:
        # Filesystem could be slow / NFS-y; never block the event loop on it.
        try:
            self.snapshots = await asyncio.to_thread(read_snapshots)
        except Exception:
            pass

    def _show_cached_snapshot(self, node: str, session: str) -> bool:
        snap = self.snapshots.get(f'{node}:{session}')
        if not isinstance(snap, dict):
            return False
        text = snap.get('lines') or ''
        ts = snap.get('ts', '?')
        if not text:
            return False
        cache_key = (node, session, ts)
        # LRU: pop on hit and reinsert so it moves to the end of insertion order.
        rendered = self._rendered_cache.pop(cache_key, None)
        if rendered is None:
            base = rich.text.Text.from_ansi(text)
            rendered = rich.text.Text(f'(cached snapshot · {ts})\n', style='dim') + base
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
        if not (0 <= idx < len(self.all_sessions)):
            return None
        return self.all_sessions[idx][0], self.all_sessions[idx][1]

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
        if sess in ('<Start Shell>', '<offline>'):
            self.log_view.update("")
            return
        if not self._show_cached_snapshot(node, sess):
            self.log_view.update(f"Loading preview  {node}:{sess} …")

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Emitted by Enter and by a single mouse click (see
        # ClickToAttachDataTable). Resolve the target straight from the event
        # so the attach can't race a not-yet-processed RowHighlighted.
        target = self._row_target(event.row_key)
        if target is not None:
            self.selected_node, self.selected_session = target
        await self.action_attach_session()

    # ── live preview (the ONLY network call in the frontend) ─────────────────

    async def _spawn_preview_capture(self, node: str, sess: str):
        """Spawn the `tmux capture-pane` fetch for the preview pane.

        CRITICAL: stdin MUST be DEVNULL (and ssh gets -n). `ssh host cmd`
        reads local stdin by default and forwards it to the remote command,
        so an inherited terminal stdin makes every 1s preview ssh *eat the
        user's keystrokes* — you have to press a key several times before one
        reaches the TUI. DEVNULL/-n keeps the terminal input for the app.
        """
        if node == 'localhost':
            return await asyncio.create_subprocess_exec(
                'tmux', 'capture-pane', '-p', '-e', '-t', sess,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        ctl = _get_ssh_args(node)
        return await asyncio.create_subprocess_exec(
            'ssh', '-n', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
            *ctl, node,
            f"tmux capture-pane -p -e -t {shlex.quote(sess)}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _preview_loop(self) -> None:
        """Spawn `tmux capture-pane` for the highlighted row.

        Carefully avoids leaking ssh processes: on timeout we explicitly
        kill the subprocess and reap it (asyncio.wait_for cancels the
        await but does NOT kill the OS process). And we skip the live
        fetch entirely if the user is actively navigating, so a fast burst
        of ↑/↓ doesn't spawn 10 ssh's that all time out 3s later.
        """
        last_key = ""
        last_hash = ""
        # Per-(node, session) consecutive timeout counter — backs off
        # live preview for sessions on overloaded nodes.
        timeout_counts: dict = {}
        backoff_until: dict = {}
        import time as _time
        while True:
            # Outer guard: one bad iteration (anything outside the fetch
            # block too) must never kill the preview worker for the rest of
            # the session — Textual does not restart a dead worker.
            try:
                await asyncio.sleep(1.0)
                node = self.selected_node
                sess = self.selected_session
                if not node or not sess or sess in ('<Start Shell>', '<offline>'):
                    continue
                # Skip if the user is still navigating quickly.
                if _time.monotonic() - getattr(self, '_selection_changed_at', 0) < 0.5:
                    continue
                key = f"{node}:{sess}"
                # Honor per-session backoff after repeated timeouts.
                if backoff_until.get(key, 0) > _time.monotonic():
                    continue

                proc = None
                try:
                    proc = await self._spawn_preview_capture(node, sess)
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=4)
                    content = stdout.decode(errors='replace')
                    # Successful fetch — reset failure counter for this session.
                    timeout_counts.pop(key, None)
                    backoff_until.pop(key, None)

                    if (node, sess) != (self.selected_node, self.selected_session):
                        continue
                    h = hashlib.md5(content.encode()).hexdigest()
                    if key == last_key and h == last_hash:
                        continue
                    last_key, last_hash = key, h
                    self.log_view.update(rich.text.Text.from_ansi(content))
                except asyncio.TimeoutError:
                    # Reap the runaway ssh — otherwise it accumulates as a
                    # zombie eating CPU on the login node.
                    if proc is not None:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                    n = timeout_counts.get(key, 0) + 1
                    timeout_counts[key] = n
                    # After 2 consecutive timeouts, stop probing this session
                    # for 60s — the remote is too overloaded to be useful.
                    if n >= 2:
                        backoff_until[key] = _time.monotonic() + 60
                except Exception:
                    # ssh missing, decode error, etc. Reap and back this
                    # session off too, so a persistently-failing fetch isn't
                    # respawned every second with no feedback.
                    if proc is not None:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                    n = timeout_counts.get(key, 0) + 1
                    timeout_counts[key] = n
                    if n >= 2:
                        backoff_until[key] = _time.monotonic() + 60
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    # ── interactive actions ──────────────────────────────────────────────────

    async def action_attach_session(self) -> None:
        node, sess = self.selected_node, self.selected_session
        if not node or not sess or sess == '<offline>':
            return
        used_warm = False
        with self.suspend():
            if node == 'localhost':
                if sess == '<Start Shell>':
                    subprocess.call([os.environ.get('SHELL', '/bin/bash')])
                else:
                    subprocess.call(['tmux', 'attach', '-t', sess])
            elif sess == '<Start Shell>':
                sys.stdout.write(f"\n[atmux] connecting to {node}…\n")
                sys.stdout.flush()
                base = ['ssh'] + _get_ssh_args(node) + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node]
                subprocess.call(base)
            else:
                # Try the pre-warmed ssh slave first — instant if available.
                used_warm = self._warm_pool.attach(node, sess)
                if not used_warm:
                    sys.stdout.write(f"\n[atmux] connecting to {node}:{sess}…\n")
                    sys.stdout.flush()
                    base = ['ssh'] + _get_ssh_args(node) + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node]
                    subprocess.call(base + ['tmux', 'attach', '-t', shlex.quote(sess)])
        # Replace the (now-consumed) warm slave so the next attach is fast too.
        if node != 'localhost' and sess not in ('<Start Shell>',):
            self._warm_pool.warm(node)

    async def action_open_shell(self) -> None:
        node = self.selected_node
        if not node:
            return
        with self.suspend():
            if node == 'localhost':
                subprocess.call([os.environ.get('SHELL', '/bin/bash')])
            else:
                sys.stdout.write(f"\n[atmux] connecting to {node}…\n")
                sys.stdout.flush()
                base = ['ssh'] + _get_ssh_args(node) + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node]
                subprocess.call(base)

    async def action_local_shell(self) -> None:
        with self.suspend():
            subprocess.call(['tmux', 'new-session', '-A', '-s', 'autotmux_local'])

    async def on_unmount(self) -> None:
        # Tear down warm slaves so we don't leave orphan ssh processes.
        try:
            self._warm_pool.shutdown()
        except Exception:
            pass

    async def action_new_window(self) -> None:
        node, sess = self.selected_node, self.selected_session
        if not node or not sess or sess == '<offline>':
            return
        if node == 'localhost':
            cmd = [os.environ.get('SHELL', '/bin/bash')] if sess == '<Start Shell>' else ['tmux', 'attach', '-t', sess]
        else:
            base = ['ssh'] + _get_ssh_args(node) + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node]
            cmd = base if sess == '<Start Shell>' else base + ['tmux', 'attach', '-t', shlex.quote(sess)]

        if os.environ.get('TMUX'):
            wname = f"{node}-{sess}" if sess != '<Start Shell>' else f"{node}-shell"
            subprocess.call(['tmux', 'new-window', '-n', wname, *cmd],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            with self.suspend():
                print("\n[AutoTmux] Not inside tmux – falling back to direct attach.")
                time.sleep(1)
                subprocess.call(cmd)


def _daemon_running() -> bool:
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
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


def _launch_daemon() -> None:
    """Start the daemon if it isn't already running.

    Prefer the installed `atd` console script; fall back to running the
    module directly so this works in dev / unusual PATH setups.
    """
    if _daemon_running():
        return
    atd = shutil.which('atd')
    cmd = [atd, 'start'] if atd else [sys.executable, '-m', 'autotmux.daemon', 'start']
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


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
    _launch_daemon()
    if node == 'localhost':
        return subprocess.call(['tmux', 'attach', '-t', session])
    base = ['ssh'] + _get_ssh_args(node) + ['-o', 'StrictHostKeyChecking=accept-new', '-t', node]
    return subprocess.call(base + ['tmux', 'attach', '-t', shlex.quote(session)])


def _build_argparser():
    import argparse
    p = argparse.ArgumentParser(
        prog='atmux',
        description='AutoTmux — terminal dashboard for tmux sessions across slurm nodes.',
    )
    p.add_argument('--version', action='version', version=f'AutoTmux {__version__}')
    p.add_argument('-a', '--attach', metavar='NODE:SESSION',
                   help='Skip the TUI and attach directly to NODE:SESSION.')
    p.add_argument('--no-mouse', action='store_true',
                   help='Disable mouse support entirely (keyboard-only). Use '
                        'over a laggy SSH link if keys still feel unresponsive.')
    return p


def main():
    args = _build_argparser().parse_args()
    if args.attach:
        sys.exit(_direct_attach(args.attach))
    _launch_daemon()
    AutotmuxApp().run(mouse=not args.no_mouse)


if __name__ == "__main__":
    main()
