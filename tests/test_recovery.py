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


class MaybeRecoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_running = cli._daemon_running
        self._saved_launch = cli._launch_daemon
        cli._daemon_running = lambda: False  # isolate from real system state
        cli._launch_daemon = lambda: None    # never spawn a real daemon in tests

    def tearDown(self):
        cli._daemon_running = self._saved_running
        cli._launch_daemon = self._saved_launch

    async def test_no_recovery_when_daemon_alive(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._crash_looping = True
            cli._daemon_running = lambda: True
            app._maybe_recover_daemon()
            self.assertEqual(calls, [])
            self.assertFalse(app._crash_looping)

    async def test_restart_dispatched_when_dead(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._crash_looping = False
            cli._daemon_running = lambda: False
            app._maybe_recover_daemon()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(app._restart_attempts), 1)
            self.assertFalse(app._crash_looping)

    async def test_stops_after_loop_guard_and_sets_banner(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._crash_looping = False
            cli._daemon_running = lambda: False
            for _ in range(5):
                app._maybe_recover_daemon()
            self.assertEqual(len(calls), 3)       # capped at limit=3
            self.assertTrue(app._crash_looping)


if __name__ == '__main__':
    unittest.main()
