"""Edge cases and adversarial inputs that the happy-path tests miss."""
import os
import shlex
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import daemon as d


class GoneNodeCleanupTests(unittest.TestCase):
    """When a node disappears from squeue, all per-node state should be
    cleaned up — including the master proc handle and the backoff entry."""

    def setUp(self):
        d._known_nodes_info.clear()
        d._master_backoff.clear()
        d._master_procs.clear()

    def tearDown(self):
        d._known_nodes_info.clear()
        d._master_backoff.clear()
        d._master_procs.clear()

    def test_backoff_cleaned_when_node_leaves_squeue(self):
        # Simulate a node that's been failing for a while.
        d._known_nodes_info['ghost'] = {'time': '0:30'}
        d._backoff_record_failure('ghost')
        self.assertIn('ghost', d._master_backoff)
        # Now squeue no longer reports it. We expect _squeue_loop's
        # cleanup to drop the per-node bookkeeping.
        d._cleanup_gone_node('ghost')
        self.assertNotIn('ghost', d._master_backoff,
                         "backoff entry leaked after node disappeared")

    def test_master_proc_cleaned_when_node_leaves_squeue(self):
        class DummyProc:
            def __init__(self):
                self.terminated = False
                self.killed = False
                self.returncode = None
            def poll(self):
                return self.returncode
            def terminate(self):
                self.terminated = True
                self.returncode = 0
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.killed = True
                self.returncode = -9
        proc = DummyProc()
        d._known_nodes_info['ghost'] = {'time': '0:30'}
        d._master_procs['ghost'] = proc
        d._cleanup_gone_node('ghost')
        self.assertNotIn('ghost', d._master_procs,
                         "master proc handle leaked after node disappeared")
        self.assertTrue(proc.terminated or proc.killed,
                        "stale master process should be terminated")


