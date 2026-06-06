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


if __name__ == '__main__':
    unittest.main()
