"""Tests for autotmux.paths — XDG-aware runtime dir resolution."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import paths


class PickBaseTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get('XDG_RUNTIME_DIR')

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('XDG_RUNTIME_DIR', None)
        else:
            os.environ['XDG_RUNTIME_DIR'] = self._saved

    def test_uses_xdg_when_set_and_writable(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ['XDG_RUNTIME_DIR'] = td
            base = paths._pick_base()
            self.assertEqual(base, os.path.join(td, 'autotmux'))
            self.assertTrue(os.path.isdir(base))

    def test_falls_back_to_tmp_when_xdg_unset(self):
        os.environ.pop('XDG_RUNTIME_DIR', None)
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_falls_back_to_tmp_when_xdg_not_writable(self):
        os.environ['XDG_RUNTIME_DIR'] = '/proc/nonexistent-not-writable'
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_module_constants_live_under_base(self):
        self.assertTrue(paths.PID_FILE.startswith(paths.BASE))
        self.assertEqual(os.path.basename(paths.PID_FILE), 'daemon.pid')
        self.assertEqual(os.path.basename(paths.STATE_FILE), 'daemon.json')
        self.assertTrue(paths.CTL_DIR.startswith(paths.BASE))


if __name__ == '__main__':
    unittest.main()
