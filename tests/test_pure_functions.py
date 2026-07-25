"""Pure-function tests for autotmux + autotmux_daemon.

Covers what we can test without a running daemon or terminal: file I/O
helpers, state-shape building, and the small bookkeeping functions.
Run with `python -m unittest tests/test_pure_functions.py`.
"""
import json
import os
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


class BuildSessionRowsTests(unittest.TestCase):
    def test_empty_state(self):
        self.assertEqual(autotmux.build_session_rows({}), [])

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
        self.assertEqual(rows[0][1], '<offline>')
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

    def test_alive_node_no_sessions_shows_start_shell(self):
        state = {'nodes': {'h1': {'alive': True, 'info': {}, 'sessions': []}}}
        rows = autotmux.build_session_rows(state)
        self.assertEqual(rows[0][1], '<Start Shell>')

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
        class NotJSON: pass
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
        fd = getattr(d, '_singleton_lock_fd', None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            d._singleton_lock_fd = None

    def test_second_acquire_fails_while_held(self):
        with tempfile.TemporaryDirectory() as td:
            lockfile = os.path.join(td, 'daemon.pid.lock')
            with mock.patch.object(d, 'LOCK_FILE', lockfile):
                self.assertTrue(d._acquire_singleton_lock(),
                                "first acquire should succeed")
                self.assertFalse(d._acquire_singleton_lock(),
                                 "second acquire must fail while the lock is held")


class DaemonBookkeepingTests(unittest.TestCase):
    def test_master_alive_localhost_always_true(self):
        self.assertTrue(d._master_alive('localhost'))
        self.assertTrue(d._master_alive('localhost', deep=True))

    def test_get_nodes_always_includes_localhost(self):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
