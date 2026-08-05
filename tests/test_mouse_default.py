"""Mouse tracking must default OFF over SSH: its per-move/scroll report bytes
compete with keystrokes and make arrow keys feel dead on a remote/loaded
terminal (atmux's primary use is over SSH into a cluster). Locally it stays on
for click-to-attach. --mouse / --no-mouse force either way."""
import os
import sys
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli


def _args(no_mouse=False, mouse=False):
    return type('A', (), {'no_mouse': no_mouse, 'mouse': mouse})()


class WantMouseTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ('SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT',
                                 'MOSH_CONNECTION', 'MOSH_IP')}
        # These assert the *defaults*, so they must not read whatever the
        # developer happens to have set in their own config.
        self._temp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            cli.config, 'CONFIG_PATH',
            os.path.join(self._temp.name, 'absent.toml'))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_ssh(self, on):
        for k in ('SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT',
                  'MOSH_CONNECTION', 'MOSH_IP'):
            os.environ.pop(k, None)
        if on:
            os.environ['SSH_CONNECTION'] = '10.0.0.1 22 10.0.0.2 22'

    def test_off_over_ssh_by_default(self):
        self._set_ssh(True)
        self.assertTrue(cli._is_remote_session())
        self.assertFalse(cli._want_mouse(_args()))

    def test_on_locally_by_default(self):
        self._set_ssh(False)
        self.assertFalse(cli._is_remote_session())
        self.assertTrue(cli._want_mouse(_args()))

    def test_force_mouse_over_ssh(self):
        self._set_ssh(True)
        self.assertTrue(cli._want_mouse(_args(mouse=True)))

    def test_ssh_client_only_and_mosh_are_still_remote(self):
        self._set_ssh(False)
        os.environ['SSH_CLIENT'] = '10.0.0.1 12345 22'
        self.assertTrue(cli._is_remote_session())
        self.assertFalse(cli._want_mouse(_args()))
        os.environ.pop('SSH_CLIENT')
        os.environ['MOSH_CONNECTION'] = '10.0.0.1 60000 10.0.0.2 60001'
        self.assertTrue(cli._is_remote_session())
        self.assertFalse(cli._want_mouse(_args()))

    def test_no_mouse_wins_locally(self):
        self._set_ssh(False)
        self.assertFalse(cli._want_mouse(_args(no_mouse=True)))

    def test_no_mouse_beats_mouse(self):
        # --no-mouse takes precedence if both somehow given.
        self._set_ssh(False)
        self.assertFalse(cli._want_mouse(_args(no_mouse=True, mouse=True)))

    def test_cli_rejects_conflicting_mouse_flags(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli._build_argparser().parse_args(['--mouse', '--no-mouse'])
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
