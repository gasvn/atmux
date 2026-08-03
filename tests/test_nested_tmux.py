"""Tests for the nested-tmux transparent-outer handling.

When atmux runs inside tmux and attaches another tmux, the outer server must be
made transparent (prefix None / private key-table / status off, F12 to recover)
so the inner session receives the prefix. These tests drive the real helpers
against an ISOLATED tmux server on a private socket — never the live one.
"""
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux


def _have_tmux() -> bool:
    return shutil.which('tmux') is not None


@unittest.skipUnless(_have_tmux(), 'tmux not installed')
class NestedTmuxTransparentTests(unittest.TestCase):
    def setUp(self):
        autotmux._outer_tmux_state = None
        self.tmpdir = tempfile.mkdtemp()
        self._prev_guard = autotmux.GUARD_FILE
        self.guard_file = os.path.join(self.tmpdir, 'daemon.guard')
        autotmux.GUARD_FILE = self.guard_file
        self.sock = os.path.join(self.tmpdir, 'outer.sock')
        try:
            subprocess.run(
                ['tmux', '-S', self.sock, 'new-session', '-d', '-s', 'outer'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            autotmux.GUARD_FILE = self._prev_guard
            self.skipTest(f'tmux server unavailable in this sandbox: {exc.stderr.strip()}')
        # Point the helpers at our isolated server via $TMUX (they parse the
        # socket from its first field).
        self._prev_tmux = os.environ.get('TMUX')
        os.environ['TMUX'] = f'{self.sock},0,0'
        self.context = autotmux._outer_tmux_context()
        self.assertIsNotNone(self.context)

    def tearDown(self):
        autotmux._outer_tmux_state = None
        subprocess.run(['tmux', '-S', self.sock, 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self._prev_tmux is None:
            os.environ.pop('TMUX', None)
        else:
            os.environ['TMUX'] = self._prev_tmux
        autotmux.GUARD_FILE = self._prev_guard
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _opt(self, name: str) -> str:
        r = subprocess.run(['tmux', '-S', self.sock, 'show-options', '-v', name],
                           capture_output=True, text=True)
        value = r.stdout.strip()
        if value:
            return value
        # tmux 2.7 prints nothing for an inherited session option unless -g is
        # used. The production code preserves that distinction with set -u.
        r = subprocess.run(
            ['tmux', '-S', self.sock, 'show-options', '-g', '-v', name],
            capture_output=True, text=True)
        return r.stdout.strip()

    def _session_opt(self, name: str) -> str:
        return subprocess.run(
            ['tmux', '-S', self.sock, 'show-options', '-v', name],
            capture_output=True, text=True).stdout.strip()

    def _server_opt(self, name: str) -> str:
        return subprocess.run(
            ['tmux', '-S', self.sock, 'show-options', '-s', '-v', name],
            capture_output=True, text=True).stdout.strip()

    def _global_opt(self, name: str) -> str:
        return subprocess.run(
            ['tmux', '-S', self.sock, 'show-options', '-g', '-v', name],
            capture_output=True, text=True).stdout.strip()

    def _root_f12(self) -> str:
        r = subprocess.run(['tmux', '-S', self.sock, 'list-keys', '-T', 'root'],
                           capture_output=True, text=True)
        return '\n'.join(line for line in r.stdout.splitlines() if 'F12' in line)

    def _recovery_f12(self) -> str:
        r = subprocess.run(
            ['tmux', '-S', self.sock, 'list-keys', '-T', self.context['table']],
                           capture_output=True, text=True)
        return '\n'.join(line for line in r.stdout.splitlines() if 'F12' in line)

    def _spawn_lease_owner(self, tmux_value=None):
        code = (
            "import sys; from autotmux import cli; "
            "print('READY', cli._tmux_step_aside(), flush=True); "
            "sys.stdin.readline(); "
            "print('RESTORED', cli._tmux_restore(), flush=True)"
        )
        env = os.environ.copy()
        env['AUTOTMUX_GUARD_FILE'] = self.guard_file
        if tmux_value is not None:
            env['TMUX'] = tmux_value
        source = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
        env['PYTHONPATH'] = source + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.Popen(
            [sys.executable, '-c', code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def _wait_line(self, proc, timeout=8):
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            stderr = proc.stderr.read() if proc.poll() is not None else ''
            self.fail(f'lease helper timed out (rc={proc.poll()}): {stderr}')
        return proc.stdout.readline().strip()

    def _release_owner(self, proc):
        proc.stdin.write('\n')
        proc.stdin.flush()
        line = self._wait_line(proc)
        proc.wait(timeout=8)
        stderr = proc.stderr.read()
        self._close_owner_streams(proc)
        self.assertEqual(line, 'RESTORED True', stderr)

    @staticmethod
    def _close_owner_streams(proc):
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def test_step_aside_makes_outer_transparent(self):
        self.assertTrue(autotmux._tmux_step_aside())
        self.assertEqual(self._opt('prefix'), 'None')
        self.assertEqual(self._opt('prefix2'), 'None')
        self.assertEqual(self._opt('key-table'), self.context['table'])
        self.assertEqual(self._opt('status'), 'off')
        self.assertEqual(
            self._server_opt('escape-time'),
            str(autotmux._OUTER_TMUX_NESTED_ESCAPE_TIME),
        )

    def test_escape_time_is_leased_and_exactly_restored(self):
        subprocess.run(
            ['tmux', '-S', self.sock, 'set-option', '-s',
             'escape-time', '317'], check=True)
        self.assertTrue(autotmux._tmux_step_aside())
        self.assertEqual(self._server_opt('escape-time'), '10')
        snapshot = autotmux._outer_tmux_latency_snapshot(self.context)
        lease = autotmux._decode_outer_tmux_latency_lease(
            snapshot[1], self.context)
        self.assertEqual(lease['original'], 317)
        self.assertEqual(lease['target'], 10)
        self.assertEqual(len(lease['owners']), 1)

        self.assertTrue(autotmux._tmux_restore())
        self.assertEqual(self._server_opt('escape-time'), '317')
        self.assertEqual(self._global_opt(self.context['latency_state_key']), '')

    def test_escape_time_already_below_target_is_never_increased(self):
        subprocess.run(
            ['tmux', '-S', self.sock, 'set-option', '-s',
             'escape-time', '0'], check=True)
        self.assertTrue(autotmux._tmux_step_aside())
        self.assertEqual(self._server_opt('escape-time'), '0')
        self.assertTrue(autotmux._tmux_restore())
        self.assertEqual(self._server_opt('escape-time'), '0')

    def test_step_aside_binds_full_private_f12_recovery(self):
        # The whole toggle sequence must land on F12 — not just the first
        # command (the tmux-2.7 escaped-semicolon gotcha this fix hinges on).
        root_before = self._root_f12()
        autotmux._tmux_step_aside()
        self.assertEqual(self._root_f12(), root_before)
        recovery = self._recovery_f12()
        self.assertIn('prefix', recovery)
        self.assertIn('prefix2', recovery)
        self.assertIn('key-table', recovery)
        self.assertIn('status', recovery)
        self.assertIn(self.context['state_key'], recovery)
        self.assertIn('-u', recovery)

    def test_f12_actually_recovers_attached_outer_client(self):
        subprocess.run(
            ['tmux', '-S', self.sock, 'respawn-pane', '-k', '-t', 'outer',
             'cat -v'], check=True)
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        env['TERM'] = 'xterm-256color'
        client = subprocess.Popen(
            ['tmux', '-S', self.sock, 'attach-session', '-t', 'outer'],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, env=env)
        os.close(slave_fd)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                listed = subprocess.run(
                    ['tmux', '-S', self.sock, 'list-clients'],
                    capture_output=True, text=True)
                if listed.returncode == 0 and listed.stdout.strip():
                    break
                time.sleep(0.05)
            else:
                self.fail('isolated outer tmux client did not attach')

            self.assertTrue(autotmux._tmux_step_aside())
            # Model an inner tmux prefix sequence. With the private transparent
            # table, both C-b and its following command key reach the pane.
            os.write(master_fd, b'\x02X')
            deadline = time.monotonic() + 5
            captured = ''
            while time.monotonic() < deadline:
                captured = subprocess.run(
                    ['tmux', '-S', self.sock, 'capture-pane', '-p', '-t', 'outer'],
                    capture_output=True, text=True).stdout
                if '^BX' in captured:
                    break
                time.sleep(0.05)
            self.assertIn('^BX', captured)

            os.write(master_fd, b'\x1b[24~')  # xterm F12
            deadline = time.monotonic() + 5
            while (time.monotonic() < deadline
                   and self._session_opt('key-table') == self.context['table']):
                time.sleep(0.05)
            self.assertEqual(self._session_opt('key-table'), '')
            self.assertEqual(self._session_opt('prefix'), '')
            self.assertEqual(self._session_opt('prefix2'), '')
            self.assertEqual(self._server_opt('escape-time'), '500')
            self.assertEqual(
                self._global_opt(self.context['latency_state_key']), '')
            self.assertTrue(autotmux._tmux_restore())

            os.write(master_fd, b'\x02d')  # restored default C-b, detach
            client.wait(timeout=5)
        finally:
            if client.poll() is None:
                client.kill()
                client.wait(timeout=5)
            os.close(master_fd)

    def test_client_handoff_executes_helper_and_reattaches(self):
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        env['TERM'] = 'xterm-256color'
        client = subprocess.Popen(
            ['tmux', '-S', self.sock, 'attach-session', '-t', 'outer'],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, env=env)
        os.close(slave_fd)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                listed = subprocess.run(
                    ['tmux', '-S', self.sock, 'list-clients'],
                    capture_output=True, text=True)
                if listed.returncode == 0 and listed.stdout.strip():
                    break
                time.sleep(0.05)
            else:
                self.fail('isolated outer tmux client did not attach')

            with mock.patch.object(autotmux, '_GATEWAY_POOL', None):
                self.assertTrue(autotmux._handoff_outer_tmux_client(
                    ['--version']))

            output = bytearray()
            deadline = time.monotonic() + 8
            reattached = False
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if ready:
                    try:
                        output.extend(os.read(master_fd, 65536))
                    except OSError:
                        break
                listed = subprocess.run(
                    ['tmux', '-S', self.sock, 'list-clients'],
                    capture_output=True, text=True)
                reattached = bool(
                    listed.returncode == 0 and listed.stdout.strip())
                if b'AutoTmux ' in output and reattached:
                    break
            self.assertIn(b'AutoTmux ', output)
            self.assertTrue(reattached)
            self.assertIsNone(client.poll())
        finally:
            if client.poll() is None:
                client.terminate()
                try:
                    client.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait(timeout=3)
            os.close(master_fd)

    def test_restore_reverts_everything(self):
        autotmux._tmux_step_aside()
        autotmux._tmux_restore()
        # Transparent values are gone.
        self.assertNotEqual(self._opt('prefix'), 'None')
        # tmux's global prefix2 default is itself None; restoration means our
        # session-local override disappeared, not that the effective value
        # became a different key.
        self.assertEqual(self._session_opt('prefix2'), '')
        self.assertNotEqual(self._opt('key-table'), self.context['table'])
        self.assertNotEqual(self._opt('status'), 'off')
        # User root binding remains untouched; only our private binding is gone.
        self.assertEqual(self._root_f12(), '')
        self.assertEqual(self._recovery_f12(), '')

    def test_restore_preserves_custom_options_and_root_f12_binding(self):
        subprocess.run(
            ['tmux', '-S', self.sock, 'set', 'prefix', 'C-a'], check=True)
        subprocess.run(
            ['tmux', '-S', self.sock, 'set', 'prefix2', 'C-z'], check=True)
        subprocess.run(
            ['tmux', '-S', self.sock, 'bind-key', '-T', 'root', 'F12',
             'display-message', 'user-f12'], check=True)
        root_before = self._root_f12()
        autotmux._tmux_step_aside()
        autotmux._tmux_restore()
        self.assertEqual(self._opt('prefix'), 'C-a')
        self.assertEqual(self._opt('prefix2'), 'C-z')
        self.assertEqual(self._opt('key-table'), 'root')
        self.assertEqual(self._opt('status'), 'on')
        self.assertEqual(self._root_f12(), root_before)
        self.assertEqual(self._recovery_f12(), '')

    def test_concurrent_processes_restore_only_after_last_detach(self):
        """One window detaching must not kill shortcuts in another window."""
        first = second = None
        try:
            first = self._spawn_lease_owner()
            self.assertEqual(self._wait_line(first), 'READY True')
            second = self._spawn_lease_owner()
            self.assertEqual(self._wait_line(second), 'READY True')

            snapshot = autotmux._outer_tmux_snapshot(self.context)
            lease = autotmux._decode_outer_tmux_lease(snapshot[1], self.context)
            self.assertEqual(len(lease['owners']), 2)
            latency_snapshot = autotmux._outer_tmux_latency_snapshot(self.context)
            latency_lease = autotmux._decode_outer_tmux_latency_lease(
                latency_snapshot[1], self.context)
            self.assertEqual(len(latency_lease['owners']), 2)

            self._release_owner(first)
            first = None
            self.assertEqual(self._opt('prefix'), 'None')
            self.assertEqual(self._opt('prefix2'), 'None')
            self.assertEqual(self._opt('key-table'), self.context['table'])
            snapshot = autotmux._outer_tmux_snapshot(self.context)
            lease = autotmux._decode_outer_tmux_lease(snapshot[1], self.context)
            self.assertEqual(len(lease['owners']), 1)
            self.assertEqual(self._server_opt('escape-time'), '10')
            latency_snapshot = autotmux._outer_tmux_latency_snapshot(self.context)
            latency_lease = autotmux._decode_outer_tmux_latency_lease(
                latency_snapshot[1], self.context)
            self.assertEqual(len(latency_lease['owners']), 1)

            self._release_owner(second)
            second = None
            self.assertNotEqual(self._opt('prefix'), 'None')
            self.assertEqual(self._session_opt('prefix2'), '')
            self.assertEqual(self._opt('key-table'), 'root')
            self.assertEqual(self._server_opt('escape-time'), '500')
        finally:
            for proc in (first, second):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc is not None:
                    self._close_owner_streams(proc)

    def test_stale_crashed_owner_is_reaped_by_next_attach(self):
        crashed = self._spawn_lease_owner()
        self.assertEqual(self._wait_line(crashed), 'READY True')
        crashed.kill()
        crashed.wait(timeout=5)
        self._close_owner_streams(crashed)
        self.assertEqual(self._opt('prefix'), 'None')

        # A future attach adopts the still-valid original settings, drops the
        # dead PID lease, and restores cleanly when it exits.
        self.assertTrue(autotmux._tmux_step_aside())
        self.assertTrue(autotmux._tmux_restore())
        self.assertNotEqual(self._opt('prefix'), 'None')
        self.assertEqual(self._opt('key-table'), 'root')

    def test_escape_time_lease_is_shared_across_outer_sessions(self):
        """A detach in session $0 must not slow a still-nested session $1."""
        subprocess.run(
            ['tmux', '-S', self.sock, 'new-session', '-d', '-s', 'other'],
            check=True)
        other_id = subprocess.run(
            ['tmux', '-S', self.sock, 'display-message', '-p',
             '-t', 'other', '#{session_id}'],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertRegex(other_id, r'^\$[0-9]+$')
        other_tmux = f'{self.sock},0,{other_id[1:]}'
        first = second = None
        try:
            first = self._spawn_lease_owner()
            self.assertEqual(self._wait_line(first), 'READY True')
            second = self._spawn_lease_owner(other_tmux)
            self.assertEqual(self._wait_line(second), 'READY True')
            self.assertEqual(self._server_opt('escape-time'), '10')

            self._release_owner(first)
            first = None
            self.assertEqual(self._server_opt('escape-time'), '10')
            other_table = subprocess.run(
                ['tmux', '-S', self.sock, 'show-options', '-t', other_id,
                 '-v', 'key-table'], capture_output=True, text=True,
            ).stdout.strip()
            self.assertTrue(other_table.startswith('autotmux-off-'))

            self._release_owner(second)
            second = None
            self.assertEqual(self._server_opt('escape-time'), '500')
        finally:
            for proc in (first, second):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc is not None:
                    self._close_owner_streams(proc)


class WillNestLogicTests(unittest.TestCase):
    def setUp(self):
        self._prev_tmux = os.environ.get('TMUX')
        autotmux._outer_tmux_state = None

    def tearDown(self):
        autotmux._outer_tmux_state = None
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
        self.assertFalse(autotmux._will_nest_tmux(
            autotmux._START_SHELL_SESSION))
        self.assertFalse(autotmux._will_nest_tmux(
            autotmux._OFFLINE_SESSION))

    def test_real_session_matching_placeholder_label_still_nests(self):
        os.environ['TMUX'] = '/tmp/x,0,0'
        self.assertTrue(autotmux._will_nest_tmux('<Start Shell>'))
        self.assertTrue(autotmux._will_nest_tmux('<offline>'))

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

    def test_tmux_context_allows_commas_in_socket_path(self):
        os.environ['TMUX'] = '/tmp/outer,socket,123,7'
        context = autotmux._outer_tmux_context()
        self.assertEqual(context['socket'], '/tmp/outer,socket')
        self.assertEqual(context['server_pid'], '123')
        self.assertEqual(context['session'], '$7')

    def test_step_aside_is_one_bounded_tmux_transaction(self):
        options = {name: None for name in autotmux._OUTER_TMUX_OPTIONS}
        with mock.patch.object(
                autotmux, '_outer_tmux_snapshot', return_value=(options, None)), \
             mock.patch.object(
                 autotmux, '_acquire_outer_tmux_latency', return_value=None), \
             mock.patch.object(autotmux, '_acquire_outer_tmux_lock', return_value=9), \
             mock.patch.object(autotmux, '_release_outer_tmux_lock'), \
             mock.patch.object(autotmux, '_tmux', return_value=True) as run:
            self.assertTrue(autotmux._tmux_step_aside())
        run.assert_called_once()
        argv = run.call_args.args
        self.assertEqual(argv.count(';'), 5)
        self.assertEqual(argv.count('\\;'), 6)
        self.assertIn('bind-key', argv)
        self.assertIn('prefix2', argv)
        self.assertIn('status', argv)

    def test_restore_transaction_is_followed_by_best_effort_size_refresh(self):
        context = autotmux._outer_tmux_context()
        original = {
            'prefix': 'C-a', 'prefix2': 'C-z',
            'key-table': None, 'status': None,
        }
        autotmux._outer_tmux_state = {
            'context': context, 'owner_id': 'mine', 'original': original,
        }
        lease = {
            'version': autotmux._OUTER_TMUX_LEASE_VERSION,
            'table': context['table'], 'original': original,
            'owners': [{'id': 'mine', 'pid': os.getpid(),
                        'token': autotmux.lifecycle.process_token(os.getpid())}],
        }
        with mock.patch.object(
                autotmux, '_outer_tmux_snapshot',
                return_value=(original, autotmux._encode_outer_tmux_lease(lease))), \
             mock.patch.object(autotmux, '_acquire_outer_tmux_lock', return_value=9), \
             mock.patch.object(autotmux, '_release_outer_tmux_lock'), \
             mock.patch.object(autotmux, '_tmux', return_value=True) as run:
            self.assertTrue(autotmux._tmux_restore())
        self.assertEqual(run.call_count, 2)
        restore_args = run.call_args_list[0].args
        self.assertEqual(restore_args.count(';'), 5)
        self.assertEqual(
            run.call_args_list[1].args, ('refresh-client', '-S'))
        self.assertIn('C-a', restore_args)
        self.assertIn('C-z', restore_args)
        self.assertIn('-u', restore_args)
        self.assertIsNone(autotmux._outer_tmux_state)

    def test_missing_metadata_is_not_silently_accepted_while_transparent(self):
        context = autotmux._outer_tmux_context()
        original = {name: None for name in autotmux._OUTER_TMUX_OPTIONS}
        autotmux._outer_tmux_state = {
            'context': context, 'owner_id': 'mine', 'original': original,
        }
        current = dict(original)
        current.update({
            'prefix': 'None', 'prefix2': 'None',
            'key-table': context['table'], 'status': 'off',
        })
        with mock.patch.object(
                autotmux, '_outer_tmux_snapshot', return_value=(current, None)), \
             mock.patch.object(autotmux, '_acquire_outer_tmux_lock', return_value=9), \
             mock.patch.object(autotmux, '_release_outer_tmux_lock'), \
             mock.patch.object(autotmux, '_tmux') as run:
            self.assertFalse(autotmux._tmux_restore())
        run.assert_not_called()
        self.assertIsNotNone(autotmux._outer_tmux_state)

    def test_direct_attach_steps_outer_tmux_aside_and_restores(self):
        os.environ['TMUX'] = '/tmp/x,0,0'
        with mock.patch.object(autotmux, '_request_daemon_start'), \
             mock.patch.object(
                 autotmux, '_tmux_step_aside', return_value=True) as step, \
             mock.patch.object(
                 autotmux, '_tmux_restore', return_value=True) as restore, \
             mock.patch.object(
                 autotmux, '_run_user_command', return_value=(0, '')) as run:
            self.assertEqual(autotmux._direct_attach('localhost:main'), 0)
        step.assert_called_once_with()
        restore.assert_called_once_with()
        run.assert_called_once_with(['tmux', 'attach', '-t', 'main'])

    def test_outer_client_handoff_removes_outer_tmux_from_data_path(self):
        os.environ['TMUX'] = '/tmp/outer.sock,123,0'
        with mock.patch.object(
                autotmux, '_tmux_output', return_value='/dev/pts/42\n'), \
             mock.patch.object(autotmux, '_tmux', return_value=True) as run, \
             mock.patch.object(autotmux, '_GATEWAY_POOL', None):
            self.assertTrue(autotmux._handoff_outer_tmux_client(
                ['--attach', 'gpu1:train session']))
        args = run.call_args.args
        self.assertEqual(args[:4], (
            'detach-client', '-t', '/dev/pts/42', '-E'))
        command = args[4]
        self.assertIn('unset TMUX TMUX_PANE', command)
        self.assertIn('--attach', command)
        self.assertIn('gpu1:train session', command)
        self.assertIn('/tmp/outer.sock', command)
        self.assertIn("attach-session -t '$0'", command)

    def test_outer_client_handoff_fails_closed_without_safe_client_tty(self):
        for value in (None, '', 'relative', '/dev/pts/1\n/dev/pts/2'):
            with self.subTest(value=value), \
                 mock.patch.object(autotmux, '_tmux_output',
                                   return_value=value), \
                 mock.patch.object(autotmux, '_tmux') as run, \
                 mock.patch.object(autotmux, '_GATEWAY_POOL', None):
                self.assertFalse(autotmux._handoff_outer_tmux_client(
                    ['--attach', 'gpu1:train']))
                run.assert_not_called()

    def test_outer_client_handoff_preserves_live_gateway_order(self):
        os.environ['TMUX'] = '/tmp/outer.sock,123,0'
        pool = SimpleNamespace(
            gateways=('login1', 'login2'), active_gateway='login2')
        with mock.patch.object(
                autotmux, '_tmux_output', return_value='/dev/pts/42'), \
             mock.patch.object(autotmux, '_tmux', return_value=True) as run, \
             mock.patch.object(autotmux, '_GATEWAY_POOL', pool):
            self.assertTrue(autotmux._handoff_outer_tmux_client(
                ['--attach', 'gpu1:train']))
        command = run.call_args.args[4]
        self.assertLess(
            command.index('--gateway login2'),
            command.index('--gateway login1'))

    def test_direct_attach_restore_failure_is_visible_and_nonzero(self):
        import io
        os.environ['TMUX'] = '/tmp/x,0,0'
        stderr = io.StringIO()
        with mock.patch.object(autotmux, '_request_daemon_start'), \
             mock.patch.object(autotmux, '_tmux_step_aside', return_value=True), \
             mock.patch.object(autotmux, '_tmux_restore', return_value=False), \
             mock.patch.object(
                 autotmux, '_run_user_command', return_value=(0, '')), \
             mock.patch.object(autotmux.sys, 'stderr', stderr):
            self.assertEqual(autotmux._direct_attach('localhost:main'), 1)
        self.assertIn('press F12', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
