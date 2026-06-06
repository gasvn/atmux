"""Tests for autotmux.config — TOML daemon tunables with safe fallbacks."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import config


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved_path = config.CONFIG_PATH

    def tearDown(self):
        config.CONFIG_PATH = self._saved_path

    def _write(self, td, text):
        p = os.path.join(td, 'config.toml')
        with open(p, 'w') as f:
            f.write(text)
        config.CONFIG_PATH = p
        return p

    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            config.CONFIG_PATH = os.path.join(td, 'nope.toml')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_daemon_table_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = 60\n')
            cfg = config.load()
            self.assertEqual(cfg['squeue_interval'], 60)
            self.assertEqual(cfg['health_interval'],
                             config.DEFAULTS['health_interval'])

    def test_flat_layout_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'connect_timeout = 15\n')
            self.assertEqual(config.load()['connect_timeout'], 15)

    def test_unknown_key_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nbogus = 5\n')
            cfg = config.load()
            self.assertNotIn('bogus', cfg)
            self.assertEqual(cfg, config.DEFAULTS)

    def test_bool_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, '[daemon]\nsqueue_interval = true\n')
            self.assertEqual(config.load()['squeue_interval'],
                             config.DEFAULTS['squeue_interval'])

    def test_malformed_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, 'this is = not valid toml [[[')
            self.assertEqual(config.load(), config.DEFAULTS)

    def test_load_returns_a_copy(self):
        cfg = config.load()
        cfg['squeue_interval'] = 999
        self.assertNotEqual(config.DEFAULTS['squeue_interval'], 999)


if __name__ == '__main__':
    unittest.main()
