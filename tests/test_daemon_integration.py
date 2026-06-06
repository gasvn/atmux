"""Integration tests that actually start the daemon as a subprocess.

These talk to the real OS (write to /tmp, fork ssh masters for any nodes
in the user's squeue), so they assume the user has at least localhost
available. Each test stops + restarts the daemon to keep tests isolated.

Skipped automatically when `squeue` / `ssh` / `atd` aren't on PATH —
keeps unit-test CI green outside HPC clusters.
"""
import json
import os
import shutil
import signal
import subprocess
import time
import unittest


UID = os.getuid()
from autotmux import paths
PID_FILE = paths.PID_FILE
STATE_FILE = paths.STATE_FILE
SNAPSHOT_FILE = paths.SNAPSHOT_FILE
LOG_FILE = paths.LOG_FILE
CTL_DIR = paths.CTL_DIR

ATD = shutil.which('atd') or 'atd'

_HAVE_TOOLS = all(shutil.which(t) for t in ('atd', 'squeue', 'ssh'))


def requires_cluster_tools(cls):
    """Class decorator: skip these tests when atd/squeue/ssh aren't all
    available — typical for unit-only CI environments."""
    return unittest.skipUnless(
        _HAVE_TOOLS,
        'integration tests need atd, squeue, and ssh on PATH',
    )(cls)


def _atd(*args, timeout=20):
    return subprocess.run([ATD, *args], capture_output=True, text=True, timeout=timeout)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


@requires_cluster_tools
class DaemonLifecycleTests(unittest.TestCase):
    def setUp(self):
        _atd('stop')
        time.sleep(0.5)

    def tearDown(self):
        _atd('stop')

    def test_start_writes_pid_file(self):
        _atd('start')
        time.sleep(2)
        pid = _read_pid()
        self.assertIsNotNone(pid)
        self.assertTrue(_pid_alive(pid))

    def test_double_start_is_idempotent(self):
        r1 = _atd('start')
        time.sleep(2)
        pid1 = _read_pid()
        r2 = _atd('start')
        time.sleep(1)
        pid2 = _read_pid()
        self.assertEqual(pid1, pid2, "second start should detect running daemon")
        self.assertIn('Already running', r2.stdout + r2.stderr,
                      "second start should print 'Already running'")

    def test_stop_removes_pid_file(self):
        _atd('start')
        time.sleep(2)
        self.assertIsNotNone(_read_pid())
        _atd('stop')
        time.sleep(1)
        self.assertFalse(os.path.exists(PID_FILE))

    def test_state_file_has_required_keys(self):
        _atd('start')
        # First squeue cycle takes a moment.
        deadline = time.time() + 35
        state = None
        while time.time() < deadline:
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                if state.get('nodes'):
                    break
            except Exception:
                pass
            time.sleep(1)
        self.assertIsNotNone(state, "daemon never wrote a state file")
        for key in ['pid', 'user', 'updated', 'squeue_long', 'squeue_pending',
                    'squeue_updated', 'nodes']:
            self.assertIn(key, state, f"state file missing key: {key}")

    def test_state_file_includes_localhost(self):
        _atd('start')
        deadline = time.time() + 35
        nodes = {}
        while time.time() < deadline:
            try:
                with open(STATE_FILE) as f:
                    nodes = json.load(f).get('nodes', {})
                if 'localhost' in nodes:
                    break
            except Exception:
                pass
            time.sleep(1)
        self.assertIn('localhost', nodes)
        self.assertTrue(nodes['localhost']['alive'])

    def test_atomic_write_leaves_no_tmp_files(self):
        _atd('start')
        time.sleep(35)  # several state writes
        leftovers = []
        for parent in ['/tmp']:
            for fn in os.listdir(parent):
                if fn.startswith(f'autotmux_daemon_{UID}.json.tmp') or \
                   fn.startswith(f'autotmux_snapshots_{UID}.json.tmp'):
                    leftovers.append(os.path.join(parent, fn))
        self.assertEqual(leftovers, [],
                         f"atomic write should not leave .tmp files: {leftovers}")

    def test_signal_handler_logs_graceful_shutdown(self):
        _atd('start')
        time.sleep(3)
        pid = _read_pid()
        os.kill(pid, signal.SIGTERM)
        # Give the handler time to log + flush
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        self.assertFalse(_pid_alive(pid), "daemon should exit on SIGTERM")
        with open(LOG_FILE) as f:
            log_text = f.read()
        self.assertIn('shutting down', log_text.lower(),
                      "graceful shutdown should be logged")


@requires_cluster_tools
class SshLeakTests(unittest.TestCase):
    """Daemon shouldn't leak ssh master processes across restarts."""

    def setUp(self):
        _atd('stop')
        # Sweep prior zombies
        subprocess.run(['pkill', '-f', f'ControlPath={CTL_DIR}'],
                       capture_output=True)
        time.sleep(1)

    def tearDown(self):
        pass  # leave daemon running for final test

    def _count_daemon_ssh(self):
        r = subprocess.run(
            ['pgrep', '-af', f'ControlPath={CTL_DIR}'],
            capture_output=True, text=True,
        )
        return len([line for line in r.stdout.splitlines() if line.strip()])

    def test_no_ssh_leak_across_restart_cycles(self):
        # Start, stop, start, stop, start — count ssh after each cycle.
        # The test that matters is whether the count GROWS across cycles
        # (indicating leaks). The first cycle starts from a wiped state
        # (setUp pkill) and may have masters still mid-bind at the count
        # moment, so we ignore it and compare the later steady-state ones.
        counts = []
        for _ in range(3):
            _atd('start')
            time.sleep(8)  # generous time so masters can bind
            counts.append(self._count_daemon_ssh())
            _atd('stop')
            time.sleep(2)
        # Adopt-healthy means masters from cycle 1 carry into cycles 2/3
        # via ControlPersist. So cycles 2 and 3 should be similar.
        steady = counts[1:]
        self.assertLess(max(steady) - min(steady), 4,
                        f"ssh count not converging across restarts: {counts}")

    def test_no_ssh_leak_during_steady_state(self):
        _atd('start')
        time.sleep(15)  # let one full health cycle happen
        c1 = self._count_daemon_ssh()
        time.sleep(45)  # wait through another health cycle
        c2 = self._count_daemon_ssh()
        # Bounded growth — even in unstable cluster conditions, the
        # frontend leak fix means we shouldn't see counts climb.
        self.assertLessEqual(c2, c1 + 2,
                             f"ssh count climbed: {c1} -> {c2}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
