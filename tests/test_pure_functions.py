"""Pure-function tests for autotmux + autotmux_daemon.

Covers what we can test without a running daemon or terminal: file I/O
helpers, state-shape building, and the small bookkeeping functions.
Run with `python -m unittest tests/test_pure_functions.py`.
"""
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux
from autotmux import daemon as d


class ReadStateTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            autotmux.STATE_FILE = os.path.join(td, 'nope.json')
            self.assertEqual(autotmux.read_state(), {})

    def test_malformed_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 's.json')
            with open(p, 'w') as f:
                f.write('not json {')
            autotmux.STATE_FILE = p
            self.assertEqual(autotmux.read_state(), {})

    def test_valid_state_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 's.json')
            payload = {'updated': 'now', 'nodes': {}, 'squeue_long': 'x'}
            with open(p, 'w') as f:
                json.dump(payload, f)
            autotmux.STATE_FILE = p
            self.assertEqual(autotmux.read_state(), payload)

    def test_valid_json_with_wrong_top_level_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'state.json')
            with open(p, 'w') as f:
                json.dump(['not', 'a', 'state'], f)
            autotmux.STATE_FILE = p
            self.assertEqual(autotmux.read_state(), {})

    def test_checked_reader_distinguishes_valid_empty_from_failed_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'state.json')
            autotmux.STATE_FILE = p
            self.assertEqual(autotmux._read_state_checked(), (False, {}))
            with open(p, 'w') as f:
                json.dump({}, f)
            self.assertEqual(autotmux._read_state_checked(), (True, {}))

    def test_runtime_reader_rejects_symlink_fifo_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, 'target.json')
            link = os.path.join(td, 'link.json')
            fifo = os.path.join(td, 'state.fifo')
            large = os.path.join(td, 'large.json')
            with open(target, 'w') as f:
                json.dump({}, f)
            os.symlink(target, link)
            os.mkfifo(fifo)
            with open(large, 'w') as f:
                f.write('{"value":"' + 'x' * 64 + '"}')
            started = d.time.monotonic()
            self.assertEqual(
                autotmux._read_json_dict_checked(link, 1024), (False, {}))
            self.assertEqual(
                autotmux._read_json_dict_checked(fifo, 1024), (False, {}))
            self.assertLess(d.time.monotonic() - started, 0.5)
            self.assertEqual(
                autotmux._read_json_dict_checked(large, 16), (False, {}))


class ReadSnapshotsTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'nope.json')
            self.assertEqual(autotmux.read_snapshots(), {})

    def test_malformed_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'snap.json')
            with open(p, 'w') as f:
                f.write('garbage')
            autotmux.SNAPSHOT_FILE = p
            self.assertEqual(autotmux.read_snapshots(), {})

    def test_valid_json_list_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'snap.json')
            with open(p, 'w') as f:
                json.dump(['legacy'], f)
            autotmux.SNAPSHOT_FILE = p
            self.assertEqual(autotmux.read_snapshots(), {})


