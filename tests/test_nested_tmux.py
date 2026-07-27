"""Tests for the nested-tmux transparent-outer handling.

When atmux runs inside tmux and attaches another tmux, the outer server must be
made transparent (prefix None / key-table off / status off, F12 to toggle) so
the inner session receives the prefix. These tests drive the real helpers
against an ISOLATED tmux server on a private socket — never the live one.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux


def _have_tmux() -> bool:
    return shutil.which('tmux') is not None


@unittest.skipUnless(_have_tmux(), 'tmux not installed')
class NestedTmuxTransparentTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmpdir, 'outer.sock')
        subprocess.run(['tmux', '-S', self.sock, 'new-session', '-d', '-s', 'outer'],
                       check=True)
        # Point the helpers at our isolated server via $TMUX (they parse the
        # socket from its first field).
        self._prev_tmux = os.environ.get('TMUX')
        os.environ['TMUX'] = f'{self.sock},0,0'

    def tearDown(self):
        subprocess.run(['tmux', '-S', self.sock, 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self._prev_tmux is None:
            os.environ.pop('TMUX', None)
        else:
            os.environ['TMUX'] = self._prev_tmux
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _opt(self, name: str) -> str:
        r = subprocess.run(['tmux', '-S', self.sock, 'show-options', '-v', name],
                           capture_output=True, text=True)
        return r.stdout.strip()

    def _root_f12(self) -> str:
        r = subprocess.run(['tmux', '-S', self.sock, 'list-keys', '-T', 'root'],
                           capture_output=True, text=True)
        return '\n'.join(l for l in r.stdout.splitlines() if 'F12' in l)

    def _off_f12(self) -> str:
        r = subprocess.run(['tmux', '-S', self.sock, 'list-keys', '-T', 'off'],
                           capture_output=True, text=True)
        return '\n'.join(l for l in r.stdout.splitlines() if 'F12' in l)

    def test_step_aside_makes_outer_transparent(self):
        autotmux._tmux_step_aside()
        self.assertEqual(self._opt('prefix'), 'None')
        self.assertEqual(self._opt('key-table'), 'off')
        self.assertEqual(self._opt('status'), 'off')

    def test_step_aside_binds_full_f12_command_list(self):
        # The whole toggle sequence must land on F12 — not just the first
        # command (the tmux-2.7 escaped-semicolon gotcha this fix hinges on).
        autotmux._tmux_step_aside()
        root = self._root_f12()
        self.assertIn('prefix None', root)
        self.assertIn('key-table off', root)
        self.assertIn('status off', root)
        # The `off` table must let the user (or a crashed atmux) recover.
        off = self._off_f12()
        self.assertIn('prefix', off)
        self.assertIn('key-table', off)

    def test_restore_reverts_everything(self):
        autotmux._tmux_step_aside()
        autotmux._tmux_restore()
        # Transparent values are gone.
        self.assertNotEqual(self._opt('prefix'), 'None')
        self.assertNotEqual(self._opt('key-table'), 'off')
        self.assertNotEqual(self._opt('status'), 'off')
        # F12 toggle bindings removed from both tables.
        self.assertEqual(self._root_f12(), '')
        self.assertEqual(self._off_f12(), '')


class WillNestLogicTests(unittest.TestCase):
    def setUp(self):
        self._prev_tmux = os.environ.get('TMUX')

    def tearDown(self):
        if self._prev_tmux is None:
            os.environ.pop('TMUX', None)
        else:
            os.environ['TMUX'] = self._prev_tmux

    def test_inside_tmux_real_session_nests(self):
        os.environ['TMUX'] = '/tmp/x,0,0'
        self.assertTrue(autotmux._will_nest_tmux('train'))
        self.assertTrue(autotmux._will_nest_tmux('main'))

    def test_inside_tmux_shell_or_offline_does_not_nest(self):
        os.environ['TMUX'] = '/tmp/x,0,0'
        self.assertFalse(autotmux._will_nest_tmux('<Start Shell>'))
        self.assertFalse(autotmux._will_nest_tmux('<offline>'))

    def test_outside_tmux_never_nests(self):
        os.environ.pop('TMUX', None)
        self.assertFalse(autotmux._will_nest_tmux('train'))
        self.assertFalse(autotmux._will_nest_tmux('<Start Shell>'))

    def test_tmux_helper_is_noop_without_tmux(self):
        os.environ.pop('TMUX', None)
        # Must not raise and must not shell out to a live server.
        autotmux._tmux('set', 'prefix', 'None')
        autotmux._tmux_step_aside()
        autotmux._tmux_restore()


if __name__ == '__main__':
    unittest.main()