class WriteStatusConcurrencyTests(unittest.TestCase):
    """_write_status must not race with _session_loop / _record_error
    mutating the per-node info dicts."""

    def setUp(self):
        d._known_nodes_info.clear()
        d._known_nodes_info['n1'] = {'time': '1:00', 'sessions': [['a', '1']]}
        d._known_nodes_info['n2'] = {'time': '2:00', 'sessions': []}

    def tearDown(self):
        d._known_nodes_info.clear()

    def test_write_status_survives_mutation_storm(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d.STAT_FILE = os.path.join(td, 'state.json')
            stop = threading.Event()

            def mutator():
                # Aggressively mutate inner dicts while _write_status reads.
                while not stop.is_set():
                    d._record_error('n1', 'flap')
                    d._record_error('n1', None)
                    with d._lock:
                        if 'n1' in d._known_nodes_info:
                            d._known_nodes_info['n1']['sessions'] = [['x', '1']]
                            d._known_nodes_info['n1']['sessions'] = []

            t = threading.Thread(target=mutator, daemon=True)
            t.start()
            try:
                # Each call must complete without RuntimeError.
                for _ in range(50):
                    d._write_status()
            finally:
                stop.set()
                t.join(timeout=2)
            # Final file must be valid JSON.
            with open(d.STAT_FILE) as f:
                json.load(f)


class ShellQuotingTests(unittest.TestCase):
    """Session names with spaces or special chars must not break the
    remote shell when we build the ssh command."""

    def test_quote_session_for_remote_shell(self):
        # Tmux mostly forbids spaces in names but defensive code should
        # handle it. shlex.quote is the standard helper.
        for sess in ['main', 'my session', "weird'name", 'a;rm -rf /', 'a$b']:
            quoted = shlex.quote(sess)
            # Round-trip through sh: split should give back the original
            # token list, no shell injection.
            tokens = shlex.split(quoted)
            self.assertEqual(tokens, [sess],
                             f"shlex.quote round-trip broke on: {sess!r}")


class HealthCheckStreakTests(unittest.TestCase):
    """Regression test for the "kicked out of tmux" bug.

    A single failed deep probe must NOT kill the master — otherwise an
    interactive ssh slave channel running in parallel (the user's
    `tmux attach`) gets killed when the remote node has a brief CPU
    spike. Killing should only happen after several consecutive
    failures, indicating a genuinely dead master.
    """
    def setUp(self):
        d._master_failure_streak.clear()
        d._master_backoff.clear()
        d._known_nodes_info.clear()
        d._known_nodes_info['n1'] = {'time': '1:00'}

    def tearDown(self):
        d._master_failure_streak.clear()
        d._master_backoff.clear()
        d._known_nodes_info.clear()

    def test_single_probe_failure_does_not_kill_master(self):
        with patch.object(d, '_master_alive', return_value=False), \
             patch.object(d, '_kill_master') as kill, \
             patch.object(d, '_start_master', return_value=True):
            d._health_check_node('n1')
            self.assertEqual(kill.call_count, 0,
                             "single probe failure must not kill master "
                             "(would yank user's interactive ssh)")
            self.assertEqual(d._master_failure_streak['n1'], 1)

    def test_threshold_failures_trigger_restart(self):
        with patch.object(d, '_master_alive', return_value=False), \
             patch.object(d, '_kill_master') as kill, \
             patch.object(d, '_start_master', return_value=True) as start:
            for _ in range(d.HEALTH_FAIL_THRESHOLD):
                d._health_check_node('n1')
            self.assertEqual(kill.call_count, 1)
            self.assertEqual(start.call_count, 1)
            self.assertNotIn('n1', d._master_failure_streak,
                             "streak should reset after acting on it")

    def test_success_clears_streak(self):
        # Two failures, then a success — streak goes back to zero, no kill.
        alive_returns = [False, False, True]
        with patch.object(d, '_master_alive',
                          side_effect=lambda *a, **kw: alive_returns.pop(0)), \
             patch.object(d, '_kill_master') as kill, \
             patch.object(d, '_start_master'):
            for _ in range(3):
                d._health_check_node('n1')
            self.assertEqual(kill.call_count, 0,
                             "transient failures recovering should not kill")
            self.assertNotIn('n1', d._master_failure_streak)

    def test_localhost_is_skipped(self):
        d._known_nodes_info['localhost'] = {'time': '-'}
        with patch.object(d, '_master_alive', return_value=False) as alive:
            result = d._health_check_node('localhost')
            self.assertEqual(result, 'skip-localhost')
            self.assertEqual(alive.call_count, 0)


class GoneNodeStreakTests(unittest.TestCase):
    """When a node leaves squeue, _master_failure_streak[node] must be
    popped along with backoff and master proc — otherwise it leaks."""
    def setUp(self):
        d._known_nodes_info.clear()
        d._master_backoff.clear()
        d._master_procs.clear()
        d._master_failure_streak.clear()

    def tearDown(self):
        d._known_nodes_info.clear()
        d._master_backoff.clear()
        d._master_procs.clear()
        d._master_failure_streak.clear()

    def test_streak_cleared_on_gone_node(self):
        d._known_nodes_info['ghost'] = {'time': '0:30'}
        d._master_failure_streak['ghost'] = 2
        d._cleanup_gone_node('ghost')
        self.assertNotIn('ghost', d._master_failure_streak,
                         'streak should be popped when node leaves squeue')


class HealthCheckSkipsGoneNode(unittest.TestCase):
    """_health_check_node must not act on nodes that have left squeue
    between _health_loop's snapshot and the per-node check — otherwise
    we waste a deep probe and may even spawn a master for a dead node."""
    def setUp(self):
        d._known_nodes_info.clear()
        d._master_failure_streak.clear()
        d._master_backoff.clear()

    def tearDown(self):
        d._known_nodes_info.clear()
        d._master_failure_streak.clear()
        d._master_backoff.clear()

    def test_returns_gone_when_not_in_known_nodes(self):
        # node is not in _known_nodes_info — simulating a node that left
        # squeue between _health_loop's snapshot and this call.
        result = d._health_check_node('vanished-node')
        self.assertEqual(result, 'gone')


class StartMasterDoesNotPgrepKill(unittest.TestCase):
    """_start_master_unsafe must NOT pgrep-kill ssh processes for the
    node — pgrep-killing from a 'spawn' code path is a footgun that
    yanks live masters when shallow check has a transient false positive
    on a busy login node, in turn kicking out interactive ssh users."""
    def setUp(self):
        d._known_nodes_info.clear()
        d._known_nodes_info['gpu1'] = {'time': '1:00'}

    def tearDown(self):
        d._known_nodes_info.clear()

    def test_pgrep_kill_NOT_called_in_start(self):
        from unittest.mock import patch, MagicMock
        with patch.object(d, '_kill_orphan_master_proc') as kill_proc, \
             patch.object(d, '_kill_ssh_with_ctl_path', return_value=0) as pgrep_kill, \
             patch.object(d, 'subprocess') as sp_mod, \
             patch.object(d.os.path, 'exists', return_value=True), \
             patch.object(d.os, 'unlink'):
            sp_mod.Popen.return_value = MagicMock()
            d._start_master_unsafe('gpu1')
            # Tracked-Popen cleanup is fine — kills only what WE spawned.
            kill_proc.assert_called_with('gpu1')
            # But pgrep-kill is forbidden here because it can yank a
            # mistakenly-flagged-dead-but-actually-live master.
            pgrep_kill.assert_not_called()

    def test_pgrep_kill_IS_called_in_kill(self):
        """As a sanity check: _kill_master_unsafe (the explicit
        teardown path) DOES still pgrep-kill, because in that path we
        actually want the master process gone."""
        from unittest.mock import patch
        with patch.object(d, '_kill_orphan_master_proc'), \
             patch.object(d, '_kill_ssh_with_ctl_path', return_value=0) as pgrep_kill, \
             patch.object(d, '_master_pid', return_value=None), \
             patch.object(d.subprocess, 'run'), \
             patch.object(d.os.path, 'exists', return_value=True), \
             patch.object(d.os, 'unlink'):
            d._kill_master_unsafe('gpu1')
            pgrep_kill.assert_called_with('gpu1')


class MasterKeepaliveTests(unittest.TestCase):
    """The ssh master must use ServerAliveInterval — otherwise an idle
    plain-bash session (no tmux activity to keep TCP busy) gets dropped
    by NAT/firewall after a few minutes and the user is silently kicked."""

    def test_start_master_argv_includes_server_alive_interval(self):
        from unittest.mock import patch, MagicMock
        d._known_nodes_info.clear()
        d._known_nodes_info['gpu1'] = {'time': '1:00'}
        captured_argv = []
        try:
            with patch.object(d, '_kill_orphan_master_proc'), \
                 patch.object(d, '_kill_ssh_with_ctl_path', return_value=0), \
                 patch.object(d.os.path, 'exists', return_value=True), \
                 patch.object(d.os, 'unlink'), \
                 patch.object(d.subprocess, 'Popen') as popen:
                popen.return_value = MagicMock()
                d._start_master_unsafe('gpu1')
                captured_argv = popen.call_args[0][0]
        finally:
            d._known_nodes_info.clear()
        joined = ' '.join(captured_argv)
        self.assertIn('ServerAliveInterval', joined,
                      f'master spawn must enable keepalive; got: {captured_argv}')


class SqueueTransientGraceTests(unittest.TestCase):
    """A single squeue cycle missing a node must NOT immediately trigger
    _cleanup_gone_node (which kills the master, dropping the user's
    interactive session). Require N consecutive misses first."""

    def setUp(self):
        d._known_nodes_info.clear()
        d._gone_node_streak = {}

    def tearDown(self):
        d._known_nodes_info.clear()
        if hasattr(d, '_gone_node_streak'):
            d._gone_node_streak.clear()

    def test_first_miss_does_not_clean_up(self):
        # Helper exists and is non-destructive on first miss.
        self.assertFalse(d._mark_node_missing('gpu1'),
                         'first missing cycle must not trigger cleanup')

    def test_second_miss_triggers_cleanup(self):
        d._mark_node_missing('gpu1')
        # Threshold is GONE_NODE_THRESHOLD; with default 2, the second
        # consecutive miss should return True.
        self.assertTrue(d._mark_node_missing('gpu1'),
                        'second consecutive miss must trigger cleanup')

    def test_seeing_node_again_resets_streak(self):
        d._mark_node_missing('gpu1')
        d._mark_node_seen('gpu1')
        self.assertFalse(d._mark_node_missing('gpu1'),
                         'streak should reset after the node reappears')


if __name__ == '__main__':
    unittest.main(verbosity=2)
