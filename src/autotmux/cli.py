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

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
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
                except Exception:
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
        """Tear down every warm slave. Called from on_unmount."""
        for node in list(self._slaves):
            self._cleanup(node)

    # ── attach path ─────────────────────────────────────────────────────────

    def attach(self, node: str, session: str) -> bool:
        """Try to attach using a warm slave. Returns True if the proxy ran
        (the user's terminal was handed over and they've now detached);
        False if no warm slave was available — caller should fall back."""
        slave = self._take(node)
        if not slave:
            return False
        pid, master_fd = slave
        try:
            # Drain whatever the remote bash already printed (welcome
            # banner, prompt, etc.) so the user sees a clean tmux paint.
            self._drain(master_fd)
            cmd = f'exec tmux attach -t {shlex.quote(session)}\n'
            try:
                os.write(master_fd, cmd.encode())
            except OSError:
                return False
            self._proxy(master_fd, pid)
        finally:
            self._reap_child(pid)
            try:
                os.close(master_fd)
            except OSError:
                pass
        return True

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
        pid, _ = slave
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == 0:
                return True
        except OSError:
            pass
        # Process gone — drop without re-locking.
        self._slaves.pop(node, None)
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
                        break
                    os.write(1, data)
                # Has the ssh process exited?
                try:
                    pid, _ = os.waitpid(child_pid, os.WNOHANG)
                    if pid != 0:
                        # Drain any final output before we leave.
                        try:
                            data = os.read(master_fd, 8192)
                            if data:
                                os.write(1, data)
                        except OSError:
                            pass
                        break
                except OSError:
                    break
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


class AutotmuxApp(App):
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="upper"):
            yield DataTable(id="left_pane")
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
        # Lightweight debounce so a fast burst of ↑↓ doesn't queue up
        # a render per keystroke.
        self._preview_render_timer = None
        self._selection_changed_at = 0.0
        # Pool of pre-warmed ssh slaves — see WarmSlavePool docstring.
        self._warm_pool = WarmSlavePool()

        # Populate immediately, then keep refreshing
        self._refresh_table()
        self._refresh_jobs()
        self.set_interval(5, self._refresh_table)
        self.set_interval(10, self._refresh_jobs)
        # Snapshot reload runs in a worker thread to avoid blocking the
        # event loop on filesystem hiccups.
        self.set_interval(30, self._reload_snapshots_async)
        self.run_worker(self._preview_loop(), exclusive=True)

    # ── table refresh (pure local file read, <1ms) ───────────────────────────

    def _refresh_table(self) -> None:
        state = read_state()
        rows = build_session_rows(state)
        updated = state.get('updated', '?')

        sig = tuple(rows)
        if sig == self._last_rows_sig:
            # Hot path — rows haven't changed, skip the expensive rebuild
            # (every clear+add_row triggers RowHighlighted churn).
            if not state.get('nodes'):
                self.sub_title = "waiting for daemon… (run `atd status` to inspect)"
            else:
                self.sub_title = f"{len(rows)} sessions · updated {updated}"
            return
        self._last_rows_sig = sig

        # Restore by (node, session) so the cursor sticks even if rows reorder.
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

        if not state.get('nodes'):
            self.sub_title = "waiting for daemon… (run `atd status` to inspect)"
        else:
            stale = self._daemon_age_seconds(updated)
            if stale is not None and stale > 30:
                self.sub_title = f"⚠ daemon stale ({stale:.0f}s old) · run `atd status`"
            else:
                self.sub_title = f"{len(rows)} sessions · updated {updated}"
        # Keep an idle ssh slave warm for every node we know about — but
        # do the pty.fork off the main thread so it never blocks the UI.
        # exclusive=True ensures back-to-back refreshes don't dispatch
        # parallel warm-alls (which could race even with the per-pool lock
        # by re-spawning slaves between checks).
        nodes_in_view = {r[0] for r in rows} - {'localhost'}
        self.run_worker(
            self._warm_pool_warm_all_async(nodes_in_view),
            exclusive=True, group='warm-pool',
        )

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

    async def _warm_pool_warm_all_async(self, nodes) -> None:
        """pty.fork can be ~10–50ms on a busy login node; off the event loop."""
        await asyncio.to_thread(self._warm_pool.warm_all, nodes)

    async def action_refresh_table(self) -> None:
        self._refresh_table()
        self._refresh_jobs()
        self._reload_snapshots()

    # ── jobs panel (bottom) ──────────────────────────────────────────────────

    def _refresh_jobs(self) -> None:
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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        try:
            idx = self.table.get_row_index(event.row_key)
        except Exception:
            return
        if not (0 <= idx < len(self.all_sessions)):
            return
        new_node = self.all_sessions[idx][0]
        new_sess = self.all_sessions[idx][1]
        # Skip when the highlight event is just from a refresh that landed
        # on the same row — clearing here is what caused the 5s preview flicker.
        if (new_node, new_sess) == (self.selected_node, self.selected_session):
            return
        self.selected_node = new_node
        self.selected_session = new_sess
        self._selection_changed_at = time.monotonic()
        # Coalesce bursts of ↑/↓ into a single repaint. The cursor itself
        # moves immediately; only the right-pane preview waits 30 ms.
        if self._preview_render_timer is not None:
            self._preview_render_timer.stop()
        self._preview_render_timer = self.set_timer(0.03, self._render_preview_now)

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
        # DataTable owns Enter and emits this; route to attach.
        await self.action_attach_session()

    # ── live preview (the ONLY network call in the frontend) ─────────────────

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
                if node == 'localhost':
                    proc = await asyncio.create_subprocess_exec(
                        'tmux', 'capture-pane', '-p', '-e', '-t', sess,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    ctl = _get_ssh_args(node)
                    proc = await asyncio.create_subprocess_exec(
                        'ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                        *ctl, node,
                        f"tmux capture-pane -p -e -t {shlex.quote(sess)}",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
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
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass

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


def _should_restart(attempts, now: float, window: float = 60.0,
                    limit: int = 3) -> bool:
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
    return p


def main():
    args = _build_argparser().parse_args()
    if args.attach:
        sys.exit(_direct_attach(args.attach))
    _launch_daemon()
    AutotmuxApp().run()


if __name__ == "__main__":
    main()