class BuildSessionRowsTests(unittest.TestCase):
    def test_empty_state(self):
        self.assertEqual(autotmux.build_session_rows({}), [])

    def test_malformed_nested_shapes_do_not_crash(self):
        self.assertEqual(autotmux.build_session_rows({'nodes': []}), [])
        state = {'nodes': {'bad': [], 'also-bad': {'info': [], 'sessions': 'x'}}}
        rows = autotmux.build_session_rows(state)
        self.assertEqual(rows, [(
            'also-bad', autotmux._OFFLINE_SESSION,
            '-', '', 'OFFLINE', '', '',
        )])

    def test_offline_node_shows_last_error(self):
        state = {
            'nodes': {
                'h1': {
                    'alive': False,
                    'info': {'time': '1:00'},
                    'sessions': [],
                    'last_error': 'connect timeout',
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'h1')
        self.assertEqual(rows[0][1], autotmux._OFFLINE_SESSION)
        self.assertEqual(autotmux._session_label(rows[0][1]), '<offline>')
        self.assertIn('connect timeout', rows[0][4])

    def test_alive_node_with_sessions(self):
        state = {
            'nodes': {
                'h1': {
                    'alive': True,
                    'info': {'time': '2:00'},
                    'sessions': [['main', '3'], ['scratch', '1']],
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 'main')
        self.assertEqual(rows[0][2], '3')
        self.assertEqual(rows[0][4], 'Active')

    def test_high_remote_escape_time_is_visible_on_tmux_rows(self):
        state = {
            'nodes': {
                'h1': {
                    'alive': True,
                    'info': {'time': '2:00', 'escape_time': '500'},
                    'sessions': [['main', '1']],
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertIn('ESC 500ms', rows[0][4])

    def test_low_or_malformed_escape_time_does_not_warn(self):
        for value in ('10', 'not-a-number', None):
            state = {'nodes': {'h1': {
                'alive': True,
                'info': {'escape_time': value},
                'sessions': [['main', '1']],
            }}}
            self.assertEqual(
                autotmux.build_session_rows(state)[0][4], 'Active')

    def test_local_escape_time_is_not_flagged_as_remote_latency(self):
        state = {'nodes': {'localhost': {
            'alive': True,
            'info': {'escape_time': '500'},
            'sessions': [['main', '1']],
        }}}
        self.assertEqual(autotmux.build_session_rows(state)[0][4], 'Active')

    def test_alive_node_no_sessions_shows_start_shell(self):
        state = {'nodes': {'h1': {'alive': True, 'info': {}, 'sessions': []}}}
        rows = autotmux.build_session_rows(state)
        self.assertEqual(rows[0][1], autotmux._START_SHELL_SESSION)
        self.assertEqual(
            autotmux._session_label(rows[0][1]), '<shell>')

    def test_real_session_named_like_placeholder_remains_real(self):
        state = {'nodes': {'h1': {
            'alive': True,
            'info': {},
            'sessions': [['<offline>', '1'], ['<Start Shell>', '2']],
        }}}
        rows = autotmux.build_session_rows(state)
        self.assertEqual(
            {row[1] for row in rows}, {'<offline>', '<Start Shell>'})
        self.assertNotIn(autotmux._OFFLINE_SESSION, {row[1] for row in rows})
        self.assertNotIn(
            autotmux._START_SHELL_SESSION, {row[1] for row in rows})

    def test_alive_node_surfaces_degraded_error_in_status(self):
        state = {'nodes': {'h1': {
            'alive': True,
            'info': {},
            'sessions': [['main', '1']],
            'last_error': 'tmux list timed out',
        }}}
        rows = autotmux.build_session_rows(state)
        self.assertIn('DEGRADED', rows[0][4])
        self.assertIn('timed out', rows[0][4])

    def test_rows_sorted_by_node_then_session(self):
        state = {
            'nodes': {
                'b': {'alive': True, 'info': {}, 'sessions': [['z', '1'], ['a', '1']]},
                'a': {'alive': True, 'info': {}, 'sessions': [['m', '1']]},
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [('a', 'm'), ('b', 'a'), ('b', 'z')])


class PreviewCadenceTests(unittest.TestCase):
    def test_unchanged_content_backs_off_and_changed_content_resets(self):
        streak = 0
        delays = []
        for _ in range(5):
            streak, delay = autotmux._next_preview_cadence(True, streak)
            delays.append(delay)
        self.assertEqual(delays, [2.0, 4.0, 8.0, 8.0, 8.0])
        self.assertEqual(
            autotmux._next_preview_cadence(False, streak), (0, 1.0))


class AtomicWriteTests(unittest.TestCase):
    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'out.json')
            d._atomic_write_json(p, {'a': 1, 'b': [2, 3]})
            with open(p) as f:
                self.assertEqual(json.load(f), {'a': 1, 'b': [2, 3]})

    def test_no_tmp_leftover_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'out.json')
            d._atomic_write_json(p, {'x': 1})
            tmps = [n for n in os.listdir(td) if '.tmp' in n]
            self.assertEqual(tmps, [])

    def test_no_tmp_leftover_on_serialization_error(self):
        # An object that can't be JSON-serialized
        class NotJSON:
            pass
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'out.json')
            with self.assertRaises(TypeError):
                d._atomic_write_json(p, {'bad': NotJSON()})
            tmps = [n for n in os.listdir(td) if '.tmp' in n]
            self.assertEqual(tmps, [], "tmp file leaked after serialization error")


class AtomicWriteUniqueTmpTests(unittest.TestCase):
    """Each write must use a distinct tmp path so the three daemon loop
    threads writing the same state file can't truncate each other's tmp."""

    def test_each_write_uses_a_distinct_tmp_path(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'out.json')
            seen = []
            real_replace = os.replace

            def rec_replace(src, dst):
                seen.append(src)
                return real_replace(src, dst)

            with mock.patch('autotmux.daemon.os.replace', rec_replace):
                d._atomic_write_json(p, {'a': 1})
                d._atomic_write_json(p, {'a': 2})
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(
                seen[0], seen[1],
                "each write must use a unique tmp path (concurrent writers collide otherwise)")


class GetNodesAliasingTests(unittest.TestCase):
    """A multi-node job (`node[01-02]`) must expand into independent info
    dicts — sharing one dict makes per-node sessions/load overwrite siblings."""

    def test_expanded_range_nodes_are_independent_dicts(self):
        def fake_check_output(cmd, *a, **k):
            if cmd[0] == 'squeue':
                # daemon parses on \x1f (a '|' can appear in a job name/reason)
                return ('node[01-02]\x1f1:00\x1fjob\x1f123\x1fpart\x1fuser'
                        '\x1fRUNNING\x1f0:30\x1f2\x1freason\n')
            if cmd[0] == 'scontrol':
                return 'node01\nnode02\n'
            return ''

        with mock.patch('autotmux.daemon.subprocess.check_output', fake_check_output):
            nodes = d._get_nodes()
        self.assertIn('node01', nodes)
        self.assertIn('node02', nodes)
        self.assertIsNot(nodes['node01'], nodes['node02'],
                         "expanded range nodes must not share a single info dict")
        nodes['node01']['sessions'] = ['only-node01']
        self.assertNotIn('sessions', nodes['node02'],
                         "mutating one expanded node bled into its sibling")

    def test_pipe_in_job_name_does_not_corrupt_fields(self):
        # A '|' inside the job name must not shift STATE/job_id (the reason we
        # switched the squeue delimiter to \x1f).
        def fake_check_output(cmd, *a, **k):
            if cmd[0] == 'squeue':
                return ('node9\x1f1:00\x1fmy|weird|job\x1f123\x1fpart\x1fuser'
                        '\x1fRUNNING\x1f0:30\x1f1\x1freason\n')
            return ''
        with mock.patch('autotmux.daemon.subprocess.check_output', fake_check_output):
            nodes = d._get_nodes()
        self.assertIn('node9', nodes)
        self.assertEqual(nodes['node9']['job_name'], 'my|weird|job')
        self.assertEqual(nodes['node9']['job_id'], '123')
        self.assertEqual(nodes['node9']['state'], 'RUNNING')


class DaemonSingletonLockTests(unittest.TestCase):
    """The daemon must hold an exclusive lock so two `atd start` races can't
    both spawn a daemon."""

    def tearDown(self):
        d._release_singleton_lock()

    def test_second_acquire_fails_while_held(self):
        with tempfile.TemporaryDirectory() as td:
            lockfile = os.path.join(td, 'daemon.pid.lock')
            guardfile = os.path.join(td, 'daemon.guard')
            with mock.patch.object(d, 'LOCK_FILE', lockfile), \
                 mock.patch.object(d, 'GUARD_FILE', guardfile):
                self.assertTrue(d._acquire_singleton_lock(),
                                "first acquire should succeed")
                self.assertFalse(d._acquire_singleton_lock(),
                                 "second acquire must fail while the lock is held")

    def test_lock_open_error_is_not_misreported_as_another_daemon(self):
        denied = PermissionError(13, 'Permission denied', '/bad/guard')
        with mock.patch.object(d.lifecycle, 'open_lock_file', side_effect=denied):
            with self.assertRaises(PermissionError):
                d._acquire_singleton_lock()

    def test_acquire_clears_previous_daemon_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            guard = os.path.join(td, 'guard')
            lock = os.path.join(td, 'runtime.lock')
            for path in (guard, lock):
                with open(path, 'w') as f:
                    f.write('{"pid": 1, "base": "/stale"}')
            with mock.patch.object(d, 'GUARD_FILE', guard), \
                 mock.patch.object(d, 'LOCK_FILE', lock):
                self.assertTrue(d._acquire_singleton_lock())
                self.assertEqual(os.path.getsize(guard), 0)
                self.assertEqual(os.path.getsize(lock), 0)


class DaemonControlUXTests(unittest.TestCase):
    def test_log_files_are_private_and_special_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, 'daemon.log')
            backup = log_path + '.1'
            with open(log_path, 'w') as f:
                f.write('current')
            with open(backup, 'w') as f:
                f.write('backup')
            os.chmod(log_path, 0o644)
            os.chmod(backup, 0o644)

            d._prepare_log_files(log_path)

            self.assertEqual(os.stat(log_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

            target = os.path.join(td, 'target')
            link = os.path.join(td, 'linked.log')
            fifo = os.path.join(td, 'fifo.log')
            with open(target, 'w'):
                pass
            os.symlink(target, link)
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                d._prepare_log_files(link, backup_count=0)
            with self.assertRaises(OSError):
                d._prepare_log_files(fifo, backup_count=0)

    def test_startup_handshake_parses_ready_and_error_messages(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'PID 4321\nREADY\n')
        os.close(write_fd)
        try:
            self.assertEqual(
                d._wait_startup_message(read_fd, 0.5),
                (True, 4321, ''),
            )
        finally:
            os.close(read_fd)

        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'PID 4322\nERROR injected failure\n')
        os.close(write_fd)
        try:
            self.assertEqual(
                d._wait_startup_message(read_fd, 0.5),
                (False, 4322, 'injected failure'),
            )
        finally:
            os.close(read_fd)

    def test_detached_child_startup_failure_returns_nonzero_to_caller(self):
        # Keep the runtime dir short. The default temp location is deep enough
        # on macOS that _pick_base() rejects it for socket length and silently
        # falls back to the real /tmp base -- so the test would inspect the
        # developer's live daemon instead of its own isolated one.
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            runtime = os.path.join(td, 'runtime')
            os.mkdir(runtime, 0o700)
            guard = os.path.join(td, 'daemon.guard')
            env = os.environ.copy()
            env['XDG_RUNTIME_DIR'] = runtime
            env['AUTOTMUX_GUARD_FILE'] = guard
            script = """
import sys
from autotmux import daemon

def fail(*_args, **_kwargs):
    raise RuntimeError('injected-startup-failure')

daemon._load_runtime_configuration = fail
sys.argv = ['atd', 'start']
daemon.main_entry()
"""
            result = subprocess.run(
                [sys.executable, '-c', script], env=env,
                capture_output=True, text=True, timeout=15,
            )
            pid_file = os.path.join(runtime, 'autotmux', 'daemon.pid')
            self.assertEqual(result.returncode, 1,
                             result.stdout + result.stderr)
            self.assertIn('Startup failed', result.stderr)
            self.assertIn('injected-startup-failure', result.stderr)
            self.assertFalse(os.path.exists(pid_file))

    def test_start_reports_lock_setup_failure_without_traceback(self):
        error = PermissionError(13, 'Permission denied', '/bad/guard')
        stderr = io.StringIO()
        with mock.patch.object(d, '_resolve_daemon_pid', return_value=None), \
             mock.patch.object(d, '_lock_held', return_value=False), \
             mock.patch.object(d, '_acquire_singleton_lock', side_effect=error), \
             mock.patch.object(d.sys, 'stderr', stderr):
            self.assertFalse(d.cmd_start())
        self.assertIn('Could not acquire daemon lock', stderr.getvalue())

    def test_restart_has_no_fixed_delay_after_confirmed_stop(self):
        with mock.patch.object(d, 'cmd_stop', return_value=True), \
             mock.patch.object(d, 'cmd_start', return_value=True), \
             mock.patch.object(d.time, 'sleep') as sleep:
            self.assertTrue(d.cmd_restart())
        sleep.assert_not_called()

    def test_failed_stop_sets_nonzero_command_exit(self):
        with mock.patch.object(d.sys, 'argv', ['atd', 'stop']), \
             mock.patch.object(d, 'cmd_stop', return_value=False):
            with self.assertRaises(SystemExit) as raised:
                d.main_entry()
        self.assertEqual(raised.exception.code, 1)

    def test_status_sets_nonzero_command_exit_when_daemon_is_down(self):
        with mock.patch.object(d.sys, 'argv', ['atd', 'status']), \
             mock.patch.object(d, 'cmd_status', return_value=False):
            with self.assertRaises(SystemExit) as raised:
                d.main_entry()
        self.assertEqual(raised.exception.code, 1)

    def test_control_command_rejects_ignored_extra_options(self):
        stderr = io.StringIO()
        with mock.patch.object(d.sys, 'argv', ['atd', 'start', '--typo']), \
             mock.patch.object(d, 'cmd_start') as start, \
             mock.patch.object(d.sys, 'stderr', stderr):
            with self.assertRaises(SystemExit) as raised:
                d.main_entry()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('--typo', stderr.getvalue())
        start.assert_not_called()

    def test_short_version_alias_is_case_insensitive(self):
        stdout = io.StringIO()
        with mock.patch.object(d.sys, 'argv', ['atd', '-V']), \
             mock.patch.object(d.sys, 'stdout', stdout):
            d.main_entry()
        self.assertIn('autotmux-daemon', stdout.getvalue())

    def test_guard_json_remains_a_pid_resolution_source(self):
        with tempfile.TemporaryDirectory() as td:
            guard = os.path.join(td, 'guard')
            with open(guard, 'w') as f:
                json.dump({'pid': 4321, 'base': td}, f)
            self.assertEqual(d._read_int_file(guard), 4321)

    def test_log_disappearing_between_exists_and_open_is_clean(self):
        stdout = io.StringIO()
        with mock.patch.object(d, '_tail_owned_text',
                               side_effect=FileNotFoundError), \
             mock.patch.object(d.sys, 'stdout', stdout):
            self.assertTrue(d.cmd_logs())
        self.assertIn('no log yet', stdout.getvalue())

    def test_log_permission_error_is_reported_and_fails(self):
        stderr = io.StringIO()
        with mock.patch.object(d, '_tail_owned_text',
                               side_effect=PermissionError('denied')), \
             mock.patch.object(d.sys, 'stderr', stderr):
            self.assertFalse(d.cmd_logs())
        self.assertIn('Could not read log', stderr.getvalue())

    def test_log_command_is_a_bounded_fifty_line_tail(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, 'daemon.log')
            with open(log_path, 'w') as f:
                for index in range(100):
                    f.write(f'line-{index}\n')
            rendered = d._tail_owned_text(log_path)
        self.assertNotIn('line-49\n', rendered)
        self.assertIn('line-50\n', rendered)
        self.assertTrue(rendered.endswith('line-99\n'))
        self.assertEqual(len(rendered.splitlines()), 50)

    def test_status_labels_stale_nodes_as_last_known_when_daemon_is_down(self):
        state = {
            'updated': '2026-01-01 00:00:00',
            'updated_monotonic': 10.0,
            'nodes': {'gpu1': {'alive': True, 'socket': '/tmp/cm'}},
        }
        stdout = io.StringIO()
        with mock.patch.object(d, '_resolve_daemon_pid', return_value=None), \
             mock.patch.object(d, '_lock_held', return_value=False), \
             mock.patch.object(d.time, 'monotonic', return_value=100.0), \
             mock.patch.object(d, '_read_json_dict', return_value=state), \
             mock.patch.object(d.sys, 'stdout', stdout):
            d.cmd_status()
        output = stdout.getvalue()
        self.assertIn('Not running', output)
        self.assertIn('stale (90s old)', output)
        self.assertIn('Last-known nodes', output)

    def test_json_status_exposes_state_freshness(self):
        state = {'updated_monotonic': 10.0, 'nodes': {}}
        stdout = io.StringIO()
        with mock.patch.object(d, '_resolve_daemon_pid', return_value=123), \
             mock.patch.object(d, '_lock_held', return_value=True), \
             mock.patch.object(d.time, 'monotonic', return_value=80.0), \
             mock.patch.object(d, '_read_json_dict', return_value=state), \
             mock.patch.object(d.sys, 'stdout', stdout):
            d.cmd_status(as_json=True)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload['state_age_seconds'], 70.0)
        self.assertTrue(payload['state_stale'])

    def test_json_status_never_calls_unknown_timestamp_fresh(self):
        state = {'nodes': {'localhost': {'alive': True}}}
        stdout = io.StringIO()
        with mock.patch.object(d, '_resolve_daemon_pid', return_value=123), \
             mock.patch.object(d, '_lock_held', return_value=True), \
             mock.patch.object(d, '_read_json_dict', return_value=state), \
             mock.patch.object(d.sys, 'stdout', stdout):
            d.cmd_status(as_json=True)
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(payload['state_age_seconds'])
        self.assertTrue(payload['state_stale'])

    def test_status_age_ignores_monotonic_value_from_another_boot(self):
        state = {
            'updated_monotonic': 995.0,
            'monotonic_clock_id': 'host:old-boot',
            'updated': 'timestamp',
        }
        with mock.patch.object(
                d.lifecycle, 'monotonic_clock_id', return_value='host:new-boot'), \
             mock.patch.object(d.time, 'time', return_value=1000.0), \
             mock.patch.object(d.time, 'mktime', return_value=900.0), \
             mock.patch.object(d.time, 'strptime', return_value=object()):
            self.assertEqual(d._state_age_seconds(state), 100.0)


class DaemonBookkeepingTests(unittest.TestCase):
    def test_squeue_display_text_is_bounded_and_marks_truncation(self):
        text = 'x' * 100
        out = d._limit_squeue_text(text, limit=20)
        self.assertLess(len(out), len(text))
        self.assertIn('80 characters omitted', out)

    def test_status_includes_monotonic_freshness_fields(self):
        status = d._build_status()
        self.assertIsInstance(status['updated_monotonic'], float)
        self.assertEqual(status['monotonic_clock_id'], d._CLOCK_ID)
        self.assertIn('squeue_updated_monotonic', status)
        self.assertIn('keepalive_health', status)
        self.assertIn('interval', status['keepalive_health'])

    def test_master_alive_localhost_always_true(self):
        self.assertTrue(d._master_alive('localhost'))
        self.assertTrue(d._master_alive('localhost', deep=True))

    def test_get_nodes_always_includes_localhost(self):
        with mock.patch('autotmux.daemon.subprocess.check_output', return_value=''):
            nodes = d._get_nodes()
        self.assertIn('localhost', nodes)
        self.assertEqual(nodes['localhost']['state'], 'LOCAL')

    def test_backoff_progression(self):
        d._master_backoff.clear()
        node = '__test__'
        self.assertFalse(d._backoff_should_skip(node))
        delay1 = d._backoff_record_failure(node)
        self.assertEqual(delay1, d.BACKOFF_BASE)
        self.assertTrue(d._backoff_should_skip(node))
        delay2 = d._backoff_record_failure(node)
        self.assertEqual(delay2, d.BACKOFF_BASE * 2)
        d._backoff_clear(node)
        self.assertFalse(d._backoff_should_skip(node))
        d._master_backoff.clear()

    def test_backoff_caps_at_max(self):
        d._master_backoff.clear()
        node = '__test_cap__'
        for _ in range(20):
            delay = d._backoff_record_failure(node)
        self.assertLessEqual(delay, d.BACKOFF_CAP)
        d._master_backoff.clear()

    def test_record_error_set_and_clear(self):
        node = '__test_err__'
        d._known_nodes_info[node] = {}
        try:
            d._record_error(node, 'boom')
            self.assertEqual(d._known_nodes_info[node]['last_error'], 'boom')
            d._record_error(node, None)
            self.assertNotIn('last_error', d._known_nodes_info[node])
        finally:
            d._known_nodes_info.pop(node, None)

    def test_record_error_truncates_long_messages(self):
        node = '__test_long__'
        d._known_nodes_info[node] = {}
        try:
            d._record_error(node, 'x' * 500)
            self.assertEqual(len(d._known_nodes_info[node]['last_error']), 200)
        finally:
            d._known_nodes_info.pop(node, None)

    def test_record_error_on_unknown_node_is_noop(self):
        # Should not raise even if the node has no entry.
        d._record_error('__nope__', 'something')


def _probe_payload(sessions: str, info: str) -> str:
    return ('login banner' + d._SESSION_SECTION + sessions
            + d._NODEINFO_SECTION + '\n' + info + d._TMUXINFO_SECTION + '\n10\n')


class SessionActivityParsingTests(unittest.TestCase):
    """The probe reports each session's last-activity stamp and the remote
    clock, sampled together so idle time never depends on clock agreement
    between the laptop and the node."""

    NOW = 1_000_000

    def _sessions(self, sessions_text, clock=True):
        info = f'8\n0.50, 0.40, 0.30\n' + (f'{self.NOW}\n' if clock else '')
        return d._parse_session_payload(_probe_payload(sessions_text, info))[0]

    def test_idle_is_measured_against_the_remote_clock(self):
        rows = self._sessions(
            f'{self.NOW - 60}:2:fresh\n{self.NOW - 900}:1:quiet\n')
        self.assertEqual(rows, [['fresh', '2', 60], ['quiet', '1', 900]])

    def test_session_name_may_contain_colons(self):
        """Activity and window count lead precisely so the name can hold ':'."""
        rows = self._sessions(f'{self.NOW - 5}:1:proj:sub:run\n')
        self.assertEqual(rows, [['proj:sub:run', '1', 5]])

    def test_activity_in_the_future_never_reports_negative_idle(self):
        rows = self._sessions(f'{self.NOW + 120}:1:skewed\n')
        self.assertEqual(rows, [['skewed', '1', 0]])

    def test_older_daemon_without_a_clock_line_still_parses(self):
        self.assertEqual(
            self._sessions(f'{self.NOW - 60}:2:fresh\n', clock=False),
            [['fresh', '2']])

    def test_unparsable_activity_is_dropped_not_guessed(self):
        self.assertEqual(self._sessions('nope:2:fresh\n'), [['fresh', '2']])
        self.assertEqual(self._sessions('bare-name\n'), [['bare-name', '?']])


class IdleMarkerTests(unittest.TestCase):
    def test_quiet_sessions_are_marked_only_past_the_threshold(self):
        self.assertEqual(autotmux._idle_marker(autotmux.model.IDLE_HINT_SECONDS - 1), '')
        self.assertEqual(autotmux._idle_marker(autotmux.model.IDLE_HINT_SECONDS), '● 5m')
        self.assertEqual(autotmux._idle_marker(900), '● 15m')
        self.assertEqual(autotmux._idle_marker(7200), '● 2h')
        self.assertEqual(autotmux._idle_marker(90_000), '● 1d')

    def test_nonsense_idle_values_never_produce_a_marker(self):
        for value in (None, -1, True, False, 'x', float('nan'), float('inf')):
            with self.subTest(value=value):
                self.assertEqual(autotmux._idle_marker(value), '')

    @staticmethod
    def _lead_cell(status):
        """Render the leading IDLE cell the way the table does."""
        marker, _rest = autotmux._split_idle_marker(status)
        return autotmux._idle_cell(marker)

    def test_colour_escalates_with_age_and_is_scoped_to_the_dot(self):
        quiet = self._lead_cell('● 15m Active')
        stale = self._lead_cell('● 2h Active')
        self.assertEqual([(s.start, s.end) for s in quiet.spans], [(0, 1)])
        self.assertEqual(str(quiet.spans[0].style), 'yellow')
        self.assertEqual(str(stale.spans[0].style), 'red')
        # An hour expressed in minutes is the same tier as one expressed in h.
        self.assertEqual(str(self._lead_cell('● 60m Active').spans[0].style),
                         'red')

    def test_ordinary_status_text_is_left_unstyled(self):
        for text in ('Active', 'OFFLINE: boom', 'No sessions'):
            with self.subTest(text=text):
                self.assertEqual(self._lead_cell(text).spans, [])

    def test_status_text_is_separated_from_the_marker(self):
        self.assertEqual(autotmux._split_idle_marker('● 15m Active'),
                         ('● 15m', 'Active'))
        self.assertEqual(
            autotmux._split_idle_marker('● 2h DEGRADED: connect timeout'),
            ('● 2h', 'DEGRADED: connect timeout'))
        self.assertEqual(autotmux._split_idle_marker('Active'), ('', 'Active'))
        self.assertEqual(autotmux._split_idle_marker(''), ('', ''))


class CompactCellTests(unittest.TestCase):
    """The table has to fit seven columns beside a preview pane, so each cell
    is written for width. None of these affect routing -- the row tuple keeps
    the real values."""

    def test_login_nodes_lose_the_cluster_domain(self):
        self.assertEqual(
            autotmux._node_label('login--holylogin06.rc.fas.harvard.edu'),
            'login:holylogin06')

    def test_compute_nodes_are_left_alone(self):
        self.assertEqual(autotmux._node_label('holygpu8a11104'),
                         'holygpu8a11104')
        self.assertEqual(autotmux._node_label('localhost'), 'localhost')

    def test_node_label_never_returns_empty_for_a_named_node(self):
        for node in ('login--login1', 'node.with.dots', 'x'):
            with self.subTest(node=node):
                self.assertTrue(autotmux._node_label(node))

    def test_time_left_is_reduced_to_a_magnitude(self):
        cases = {
            '1-01:47:12': '1d1h', '1-00:00:00': '1d', '2:05:30': '2h05',
            '59:30': '59m', '0:45': '0m', 'UNLIMITED': '∞',
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(autotmux._time_left_label(raw), want)

    def test_unparseable_time_is_shown_as_given(self):
        """Better an odd-looking cell than a confidently wrong duration."""
        for raw in ('-', 'N/A', 'garbage', ''):
            with self.subTest(raw=raw):
                self.assertEqual(autotmux._time_left_label(raw), raw)

    def test_load_and_cpus_share_one_cell(self):
        self.assertEqual(autotmux._load_label('4.44', '12'), '4.4/12')
        self.assertEqual(autotmux._load_label('65.88', '1'), '65.9/1')

    def test_load_cell_degrades_when_a_half_is_missing(self):
        self.assertEqual(autotmux._load_label('', '12'), '12')
        self.assertEqual(autotmux._load_label('4.4', ''), '4.4')
        self.assertEqual(autotmux._load_label('', ''), '')
        self.assertEqual(autotmux._load_label('n/a', '12'), 'n/a/12')

    def test_cpu_count_is_probed_machine_wide(self):
        """The load average is node-wide, so the divisor has to be too.

        Plain `nproc` reports the CPUs the calling process may run on, and an
        SSH session adopted into a Slurm job's cgroup sees as few as one -- so
        an idle 96-core node reported `4.2/1` and read as four times
        oversubscribed.
        """
        script = d._session_probe_script()
        self.assertIn('nproc --all', script)
        # And a machine without GNU coreutils still answers with something.
        self.assertIn('getconf _NPROCESSORS_ONLN', script)

    def test_an_uneventful_status_is_left_blank(self):
        """"Active" on every healthy row is a constant that buries the rows
        which do have something to report."""
        self.assertEqual(autotmux._status_text('Active'), '')
        self.assertEqual(autotmux._status_text('No sessions'), '')

    def test_anything_worth_reading_is_kept(self):
        for text in ('OFFLINE: connect timeout', 'DEGRADED: boom',
                     '⚠ ESC 500ms'):
            with self.subTest(text=text):
                self.assertEqual(autotmux._status_text(text), text)

    def test_a_warning_survives_without_its_baseline(self):
        self.assertEqual(
            autotmux._status_text('Active · ⚠ NET retry 5s'), '⚠ NET retry 5s')
        self.assertEqual(
            autotmux._status_text('No sessions · ⚠ NET retry 5s'),
            '⚠ NET retry 5s')

    def test_window_count_rides_on_the_session_name(self):
        self.assertEqual(autotmux._session_cell('train', '1'), 'train')
        self.assertEqual(autotmux._session_cell('train', '3'), 'train ·3')

    def test_an_unknown_window_count_adds_nothing(self):
        for windows in ('-', '?', '', None, 'x'):
            with self.subTest(windows=windows):
                self.assertEqual(
                    autotmux._session_cell('train', windows), 'train')

    def test_start_shell_placeholder_is_short(self):
        """It appears on every session-less node, so its width sets the
        SESSION column for the whole table."""
        label = autotmux._session_label(autotmux._START_SHELL_SESSION)
        self.assertEqual(label, '<shell>')
        self.assertLessEqual(len(label), 8)


class NewTerminalWindowTests(unittest.TestCase):
    """`o` promised a new window but could only make one inside tmux, so run
    from a plain terminal it did exactly what Enter does."""

    def test_hands_the_session_to_the_url_handler(self):
        with mock.patch.object(autotmux.sys, 'platform', 'darwin'), \
             mock.patch.object(autotmux.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '', '')
            ok, why = autotmux._open_new_terminal_window('gpu1', 'train')
        self.assertTrue(ok, why)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], 'open')
        self.assertEqual(argv[1], autotmux.notify.attach_url('gpu1', 'train'))

    def test_reports_a_missing_handler_instead_of_failing_silently(self):
        with mock.patch.object(autotmux.sys, 'platform', 'darwin'), \
             mock.patch.object(autotmux.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess(
                [], 1, '', 'No application knows how to open URL')
            ok, why = autotmux._open_new_terminal_window('gpu1', 'train')
        self.assertFalse(ok)
        self.assertIn('No application knows', why)

    def test_a_broken_open_command_is_not_an_exception(self):
        with mock.patch.object(autotmux.sys, 'platform', 'darwin'), \
             mock.patch.object(autotmux.subprocess, 'run',
                               side_effect=OSError('no open')):
            ok, why = autotmux._open_new_terminal_window('gpu1', 'train')
        self.assertFalse(ok)
        self.assertIn('no open', why)

    def test_rows_with_no_link_form_are_refused_before_spawning(self):
        for session in (autotmux._START_SHELL_SESSION, autotmux._OFFLINE_SESSION):
            with mock.patch.object(autotmux.sys, 'platform', 'darwin'), \
                 mock.patch.object(autotmux.subprocess, 'run') as run:
                ok, _why = autotmux._open_new_terminal_window('gpu1', session)
            self.assertFalse(ok)
            run.assert_not_called()

    def test_other_platforms_say_so_rather_than_pretending(self):
        with mock.patch.object(autotmux.sys, 'platform', 'linux'), \
             mock.patch.object(autotmux.subprocess, 'run') as run:
            ok, why = autotmux._open_new_terminal_window('gpu1', 'train')
        self.assertFalse(ok)
        self.assertIn('macOS', why)
        run.assert_not_called()


class AttentionOrderingTests(unittest.TestCase):
    """Node-name order gave the top of the table to whichever host sorted
    first, which says nothing about where to look."""

    def _row(self, node, session, status):
        return (node, session, '1', '1:00', status, '96', '4.0')

    def test_a_freshly_quiet_session_outranks_a_working_one(self):
        quiet = self._row('zz', 'train', f'{autotmux._IDLE_DOT} 8m Active')
        busy = self._row('aa', 'train', 'Active')
        self.assertLess(autotmux._attention_rank(quiet),
                        autotmux._attention_rank(busy))

    def test_a_long_dead_session_sinks_below_working_ones(self):
        """It stopped being news hours ago; keeping it on top would push live
        work off the screen."""
        stale = self._row('aa', 'train', f'{autotmux._IDLE_DOT} 9h Active')
        busy = self._row('zz', 'train', 'Active')
        self.assertGreater(autotmux._attention_rank(stale),
                           autotmux._attention_rank(busy))

    def test_an_offline_node_comes_first(self):
        offline = self._row('zz', autotmux._OFFLINE_SESSION, 'OFFLINE: timeout')
        degraded = self._row('zz', 'train', 'DEGRADED: no route')
        for row in (offline, degraded):
            self.assertEqual(autotmux._attention_rank(row), 0)

    def test_placeholder_rows_sink_to_the_bottom(self):
        placeholder = self._row('aa', autotmux._START_SHELL_SESSION, 'No sessions')
        real = self._row('zz', 'train', f'{autotmux._IDLE_DOT} 9h Active')
        self.assertGreater(autotmux._attention_rank(placeholder),
                           autotmux._attention_rank(real))

    def test_order_is_stable_within_a_tier(self):
        """Only the tier reorders; inside one, node and session still decide,
        so the table does not shuffle between refreshes."""
        state = {'nodes': {
            'nodeB': {'alive': True, 'info': {}, 'sessions': [['b', '1'], ['a', '1']]},
            'nodeA': {'alive': True, 'info': {}, 'sessions': [['z', '1']]},
        }}
        rows = autotmux.build_session_rows(state)
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [('nodeA', 'z'), ('nodeB', 'a'), ('nodeB', 'b')])


class LocalAttachTakeoverTests(unittest.TestCase):
    """tmux sizes a session's windows to its smallest attached client, so a
    second client pins the window small and leaves the rest of a bigger window
    blank. Every remote path already takes the session over; local ones did
    not, which is exactly where two displays of different sizes meet."""

    def test_local_attach_detaches_other_clients(self):
        self.assertEqual(autotmux._local_attach_argv('train'),
                         ['tmux', 'attach-session', '-d', '-t', 'train'])

    def test_the_session_name_is_passed_as_argv(self):
        """No shell is involved locally, so it needs no quoting -- and must not
        get any, or the quotes become part of the name."""
        argv = autotmux._local_attach_argv('my session')
        self.assertEqual(argv[-1], 'my session')

    def test_the_remote_wire_format_is_left_alone(self):
        """`_run_remote_user_command` and the gateway both parse a literal
        4-element ['tmux','attach','-t',NAME]; adding -d at the call sites
        would make the gateway reject its own request."""
        source = inspect.getsource(autotmux._run_remote_user_command)
        self.assertIn("command[:3] == ['tmux', 'attach', '-t']", source)
        self.assertIn("'tmux', 'attach-session', '-d', '-t'", source)


class GatewayHealthNoteTests(unittest.TestCase):
    """The subtitle used to say `1/4 healthy` whenever three gateways had
    simply never been needed, which reads as three broken ones."""

    def test_silent_when_nothing_has_failed(self):
        items = [{'name': 'k6', 'state': 'healthy'},
                 {'name': 'k7', 'state': 'unknown'},
                 {'name': 'b8', 'state': 'unknown'}]
        self.assertEqual(autotmux._gateway_health_note(items), '')

    def test_names_the_gateways_that_are_actually_failing(self):
        items = [{'name': 'k6', 'state': 'healthy'},
                 {'name': 'k7', 'state': 'backoff'},
                 {'name': 'b8', 'state': 'probing'}]
        note = autotmux._gateway_health_note(items)
        self.assertIn('b8', note)
        self.assertIn('k7', note)
        self.assertNotIn('k6', note)

    def test_shrugs_at_a_malformed_payload(self):
        for items in (None, 'nope', [None, 3, {'state': 'backoff'}]):
            self.assertNotIn('None', autotmux._gateway_health_note(items))


class MissingBinaryMessageTests(unittest.TestCase):
    def test_enoent_explains_itself(self):
        """`tmux: No such file or directory` reads as a missing *session*."""
        with mock.patch.object(
                autotmux.subprocess, 'call',
                side_effect=FileNotFoundError(2, 'No such file or directory')):
            code, error = autotmux._run_user_command(['/opt/bin/tmux', 'ls'])
        self.assertEqual(code, 127)
        self.assertIn('tmux is not on PATH', error)
        self.assertNotIn('No such file or directory', error)

    def test_other_errors_keep_their_own_words(self):
        with mock.patch.object(autotmux.subprocess, 'call',
                               side_effect=PermissionError(13, 'Permission denied')):
            code, error = autotmux._run_user_command(['/opt/bin/tmux'])
        self.assertEqual(code, 127)
        self.assertIn('Permission denied', error)


class IdleThresholdConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = (autotmux.model.IDLE_HINT_SECONDS, autotmux.model.IDLE_STALE_SECONDS)

    def tearDown(self):
        (autotmux.model.IDLE_HINT_SECONDS,
         autotmux.model.IDLE_STALE_SECONDS) = self._saved

    def _apply(self, body: str):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'config.toml')
            with open(path, 'w') as handle:
                handle.write(body)
            with mock.patch.object(autotmux.config, 'CONFIG_PATH', path):
                autotmux._apply_idle_thresholds()
        return autotmux.model.IDLE_HINT_SECONDS, autotmux.model.IDLE_STALE_SECONDS

    def test_thresholds_come_from_config(self):
        self.assertEqual(
            self._apply('[client]\nidle_hint = 60\nidle_stale = 120\n'),
            (60, 120))

    def test_a_stale_tier_below_the_hint_is_lifted(self):
        """Otherwise every flagged session would render in the red tier."""
        self.assertEqual(
            self._apply('[client]\nidle_hint = 600\nidle_stale = 60\n'),
            (600, 600))

    def test_out_of_range_values_fall_back(self):
        self.assertEqual(self._apply('[client]\nidle_hint = -5\n')[0], 300)

    def test_absent_config_keeps_the_defaults(self):
        self.assertEqual(self._apply('[client]\ngateways = ["a"]\n'),
                         (300, 3600))

    def test_a_broken_config_never_raises(self):
        self.assertEqual(self._apply('[client\nbroken'), (300, 3600))


class IdleRowTests(unittest.TestCase):
    @staticmethod
    def _state(session_entry):
        return {'nodes': {'gpu1': {
            'alive': True, 'socket': '/tmp/x', 'info': {}, 'last_error': '',
            'sessions': [session_entry]}}}

    def test_quiet_session_gets_a_dot_in_status(self):
        row = autotmux.build_session_rows(self._state(['train', '2', 900]))[0]
        self.assertEqual(row[4], '● 15m Active')

    def test_busy_session_status_is_unchanged(self):
        row = autotmux.build_session_rows(self._state(['train', '2', 30]))[0]
        self.assertEqual(row[4], 'Active')

    def test_the_dot_never_leaks_into_the_attach_target(self):
        """STATUS is decoration; row[1] is what Enter attaches to."""
        row = autotmux.build_session_rows(self._state(['train', '2', 9000]))[0]
        self.assertEqual(row[1], 'train')
        self.assertNotIn(autotmux._IDLE_DOT, row[1])

    def test_rows_stay_seven_wide_for_the_table(self):
        self.assertEqual(
            len(autotmux.build_session_rows(self._state(['t', '1', 900]))[0]), 7)

    def test_entries_without_idle_data_render_as_before(self):
        row = autotmux.build_session_rows(self._state(['train', '2']))[0]
        self.assertEqual((row[1], row[4]), ('train', 'Active'))

    def test_degraded_node_keeps_its_reason_alongside_the_dot(self):
        state = self._state(['train', '2', 900])
        state['nodes']['gpu1']['last_error'] = 'connect timeout'
        self.assertEqual(
            autotmux.build_session_rows(state)[0][4],
            '● 15m DEGRADED: connect timeout')


if __name__ == '__main__':
    unittest.main(verbosity=2)


class SqueueMessageTests(unittest.TestCase):
    """What the jobs panel says when it cannot run squeue.

    A gateway-mode client runs its daemon on a machine with no Slurm at all
    -- the queue comes from the login node a moment later. Reporting the
    missing binary as an error meant every cold open of the dashboard showed
    "[Errno 2] No such file or directory: 'squeue'" for a few seconds, which
    reads as a broken install and is not one.
    """

    def _with(self, error):
        from autotmux import daemon
        saved = daemon._hard_check_output

        def boom(*_args, **_kwargs):
            raise error

        daemon._hard_check_output = boom
        try:
            return daemon._get_squeue_text(['-l'])
        finally:
            daemon._hard_check_output = saved

    def test_no_slurm_here_says_what_it_means(self):
        text = self._with(FileNotFoundError(2, 'No such file or directory',
                                            'squeue'))
        self.assertNotIn('Errno', text)
        self.assertIn('no Slurm', text)
        self.assertIn('gateway', text)

    def test_a_real_failure_still_reports_itself(self):
        """The quiet message is only for the binary being absent. Anything
        else is a fault worth naming."""
        self.assertIn('boom', self._with(OSError('boom')))
        self.assertIn('error', self._with(OSError('boom')))

    def test_a_timeout_is_still_a_timeout(self):
        import subprocess
        self.assertIn('timed out',
                      self._with(subprocess.TimeoutExpired('squeue', 5)))
