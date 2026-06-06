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

    def test_dead_pid_in_legacy_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = os.path.join(td, 'legacy.pid')
            with open(legacy, 'w') as f:
                f.write('999999999')  # a pid extremely unlikely to exist
            d.LEGACY_PID_FILE = legacy
            d.PID_FILE = os.path.join(td, 'new.pid')
            d._stop_legacy_daemon()  # dead pid → noop, no exception


if __name__ == '__main__':
    unittest.main()
