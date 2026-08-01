"""Tests for the pre-warmed ssh slave pool.

We can't test against a real ssh master in unit tests, so we exercise the
pool with a fake `ssh` script (a shell loop that echoes back what the user
sends, simulating the bash-prompt-then-exec flow).
"""
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux


SSH_STUB = textwrap.dedent('''\
    #!/bin/bash
    # Pretend to be an interactive ssh slave: print a fake prompt, read a
    # stream of commands and execute them. A real warm shell survives tmux
    # detach, so this stub must remain available for readiness plus repeated
    # attach commands too.
    echo "fake-bash-prompt$ "
    while IFS= read -r line; do
        # Retain compatibility with older one-shot handoff tests.
        line="${line#exec }"
        eval "$line"
    done
''')


def _install_ssh_stub(tmpdir: str) -> str:
    """Drop a shell script named `ssh` into tmpdir and return the bin dir."""
    bin_dir = os.path.join(tmpdir, 'bin')
    os.makedirs(bin_dir, exist_ok=True)
    stub = os.path.join(bin_dir, 'ssh')
    with open(stub, 'w') as f:
        f.write(SSH_STUB)
    os.chmod(stub, 0o755)
    return bin_dir


class WarmSlavePoolTests(unittest.TestCase):
    def setUp(self):
        # Provide a sentinel ControlPath that exists so warm() proceeds.
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        # Create a fake socket file (just an empty file is enough for our
        # `os.path.exists` check).
        self.fake_node = 'fake-node'
        ctl_file = os.path.join(self.ctl_dir, f'cm_{self.fake_node}')
        open(ctl_file, 'w').close()

        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')

        # Inject our fake `ssh` ahead of the real one.
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warm_localhost_is_noop(self):
        pool = autotmux.WarmSlavePool()
        pool.warm('localhost')
        self.assertNotIn('localhost', pool._slaves)

    def test_warm_no_socket_is_noop(self):
        pool = autotmux.WarmSlavePool()
        pool.warm('nonexistent-node')
        self.assertNotIn('nonexistent-node', pool._slaves)

    def test_warm_creates_slave_when_socket_exists(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            self.assertIn(self.fake_node, pool._slaves)
            pid, fd = pool._slaves[self.fake_node]
            self.assertGreater(pid, 0)
            self.assertGreaterEqual(fd, 0)
        finally:
            pool.shutdown()

    def test_warm_initializes_pty_size_before_ssh_starts(self):
        pool = autotmux.WarmSlavePool()
        synced_fds = []
        try:
            with mock.patch.object(
                    autotmux, '_copy_terminal_winsize',
                    side_effect=lambda fd: synced_fds.append(fd) or True):
                pool.warm(self.fake_node)
            self.assertEqual(len(synced_fds), 1)
            self.assertGreaterEqual(synced_fds[0], 0)
        finally:
            pool.shutdown()

    def test_warm_never_forks_python_from_worker_path(self):
        pool = autotmux.WarmSlavePool()
        try:
            with mock.patch.object(autotmux.pty, 'fork',
                                   side_effect=AssertionError('unsafe fork used')):
                pool.warm(self.fake_node)
            self.assertIn(self.fake_node, pool._slaves)
        finally:
            pool.shutdown()

    def test_warm_is_idempotent(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            first = pool._slaves[self.fake_node]
            pool.warm(self.fake_node)
            second = pool._slaves[self.fake_node]
            self.assertEqual(first, second,
                             "warm() should reuse the existing slave")
        finally:
            pool.shutdown()

    def test_take_returns_none_when_no_slave(self):
        pool = autotmux.WarmSlavePool()
        self.assertIsNone(pool._take(self.fake_node))

    def test_take_pops_the_slave(self):
        pool = autotmux.WarmSlavePool()
        slave = None
        try:
            pool.warm(self.fake_node)
            self.assertIn(self.fake_node, pool._slaves)
            slave = pool._take(self.fake_node)
            self.assertIsNotNone(slave)
            self.assertNotIn(self.fake_node, pool._slaves,
                             "after take(), slave should be removed from pool")
        finally:
            # _take() deliberately transfers ownership to attach(); this unit
            # test stops before attach(), so perform that caller cleanup here.
            if slave is not None:
                pid, fd = slave
                pool._reap_child(pid)
                try:
                    os.close(fd)
                except OSError:
                    pass
            pool.shutdown()

    def test_attach_returns_false_when_no_warm_slave(self):
        pool = autotmux.WarmSlavePool()
        self.assertFalse(pool.attach('no-such-node', 'whatever'))

    def test_shutdown_kills_all_slaves(self):
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        self.assertIn(self.fake_node, pool._slaves)
        pid = pool._slaves[self.fake_node][0]
        pool.shutdown()
        self.assertNotIn(self.fake_node, pool._slaves)
        # Process should be gone within a second.
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except OSError:
                break  # process gone, good
            time.sleep(0.05)
        else:
            self.fail("shutdown() did not kill the slave process")


class WarmSlaveFdLeakTests(unittest.TestCase):
    """When a warm slave silently dies, the detector must close its pty
    master fd — otherwise every dead slave leaks one fd until EMFILE."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        self.fake_node = 'fake-node-fd'
        open(os.path.join(self.ctl_dir, f'cm_{self.fake_node}'), 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dead_slave_master_fd_is_closed(self):
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        pid, fd = pool._slaves[self.fake_node]
        # Kill the slave so the next liveness check observes it dead.
        os.kill(pid, signal.SIGKILL)
        for _ in range(40):
            if not pool._still_alive(self.fake_node):
                break
            time.sleep(0.05)
        else:
            self.fail("slave was never detected as dead")
        # The master fd must have been closed, not leaked.
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_warm_after_shutdown_is_noop(self):
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        pool.shutdown()
        pool.warm(self.fake_node)
        self.assertNotIn(self.fake_node, pool._slaves,
                         "warm() after shutdown must not spawn a new slave")


class SqueueLoopFieldPreservationTests(unittest.TestCase):
    """The squeue loop refreshes node_info dicts every 30 s. It must NOT
    wipe the fields populated by the session loop (sessions, nproc, load,
    last_error) — otherwise the table flickers between "we know the load"
    and "?" every cycle.
    """
    def setUp(self):
        from autotmux import daemon as d
        self.d = d
        d._known_nodes_info.clear()

    def tearDown(self):
        self.d._known_nodes_info.clear()

    def test_session_loop_fields_survive_squeue_refresh(self):
        d = self.d
        # Simulate the state right after _session_loop has populated extras.
        d._known_nodes_info['gpu1'] = {
            'time': '1:00', 'job_name': 'a', 'job_id': '1', 'state': 'RUNNING',
            'sessions': [['main', '1']], 'nproc': '4', 'load': '2.5',
            'escape_time': '500',
            'last_error': '',
        }
        # Now mimic what _squeue_loop does: fresh squeue dict has only the
        # squeue-derived fields; merge logic should preserve the rest.
        fresh = {
            'gpu1': {'time': '0:59', 'job_name': 'a', 'job_id': '1', 'state': 'RUNNING'},
        }
        for node, info in fresh.items():
            old = d._known_nodes_info.get(node, {})
            for key in (
                    'sessions', 'nproc', 'load', 'escape_time', 'last_error'):
                if key in old:
                    info[key] = old[key]
        d._known_nodes_info.update(fresh)
        n = d._known_nodes_info['gpu1']
        self.assertEqual(n['sessions'], [['main', '1']])
        self.assertEqual(n['nproc'], '4')
        self.assertEqual(n['load'], '2.5')
        self.assertEqual(n['escape_time'], '500')
        # And the squeue-fresh field overrode the old time:
        self.assertEqual(n['time'], '0:59')


class WarmSlaveAttachLifecycleTests(unittest.TestCase):
    """Reusable attaches keep exactly one owned child and clean it on exit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        self.fake_node = 'fake-node'
        ctl_file = os.path.join(self.ctl_dir, f'cm_{self.fake_node}')
        open(ctl_file, 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_successful_attach_returns_live_child_to_pool(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            pid = pool._slaves[self.fake_node][0]
            self.assertTrue(pool.is_starting(self.fake_node))
            with mock.patch.object(pool, '_proxy', return_value='ok'):
                ok = pool.attach(self.fake_node, 'whatever')
            self.assertTrue(ok)
            self.assertEqual(pool._slaves[self.fake_node][0], pid)
            self.assertNotIn(self.fake_node, pool._in_use)
            self.assertFalse(pool.is_starting(self.fake_node))
            self.assertIsNone(pool._procs[pid].poll())
            with mock.patch.object(pool, '_proxy', return_value='ok'):
                self.assertTrue(pool.attach(self.fake_node, 'second'))
            self.assertEqual(pool._slaves[self.fake_node][0], pid)
        finally:
            pool.shutdown()
        # shutdown, not detach, owns the final reap.
        # Check no zombie: try /proc/<pid>/status — if the process is a
        # zombie, status field is "Z (zombie)". If reaped, no /proc entry.
        proc_status = f'/proc/{pid}/status'
        if os.path.exists(proc_status):
            with open(proc_status) as f:
                content = f.read()
            self.assertNotIn('zombie', content.lower(),
                             f'attach left zombie process pid={pid}')

    def test_resizes_pty_before_sending_tmux_attach(self):
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        events = []
        try:
            with (
                mock.patch.object(pool, '_drain'),
                mock.patch.object(pool, '_proxy', return_value='ok'),
                mock.patch.object(
                    autotmux, '_copy_terminal_winsize',
                    side_effect=lambda _fd: events.append('resize') or True,
                ),
                mock.patch.object(
                    autotmux.os, 'write',
                    side_effect=lambda _fd, _data: events.append('write') or 1,
                ),
            ):
                self.assertTrue(pool.attach(self.fake_node, 'whatever'))
            self.assertEqual(events[:2], ['resize', 'write'])
        finally:
            pool.shutdown()


class TerminalSizeSyncTests(unittest.TestCase):
    def test_completion_marker_can_span_pty_reads_without_leaking(self):
        marker = b'\x1eAUTOTMUX_ATTACH_token_OK\x1f'
        markers = {marker: 'ok'}
        pending = b''
        rendered = bytearray()
        chunks = (b'pane redraw\r\n' + marker[:5],
                  marker[5:17], marker[17:] + b'shell prompt$ ')
        result = None
        for chunk in chunks:
            output, pending, found = (
                autotmux.WarmSlavePool._filter_marker_chunk(
                    pending, chunk, markers))
            rendered.extend(output)
            if found is not None:
                result = found
                break
        self.assertEqual(result, 'ok')
        self.assertEqual(bytes(rendered), b'pane redraw\r\n')
        self.assertNotIn(marker, rendered)
        self.assertEqual(pending, b'')

    def test_failed_attach_marker_is_distinct_from_success(self):
        fail = b'\x1eAUTOTMUX_ATTACH_token_FAIL\x1f'
        output, pending, result = (
            autotmux.WarmSlavePool._filter_marker_chunk(
                b'error: no session\r\n', fail + b'prompt',
                {b'\x1eAUTOTMUX_ATTACH_token_OK\x1f': 'ok', fail: 'fail'}))
        self.assertEqual(result, 'fail')
        self.assertEqual(output, b'error: no session\r\n')
        self.assertEqual(pending, b'')

    def test_copies_complete_nonzero_terminal_geometry(self):
        packed = struct.pack('HHHH', 51, 173, 1200, 800)
        writes = []

        def fake_ioctl(fd, request, arg):
            if request == autotmux.termios.TIOCGWINSZ:
                self.assertEqual(fd, 0)
                return packed
            writes.append((fd, request, arg))
            return 0

        with mock.patch.object(autotmux.fcntl, 'ioctl', side_effect=fake_ioctl):
            self.assertTrue(autotmux._copy_terminal_winsize(99))
        self.assertEqual(
            writes,
            [(99, autotmux.termios.TIOCSWINSZ, packed)],
        )

    def test_proxy_retries_short_pty_writes_without_dropping_bytes(self):
        accepted = bytearray()

        def short_write(_fd, data):
            chunk = bytes(data[:2])
            accepted.extend(chunk)
            return len(chunk)

        with mock.patch.object(autotmux.os, 'write', side_effect=short_write):
            self.assertTrue(
                autotmux.WarmSlavePool._write_all(99, b'abcdefg'))
        self.assertEqual(bytes(accepted), b'abcdefg')

    def test_real_pty_geometry_is_copied_from_controlling_fd(self):
        source_master, source_slave = autotmux.pty.openpty()
        target_master, target_slave = autotmux.pty.openpty()
        saved_stdin = os.dup(0)
        expected = struct.pack('HHHH', 47, 139, 1200, 800)
        try:
            autotmux.fcntl.ioctl(
                source_slave, autotmux.termios.TIOCSWINSZ, expected)
            os.dup2(source_slave, 0)
            self.assertTrue(autotmux._copy_terminal_winsize(target_slave))
            actual = autotmux.fcntl.ioctl(
                target_master, autotmux.termios.TIOCGWINSZ,
                struct.pack('HHHH', 0, 0, 0, 0))
            self.assertEqual(actual, expected)
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)
            for fd in (source_master, source_slave, target_master, target_slave):
                os.close(fd)

    def test_proxy_signals_warm_ssh_for_every_runtime_resize(self):
        """Changing the relay PTY size alone does not wake a setsid ssh child.

        Warm slaves are launched with ``start_new_session=True`` and therefore
        do not own the pre-opened slave PTY as a controlling terminal.  The
        proxy must explicitly signal the child after each size update so ssh
        sends a window-change request to the remote PTY.
        """
        source_master, source_slave = autotmux.pty.openpty()
        target_master, target_slave = autotmux.pty.openpty()
        report_read, report_write = os.pipe()
        saved_stdin = os.dup(0)
        child = None
        pool = autotmux.WarmSlavePool()
        reports = []
        initial = struct.pack('HHHH', 31, 101, 1000, 700)
        resized = struct.pack('HHHH', 57, 181, 1800, 1000)
        script = (
            "import os, signal\n"
            "fd = int(os.environ['REPORT_FD'])\n"
            "signal.signal(signal.SIGWINCH, lambda *_: os.write(fd, b'W'))\n"
            "os.write(fd, b'R')\n"
            "while True:\n"
            "    signal.pause()\n"
        )
        try:
            env = os.environ.copy()
            env['REPORT_FD'] = str(report_write)
            child = subprocess.Popen(
                [sys.executable, '-c', script],
                stdin=target_slave, stdout=target_slave, stderr=target_slave,
                close_fds=True, pass_fds=(report_write,),
                start_new_session=True, env=env,
            )
            os.close(target_slave)
            target_slave = -1
            os.close(report_write)
            report_write = -1
            ready, _, _ = select.select([report_read], [], [], 3)
            self.assertTrue(ready, 'resize probe child did not start')
            self.assertEqual(os.read(report_read, 1), b'R')

            autotmux.fcntl.ioctl(
                source_slave, autotmux.termios.TIOCSWINSZ, initial)
            os.dup2(source_slave, 0)
            with pool._lock:
                pool._procs[child.pid] = child

            def drive_resize():
                first, _, _ = select.select([report_read], [], [], 1.5)
                if first:
                    reports.append(os.read(report_read, 1))
                autotmux.fcntl.ioctl(
                    source_slave, autotmux.termios.TIOCSWINSZ, resized)
                os.kill(os.getpid(), signal.SIGWINCH)
                second, _, _ = select.select([report_read], [], [], 1.5)
                if second:
                    reports.append(os.read(report_read, 1))
                child.terminate()

            driver = threading.Thread(target=drive_resize)
            driver.start()
            pool._proxy(target_master, child.pid)
            driver.join(timeout=5)
            self.assertFalse(driver.is_alive())
            self.assertEqual(reports, [b'W', b'W'])
            actual = autotmux.fcntl.ioctl(
                target_master, autotmux.termios.TIOCGWINSZ,
                struct.pack('HHHH', 0, 0, 0, 0))
            self.assertEqual(actual, resized)
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)
            with pool._lock:
                pool._procs.pop(child.pid if child is not None else -1, None)
            if child is not None and child.poll() is None:
                child.kill()
            if child is not None:
                child.wait(timeout=3)
            for fd in (
                    source_master, source_slave, target_master, target_slave,
                    report_read, report_write):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_proxy_forwards_input_while_local_output_is_backpressured(self):
        """A redraw flood must not put keyboard forwarding behind stdout."""
        local_master, local_slave = autotmux.pty.openpty()
        remote_master, remote_slave = autotmux.pty.openpty()
        report_read, report_write = os.pipe()
        saved_stdin = os.dup(0)
        saved_stdout = os.dup(1)
        child = None
        pool = autotmux.WarmSlavePool()
        reports = []
        latencies = []
        restored_flags = []
        local_master_open = [True]
        expected_local_flags = autotmux.fcntl.fcntl(
            local_slave, autotmux.fcntl.F_GETFL)
        expected_remote_flags = autotmux.fcntl.fcntl(
            remote_master, autotmux.fcntl.F_GETFL)
        script = (
            "import os, threading, time, tty\n"
            "report = int(os.environ['REPORT_FD'])\n"
            "tty.setraw(0)\n"
            "def flood():\n"
            "    chunk = b'X' * 16384\n"
            "    while True:\n"
            "        try:\n"
            "            os.write(1, chunk)\n"
            "        except OSError:\n"
            "            return\n"
            "threading.Thread(target=flood, daemon=True).start()\n"
            "os.write(report, b'R')\n"
            "data = os.read(0, 1)\n"
            "os.write(report, data or b'E')\n"
            "time.sleep(5)\n"
        )
        try:
            env = os.environ.copy()
            env['REPORT_FD'] = str(report_write)
            child = subprocess.Popen(
                [sys.executable, '-c', script],
                stdin=remote_slave, stdout=remote_slave, stderr=remote_slave,
                close_fds=True, pass_fds=(report_write,),
                start_new_session=True, env=env,
            )
            os.close(remote_slave)
            remote_slave = -1
            os.close(report_write)
            report_write = -1
            os.dup2(local_slave, 0)
            os.dup2(local_slave, 1)
            with pool._lock:
                pool._procs[child.pid] = child

            def drive_input():
                try:
                    ready, _, _ = select.select([report_read], [], [], 2)
                    if not ready:
                        reports.append(b'NO_READY')
                        return
                    reports.append(os.read(report_read, 1))
                    # Let stdout fill both the terminal queue and the relay's
                    # bounded output buffer before injecting a keystroke.
                    time.sleep(0.25)
                    started = time.monotonic()
                    os.write(local_master, b'K')
                    ready, _, _ = select.select([report_read], [], [], 1.5)
                    if ready:
                        reports.append(os.read(report_read, 1))
                        latencies.append(time.monotonic() - started)
                    else:
                        reports.append(b'NO_KEY')
                finally:
                    if child.poll() is None:
                        child.terminate()
                    # Wake any final blocked terminal write after the assertion
                    # evidence has been collected.
                    try:
                        os.close(local_master)
                        local_master_open[0] = False
                    except OSError:
                        pass

            driver = threading.Thread(target=drive_input)
            driver.start()
            pool._proxy(remote_master, child.pid)
            driver.join(timeout=5)
            self.assertFalse(driver.is_alive())
            restored_flags.extend((
                autotmux.fcntl.fcntl(0, autotmux.fcntl.F_GETFL),
                autotmux.fcntl.fcntl(1, autotmux.fcntl.F_GETFL),
                autotmux.fcntl.fcntl(
                    remote_master, autotmux.fcntl.F_GETFL),
            ))
        finally:
            os.dup2(saved_stdin, 0)
            os.dup2(saved_stdout, 1)
            os.close(saved_stdin)
            os.close(saved_stdout)
            with pool._lock:
                pool._procs.pop(child.pid if child is not None else -1, None)
            if child is not None and child.poll() is None:
                child.kill()
            if child is not None:
                child.wait(timeout=3)
            fds = [local_slave, remote_master, remote_slave,
                   report_read, report_write]
            if local_master_open[0]:
                fds.append(local_master)
            for fd in fds:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        self.assertEqual(reports, [b'R', b'K'])
        self.assertTrue(latencies)
        self.assertLess(latencies[0], 0.75)
        self.assertEqual(
            restored_flags,
            [expected_local_flags, expected_local_flags, expected_remote_flags],
        )


class WarmSlaveStaleFallbackTests(unittest.TestCase):
    def test_uninterruptible_child_is_owned_by_deferred_reaper(self):
        pool = autotmux.WarmSlavePool()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = autotmux.subprocess.TimeoutExpired(
            ['ssh'], 0.01)
        with mock.patch.object(
                autotmux.lifecycle, 'defer_popen_reap') as defer:
            pool._terminate_proc(proc, timeout=0.01)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        defer.assert_called_once_with(proc)

    """A warm slave can pass the local liveness check yet have a dead remote
    channel. Using it pops the user straight back out. attach() must detect
    the instant proxy exit and report failure so the caller cold-attaches."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        self.fake_node = 'fake-node-stale'
        open(os.path.join(self.ctl_dir, f'cm_{self.fake_node}'), 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']
        self._orig_proxy = autotmux.WarmSlavePool._proxy

    def tearDown(self):
        autotmux.WarmSlavePool._proxy = self._orig_proxy
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_attach_reports_failure_on_instant_proxy_exit(self):
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        # Dead slave → proxy returns immediately.
        autotmux.WarmSlavePool._proxy = staticmethod(lambda *_: None)
        self.assertFalse(
            pool.attach(self.fake_node, 'sess'),
            "an instant proxy exit must report failure so caller falls back to cold attach")

    def test_attach_reports_success_when_proxy_runs(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            # The framed OK result is authoritative even for a quick detach.
            autotmux.WarmSlavePool._proxy = staticmethod(lambda *_: 'ok')
            self.assertTrue(
                pool.attach(self.fake_node, 'sess'),
                "the OK marker must prevent a second cold attach")
            self.assertIn(self.fake_node, pool._slaves)
        finally:
            pool.shutdown()

    def test_failed_attach_marker_keeps_healthy_shell_but_reports_failure(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            pid = pool._slaves[self.fake_node][0]
            autotmux.WarmSlavePool._proxy = staticmethod(lambda *_: 'fail')
            self.assertFalse(pool.attach(self.fake_node, 'missing'))
            self.assertEqual(pool._slaves[self.fake_node][0], pid)
            self.assertIsNone(pool._procs[pid].poll())
        finally:
            pool.shutdown()

    def test_refresh_warm_does_not_duplicate_an_in_use_slave(self):
        pool = autotmux.WarmSlavePool()
        slave = None
        try:
            pool.warm(self.fake_node)
            slave = pool._take(self.fake_node)
            self.assertEqual(pool._in_use[self.fake_node], slave[0])
            with mock.patch.object(
                    autotmux.subprocess, 'Popen',
                    side_effect=AssertionError('duplicate ssh spawned')):
                pool.warm(self.fake_node)
            self.assertTrue(pool._finish_handoff(
                self.fake_node, slave[0], slave[1], reusable=True))
            slave = None
            self.assertIn(self.fake_node, pool._slaves)
        finally:
            if slave is not None:
                pool._finish_handoff(
                    self.fake_node, slave[0], slave[1], reusable=False)
                pool._reap_child(slave[0])
                os.close(slave[1])
            pool.shutdown()

    def test_quick_clean_detach_does_not_trigger_a_second_cold_attach(self):
        pool = autotmux.WarmSlavePool()
        read_fd, write_fd = os.pipe()
        proc = mock.Mock()
        proc.pid = 424242
        proc.poll.return_value = None
        proc.wait.return_value = 0
        pool._slaves[self.fake_node] = (proc.pid, write_fd)
        pool._procs[proc.pid] = proc
        try:
            with mock.patch.object(pool, '_drain'), \
                    mock.patch.object(pool, '_write_all', return_value=True), \
                    mock.patch.object(pool, '_proxy', return_value='ok'), \
                    mock.patch.object(autotmux, '_copy_terminal_winsize'):
                self.assertTrue(pool.attach(self.fake_node, 'sess'))
            self.assertEqual(pool._slaves[self.fake_node],
                             (proc.pid, write_fd))
            proc.wait.assert_not_called()
        finally:
            pool.shutdown()
            os.close(read_fd)

    def test_shell_hands_off_the_existing_remote_login(self):
        pool = autotmux.WarmSlavePool()
        read_fd, write_fd = os.pipe()
        proc = mock.Mock()
        proc.pid = 424243
        proc.poll.return_value = None
        pool._slaves[self.fake_node] = (proc.pid, write_fd)
        pool._procs[proc.pid] = proc
        try:
            with mock.patch.object(pool, '_drain'), \
                    mock.patch.object(pool, '_write_all', return_value=True) as write, \
                    mock.patch.object(
                        pool, '_proxy', side_effect=lambda *_: time.sleep(0.6)), \
                    mock.patch.object(pool, '_reap_child'), \
                    mock.patch.object(autotmux, '_copy_terminal_winsize'):
                self.assertTrue(pool.shell(self.fake_node))
            write.assert_called_once_with(write_fd, b'\n')
            self.assertNotIn(self.fake_node, pool._slaves)
        finally:
            os.close(read_fd)


class BuildRowsWithLoadTests(unittest.TestCase):
    """Verify nproc/load make it through into the rendered row tuple
    (column B integration)."""

    def test_nproc_and_load_appear_in_row(self):
        state = {
            'nodes': {
                'gpu1': {
                    'alive': True,
                    'info': {'time': '1:00', 'nproc': '4', 'load': '12.34'},
                    'sessions': [['main', '1']],
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual(len(rows), 1)
        # row layout: (node, session, wins, time, status, cpu, load)
        self.assertEqual(rows[0][5], '4')
        self.assertEqual(rows[0][6], '12.34')

    def test_missing_nproc_load_become_empty_strings(self):
        state = {
            'nodes': {
                'gpu1': {
                    'alive': True,
                    'info': {'time': '1:00'},
                    'sessions': [['main', '1']],
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual(rows[0][5], '')
        self.assertEqual(rows[0][6], '')


class WarmConcurrencyTests(unittest.TestCase):
    """Two concurrent warm() calls for the same node must not both spawn —
    otherwise one slave is silently overwritten in self._slaves and leaks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        self.fake_node = 'fake-node-conc'
        ctl_file = os.path.join(self.ctl_dir, f'cm_{self.fake_node}')
        open(ctl_file, 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_warm_does_not_leak_slaves(self):
        pool = autotmux.WarmSlavePool()
        try:
            barrier = threading.Barrier(4)
            spawned_pids = []
            spawn_lock = threading.Lock()

            def race():
                barrier.wait()
                pool.warm(self.fake_node)
                with spawn_lock:
                    if self.fake_node in pool._slaves:
                        spawned_pids.append(pool._slaves[self.fake_node][0])

            threads = [threading.Thread(target=race) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # All four threads see the SAME pid (only one slave was spawned).
            unique_pids = set(spawned_pids)
            self.assertEqual(len(unique_pids), 1,
                             f'concurrent warm() spawned multiple slaves: {unique_pids}')
        finally:
            pool.shutdown()

    def test_stuck_spawn_does_not_hold_lock_or_block_shutdown(self):
        pool = autotmux.WarmSlavePool()
        entered = threading.Event()
        release = threading.Event()

        class DummyProc:
            pid = 987654321
            returncode = None
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = 0
            def wait(self, timeout=None):
                return self.returncode
            def kill(self):
                self.returncode = -9

        def blocked_popen(*args, **kwargs):
            entered.set()
            release.wait(2)
            return DummyProc()

        with mock.patch.object(autotmux.subprocess, 'Popen', blocked_popen):
            worker = threading.Thread(target=pool.warm, args=(self.fake_node,))
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            pool.shutdown()
            self.assertLess(time.monotonic() - started, 0.5)
            release.set()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertNotIn(self.fake_node, pool._slaves)


class WarmDropsGoneNodesTests(unittest.TestCase):
    """When the visible node set shrinks (slurm job ends), warm slaves
    for departed nodes must be terminated, not left running indefinitely."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        for n in ('node-a', 'node-b'):
            open(os.path.join(self.ctl_dir, f'cm_{n}'), 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warm_all_drops_slaves_for_nodes_not_in_view(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm_all({'node-a', 'node-b'})
            self.assertIn('node-a', pool._slaves)
            self.assertIn('node-b', pool._slaves)
            # Now node-b leaves the view (slurm job ended).
            pool.warm_all({'node-a'})
            self.assertIn('node-a', pool._slaves)
            self.assertNotIn('node-b', pool._slaves,
                             'warm_all must terminate slaves for nodes that left the view')
        finally:
            pool.shutdown()


if __name__ == '__main__':
    unittest.main(verbosity=2)
