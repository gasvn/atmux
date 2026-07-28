"""Mouse tracking must default OFF over SSH: its per-move/scroll report bytes
compete with keystrokes and make arrow keys feel dead on a remote/loaded
terminal (atmux's primary use is over SSH into a cluster). Locally it stays on
for click-to-attach. --mouse / --no-mouse force either way."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli


def _args(no_mouse=False, mouse=False):
    return type('A', (), {'no_mouse': no_mouse, 'mouse': mouse})()


class WantMouseTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ('SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT')}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_ssh(self, on):
        for k in ('SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT'):
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

    def test_no_mouse_wins_locally(self):
        self._set_ssh(False)
        self.assertFalse(cli._want_mouse(_args(no_mouse=True)))

    def test_no_mouse_beats_mouse(self):
        # --no-mouse takes precedence if both somehow given.
        self._set_ssh(False)
        self.assertFalse(cli._want_mouse(_args(no_mouse=True, mouse=True)))


if __name__ == '__main__':
    unittest.main()
