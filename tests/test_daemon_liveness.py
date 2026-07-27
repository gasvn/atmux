"""The frontend must detect a live daemon by its authoritative flock, not the
advisory pid file — which can vanish (systemd cleaning XDG_RUNTIME_DIR between
logins) while the daemon, reparented to init, keeps running. If it doesn't, the
frontend falsely declares the daemon dead and enters an unwinnable restart loop
(the singleton lock blocks the new start), which stalls the UI.
"""
import fcntl
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux


class DaemonLivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pid_file = os.path.join(self.tmp, 'daemon.pid')
        self.lock_file = self.pid_file + '.lock'
        self._saved = autotmux.PID_FILE
        autotmux.PID_FILE = self.pid_file

    def tearDown(self):
        autotmux.PID_FILE = self._saved

    def test_alive_via_lock_even_without_pid_file(self):
        # Simulate the daemon holding the singleton flock, with NO pid file.
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertFalse(os.path.exists(self.pid_file))
            self.assertTrue(autotmux._daemon_running(),
                            'a held singleton lock must count as a live daemon')
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_not_running_when_lock_free_and_no_pid(self):
        # No lock held, no pid file → genuinely not running.
        self.assertFalse(autotmux._daemon_running())

    def test_alive_via_pid_file_when_no_lock(self):
        # Back-compat: our own live pid in the pid file, no lock file present.
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))
        self.assertTrue(autotmux._daemon_running())

    def test_probe_does_not_leave_lock_held(self):
        # Probing must release the lock so a real daemon can still start after.
        self.assertFalse(autotmux._daemon_running())
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must succeed
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
