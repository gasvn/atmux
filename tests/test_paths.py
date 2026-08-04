"""Tests for autotmux.paths — XDG-aware runtime dir resolution."""
import os
import sys
import tempfile
import unittest
from unittest import mock

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
        # Keep the directory short: the default temp location is a handful of
        # characters on Linux but ~55 under /var/folders on macOS, which is
        # long enough that _pick_base() correctly rejects it for socket length
        # and falls back to /tmp -- testing the wrong branch.
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            os.environ['XDG_RUNTIME_DIR'] = td
            base = paths._pick_base()
            self.assertEqual(base, os.path.join(td, 'autotmux'))
            self.assertTrue(os.path.isdir(base))

    def test_falls_back_when_xdg_is_too_deep_for_a_socket(self):
        """A usable but deeply nested XDG dir must not strand every SSH master
        behind a ControlPath that cannot fit in a sockaddr_un."""
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            deep = os.path.join(td, 'd' * 60)
            os.makedirs(deep, 0o700)
            os.environ['XDG_RUNTIME_DIR'] = deep
            self.assertTrue(paths._usable_xdg_runtime_dir(deep))
            self.assertEqual(
                paths._pick_base(), f'/tmp/autotmux_{os.getuid()}')

    def test_falls_back_to_tmp_when_xdg_unset(self):
        os.environ.pop('XDG_RUNTIME_DIR', None)
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_falls_back_to_tmp_when_xdg_not_writable(self):
        os.environ['XDG_RUNTIME_DIR'] = '/proc/nonexistent-not-writable'
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_relative_xdg_runtime_dir_cannot_split_daemon_by_cwd(self):
        os.environ['XDG_RUNTIME_DIR'] = '.'
        base = paths._pick_base()
        self.assertEqual(base, f'/tmp/autotmux_{os.getuid()}')

    def test_insecure_or_symlinked_xdg_runtime_dir_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            loose = os.path.join(td, 'loose')
            real = os.path.join(td, 'real')
            link = os.path.join(td, 'link')
            os.mkdir(loose, 0o755)
            os.mkdir(real, 0o700)
            os.symlink(real, link)
            for candidate in (loose, link):
                os.environ['XDG_RUNTIME_DIR'] = candidate
                self.assertEqual(
                    paths._pick_base(), f'/tmp/autotmux_{os.getuid()}')

    def test_guard_override_must_be_absolute(self):
        with mock.patch.dict(os.environ,
                             {'AUTOTMUX_GUARD_FILE': 'relative.guard'}):
            with self.assertRaisesRegex(RuntimeError, 'absolute path'):
                paths._pick_guard_file()
        with mock.patch.dict(
                os.environ,
                {'AUTOTMUX_GUARD_FILE': '/tmp/a/../isolated.guard'}):
            self.assertEqual(paths._pick_guard_file(),
                             '/tmp/isolated.guard')

    def test_module_constants_live_under_base(self):
        self.assertTrue(paths.PID_FILE.startswith(paths.BASE))
        self.assertEqual(os.path.basename(paths.PID_FILE), 'daemon.pid')
        self.assertEqual(os.path.basename(paths.STATE_FILE), 'daemon.json')
        self.assertEqual(os.path.basename(paths.PREVIEW_SOCKET), 'preview.sock')
        self.assertTrue(paths.CTL_DIR.startswith(paths.BASE))
        self.assertTrue(paths.WARM_DIR.startswith(paths.BASE))
        self.assertEqual(os.path.basename(paths.WARM_DIR), 'warm')
        self.assertTrue(paths.INTERACTIVE_CTL_DIR.startswith(paths.BASE))
        self.assertEqual(
            os.path.basename(paths.INTERACTIVE_CTL_DIR), 'interactive-ctl')
        self.assertEqual(
            paths.GUARD_FILE,
            os.environ.get('AUTOTMUX_GUARD_FILE',
                           f'/tmp/autotmux_daemon_{os.getuid()}.guard'))

    def test_long_control_path_is_deterministically_hashed(self):
        ctl = os.path.join('/tmp', 'x' * 40, 'ctl')
        first = paths.control_path('node-' + 'y' * 100, ctl)
        second = paths.control_path('node-' + 'y' * 100, ctl)
        self.assertEqual(first, second)
        self.assertLess(len(os.fsencode(first)), paths._CONTROL_PATH_LIMIT)
        self.assertRegex(os.path.basename(first), r'^cm_h-[0-9a-f]{32}$')

    def test_replaced_runtime_base_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, 'runtime')
            ctl = os.path.join(base, 'ctl')
            os.mkdir(base, 0o700)
            st = os.stat(base)
            with mock.patch.object(paths, 'BASE', base), \
                 mock.patch.object(paths, 'CTL_DIR', ctl), \
                 mock.patch.object(paths, 'WARM_DIR',
                                   os.path.join(base, 'warm')), \
                 mock.patch.object(paths, 'INTERACTIVE_CTL_DIR',
                                   os.path.join(base, 'interactive-ctl')), \
                 mock.patch.object(paths, 'GATEWAY_CTL_DIR',
                                   os.path.join(base, 'gateway-ctl')), \
                 mock.patch.object(paths, '_BASE_ID', (st.st_dev, st.st_ino)):
                paths.ensure_runtime_dirs()
                os.rename(base, base + '.old')
                os.mkdir(base, 0o700)
                with self.assertRaises(RuntimeError):
                    paths.ensure_runtime_dirs()


if __name__ == '__main__':
    unittest.main()
