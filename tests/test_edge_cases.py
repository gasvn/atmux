"""Edge cases and adversarial inputs that the happy-path tests miss."""
import os
import shlex
import sys
import threading
import time
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
        d._known_nodes_info.pop('ghost')
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
        d._known_nodes_info.pop('ghost')
        d._cleanup_gone_node('ghost')
        self.assertNotIn('ghost', d._master_procs,
                         "master proc handle leaked after node disappeared")
        self.assertTrue(proc.terminated or proc.killed,
                        "stale master process should be terminated")

    def test_unreapable_master_keeps_a_deferred_popen_owner(self):
        class StuckProc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                raise d.subprocess.TimeoutExpired(['ssh'], timeout)

        proc = StuckProc()
        d._master_procs['ghost'] = proc
        with patch.object(d.lifecycle, 'defer_popen_reap') as defer:
            d._kill_orphan_master_proc('ghost')
        defer.assert_called_once_with(proc)


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
             patch.object(d, '_restart_master', return_value=True) as restart:
            for _ in range(d.HEALTH_FAIL_THRESHOLD):
                d._health_check_node('n1')
            self.assertEqual(restart.call_count, 1)
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

    def test_success_in_ensure_loop_also_breaks_health_failure_streak(self):
        d._master_failure_streak['n1'] = d.HEALTH_FAIL_THRESHOLD - 1
        d._known_nodes_info['n1']['last_error'] = (
            'ControlMaster check failed; awaiting confirmation')
        with patch.object(d, '_master_alive', return_value=True):
            d._ensure_master('n1')
        self.assertNotIn('n1', d._master_failure_streak)
        self.assertNotIn('last_error', d._known_nodes_info['n1'])

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
        d._known_nodes_info.pop('ghost')
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


class DiscoveryFailureSafetyTests(unittest.TestCase):
    def setUp(self):
        d._known_nodes_info.clear()
        d._gone_node_streak.clear()
        d._known_nodes_info['localhost'] = {
            'time': '-', 'job_name': 'local', 'job_id': '-', 'state': 'LOCAL'}
        d._known_nodes_info['gpu1'] = {
            'time': '1:00', 'job_name': 'job', 'job_id': '1', 'state': 'RUNNING'}

    def tearDown(self):
        d._known_nodes_info.clear()
        d._gone_node_streak.clear()

    def test_incomplete_poll_never_ages_remote_node_out(self):
        local = {'localhost': {
            'time': '-', 'job_name': 'local', 'job_id': '-', 'state': 'LOCAL'}}
        for _ in range(10):
            gone, known = d._merge_discovery(dict(local), complete=False)
            self.assertEqual(gone, [])
            self.assertIn('gpu1', known)
        self.assertIn('gpu1', d._known_nodes_info)
        self.assertNotIn('gpu1', d._gone_node_streak)

    def test_only_complete_misses_can_remove_node(self):
        local = {'localhost': {
            'time': '-', 'job_name': 'local', 'job_id': '-', 'state': 'LOCAL'}}
        gone = []
        for _ in range(d.GONE_NODE_THRESHOLD):
            gone, _ = d._merge_discovery(dict(local), complete=True)
        self.assertIn('gpu1', gone)
        self.assertNotIn('gpu1', d._known_nodes_info)

    def test_squeue_exception_marks_discovery_incomplete(self):
        with patch.object(d.subprocess, 'check_output', side_effect=OSError('down')):
            nodes, complete = d._discover_nodes()
        self.assertFalse(complete)
        self.assertIn('localhost', nodes)

    def test_a_host_without_slurm_still_reports_its_own_tmux(self):
        """A workstation or VPS listed as its own cluster has no squeue, and
        that is the whole point of supporting it."""
        with patch.object(d.subprocess, 'check_output',
                          side_effect=FileNotFoundError('squeue')):
            nodes, complete = d._discover_nodes()
        self.assertIn('localhost', nodes)
        self.assertEqual(nodes['localhost']['state'], 'LOCAL')
        # Incomplete, so nothing ages a node toward destructive cleanup and
        # no job notices fire on a machine that has no jobs.
        self.assertFalse(complete)

    def test_a_missing_squeue_is_said_once_not_every_poll(self):
        """One poll per squeue_interval would write thousands of identical
        warnings a day and rotate real errors out of a 1 MB log."""
        saved = d._squeue_absent
        d._squeue_absent = False
        try:
            with self.assertLogs(d.log, level='INFO') as first:
                with patch.object(d.subprocess, 'check_output',
                                  side_effect=FileNotFoundError('squeue')):
                    d._discover_nodes()
            self.assertTrue(any('no squeue' in line for line in first.output))

            with patch.object(d.subprocess, 'check_output',
                              side_effect=FileNotFoundError('squeue')):
                with self.assertNoLogs(d.log, level='INFO'):
                    for _ in range(5):
                        d._discover_nodes()
        finally:
            d._squeue_absent = saved

    def test_pending_empty_node_field_does_not_corrupt_whole_poll(self):
        # %N is empty for a pending job, so the record begins with the unit
        # separator. str.strip() removes \x1f and used to shift every field.
        pending = (
            '\x1f1:00\x1fqueued\x1f2\x1fp\x1fu\x1fPENDING\x1f0:00'
            '\x1f1\x1f(Resources)\n'
        )
        running = (
            'gpu1\x1f2:00\x1ftrain\x1f1\x1fp\x1fu\x1fRUNNING\x1f0:10'
            '\x1f1\x1fgpu1\n'
        )
        with patch.object(d.subprocess, 'check_output',
                          return_value=pending + running):
            nodes, complete = d._discover_nodes()
        self.assertTrue(complete)
        self.assertIn('gpu1', nodes)
        self.assertNotIn('', nodes)


class MasterLifecycleRaceTests(unittest.TestCase):
    def setUp(self):
        d._known_nodes_info.clear()
        d._known_nodes_info['gpu1'] = {'job_id': '1'}
        d._master_backoff.clear()

    def tearDown(self):
        d._known_nodes_info.clear()
        d._master_backoff.clear()

    def test_start_rechecks_and_adopts_winner_inside_node_lock(self):
        with patch.object(d, '_master_alive', return_value=True), \
             patch.object(d, '_start_master_unsafe') as unsafe:
            self.assertTrue(d._start_master('gpu1'))
            unsafe.assert_not_called()

    def test_single_failed_check_of_existing_socket_is_nondestructive(self):
        with patch.object(d, '_master_alive', return_value=False), \
             patch.object(d.os.path, 'exists', return_value=True), \
             patch.object(d, '_start_master') as start:
            d._ensure_master('gpu1')
            start.assert_not_called()
        self.assertNotIn('gpu1', d._master_backoff)

    def test_reappearing_node_is_not_killed_by_late_cleanup(self):
        with patch.object(d, '_kill_master_unsafe') as kill:
            d._cleanup_gone_node('gpu1')
            kill.assert_not_called()


class BoundedWorkerTests(unittest.TestCase):
    def test_large_batch_never_creates_waiting_thread_per_task(self):
        sem = threading.Semaphore(2)
        release = threading.Event()
        started = []
        lock = threading.Lock()

        def block(i):
            with lock:
                started.append(i)
            release.wait(2)

        threads = d._start_bounded_batch(
            block, [(i,) for i in range(100)], sem, 'test-bounded',
            lambda a: f'test-{a[0]}')
        try:
            self.assertLessEqual(len(threads), 2)
            deadline = time.time() + 1
            while len(started) < len(threads) and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(started), len(threads))
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=1)


class RuntimeWatchdogTests(unittest.TestCase):
    def test_repeated_runtime_loss_releases_daemon_to_recover(self):
        class FakeEvent:
            stopped = False

            def wait(self, timeout=None):
                return self.stopped

            def set(self):
                self.stopped = True

        event = FakeEvent()
        with patch.object(d, '_stop_event', event), \
             patch.object(d.paths, 'ensure_runtime_dirs',
                          side_effect=FileNotFoundError('runtime removed')) as check:
            d._serve_until_stopped()
        self.assertTrue(event.stopped)
        self.assertEqual(check.call_count, 3)

    def test_invalid_orphan_filename_is_removed_without_invoking_ssh(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, 'cm_-ProxyCommand=bad')
            with open(bad, 'w'):
                pass
            with patch.object(d, 'CTL_DIR', td), \
                 patch.object(d, 'STAT_FILE', os.path.join(td, 'missing.json')), \
                 patch.object(d, '_master_alive') as alive:
                d._cleanup_orphan_sockets()
            alive.assert_not_called()
            self.assertFalse(os.path.exists(bad))

    def test_unresponsive_owned_orphan_is_deferred_not_killed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            socket = os.path.join(td, 'cm_gpu1')
            with open(socket, 'w'):
                pass
            with patch.object(d, 'CTL_DIR', td), \
                 patch.object(d, 'STAT_FILE', os.path.join(td, 'missing.json')), \
                 patch.object(d, '_master_alive', return_value=False), \
                 patch.object(d, '_ssh_master_pids', return_value=[123]):
                d._cleanup_orphan_sockets()
            self.assertTrue(os.path.exists(socket))

    def test_ownerless_orphan_socket_is_removed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            socket = os.path.join(td, 'cm_gpu1')
            with open(socket, 'w'):
                pass
            with patch.object(d, 'CTL_DIR', td), \
                 patch.object(d, 'STAT_FILE', os.path.join(td, 'missing.json')), \
                 patch.object(d, '_master_alive', return_value=False), \
                 patch.object(d, '_ssh_master_pids', return_value=[]):
                d._cleanup_orphan_sockets()
            self.assertFalse(os.path.exists(socket))


class BoundedStartupLoaderTests(unittest.TestCase):
    def test_config_and_nss_hangs_fall_back_with_one_deadline(self):
        release = threading.Event()

        def blocked_config():
            release.wait(1)
            return dict(d.config.DEFAULTS)

        def blocked_user(_uid):
            release.wait(1)
            return type('Pw', (), {'pw_name': 'eventual-user'})()

        started = time.monotonic()
        with patch.object(d.config, 'load', side_effect=blocked_config), \
             patch.object(d.config, 'load_keepalive',
                          return_value=dict(d.config.KEEPALIVE_DEFAULTS)), \
             patch.object(d.pwd, 'getpwuid', side_effect=blocked_user):
            try:
                self.assertFalse(d._load_runtime_configuration(timeout=0.05))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(d._cfg, d.config.DEFAULTS)
                self.assertEqual(d._USER, str(os.getuid()))
            finally:
                release.set()
                time.sleep(0.05)


class KeepaliveDiscoverySafetyTests(unittest.TestCase):
    def test_malformed_squeue_row_cannot_trigger_duplicate_submit(self):
        with patch.object(d, '_hard_check_output', return_value='partial row'):
            with self.assertRaises(ValueError):
                d._keepalive_job_rows()


class SnapshotFirstRunTests(unittest.TestCase):
    def test_snapshot_pairs_are_deduplicated_and_serialized_per_node(self):
        self.assertEqual(
            d._group_snapshot_pairs([
                ('n1', 'a'), ('n2', 'x'), ('n1', 'b'), ('n1', 'a'),
            ]),
            [('n1', ('a', 'b')), ('n2', ('x',))],
        )

    def test_missing_snapshot_file_is_created_on_first_capture(self):
        import json
        import tempfile
        previous = d.SNAPSHOT_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                d.SNAPSHOT_FILE = os.path.join(td, 'snapshots.json')
                captured = {
                    'n:s': {'lines': 'pane contents', 'ts': 'now'},
                }
                self.assertTrue(d._update_snapshot_cache([('n', 's')], captured))
                with open(d.SNAPSHOT_FILE) as f:
                    self.assertEqual(json.load(f), captured)
        finally:
            d.SNAPSHOT_FILE = previous


class StatusPublicationOrderingTests(unittest.TestCase):
    def test_second_writer_skips_instead_of_overtaking_first(self):
        d._status_write_lock.acquire()
        try:
            with patch.object(d, '_atomic_write_json') as write:
                self.assertFalse(d._write_status())
            write.assert_not_called()
        finally:
            d._status_write_lock.release()


class SessionPayloadTests(unittest.TestCase):
    def test_probe_script_contains_escaped_markers_not_embedded_nuls(self):
        script = d._session_probe_script()
        self.assertNotIn('\x00', script)
        self.assertIn(r'\000AUTOTMUX_SESSIONS\000', script)
        self.assertIn(r'\000AUTOTMUX_TMUXINFO\000', script)
        self.assertIn('escape-time 10', script)
        self.assertIn('_atmux_escape -gt 10', script)

    def test_noise_and_old_text_marker_cannot_create_phantom_sessions(self):
        # Lines are activity:windows:name, so the name is free to look like a
        # section marker without shifting any field.
        payload = (
            'remote profile banner\n'
            + d._SESSION_SECTION
            + '900:2:---NODEINFO---\n990:1:main\n'
            + d._NODEINFO_SECTION
            + '\n8\n0.50, 0.25, 0.10\n1000\n'
            + d._TMUXINFO_SECTION
            + '\n500\n'
        )
        sessions, nproc, load, escape_time, _gpu = d._parse_session_payload(
            payload)
        self.assertEqual(
            sessions, [['---NODEINFO---', '2', 100], ['main', '1', 10]])
        self.assertEqual(nproc, '8')
        self.assertEqual(load, '0.50')
        self.assertEqual(escape_time, '500')

    def test_truncated_payload_is_rejected_to_preserve_last_good_sessions(self):
        with self.assertRaises(ValueError):
            d._parse_session_payload('partial output without markers')


class GoneCleanupSchedulerTests(unittest.TestCase):
    def setUp(self):
        d._gone_cleanup_pending.clear()
        d._gone_cleanup_active.clear()

    def tearDown(self):
        d._gone_cleanup_pending.clear()
        d._gone_cleanup_active.clear()

    def test_capacity_excess_is_retained_without_serial_cleanup_delay(self):
        launched = []

        def fake_start(_target, _args, _sem, name):
            if len(launched) == 2:
                return None
            launched.append(name)
            return object()

        with patch.object(d, '_bounded_daemon_thread', side_effect=fake_start), \
             patch.object(d, '_cleanup_gone_node') as cleanup:
            started = d._schedule_gone_cleanup(['n1', 'n2', 'n3', 'n4'])
        self.assertEqual(started, 2)
        self.assertEqual(len(d._gone_cleanup_pending), 2)
        cleanup.assert_not_called()


class HardSubprocessDeadlineTests(unittest.TestCase):
    def test_hard_run_returns_even_when_timeout_cleanup_is_stuck(self):
        release = threading.Event()

        def stuck_run(*_args, **_kwargs):
            release.wait(2)
            return object()

        started = time.monotonic()
        try:
            with patch.object(d.subprocess, 'run', side_effect=stuck_run), \
                 patch.object(d, '_SUBPROCESS_CLEANUP_GRACE', 0.01):
                with self.assertRaises(d.subprocess.TimeoutExpired):
                    d._hard_run(['ssh', '-V'], timeout=0.1)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            time.sleep(0.05)


class ProbeCapacitySafetyTests(unittest.TestCase):
    def setUp(self):
        d._known_nodes_info.clear()
        d._master_failure_streak.clear()
        d._master_backoff.clear()

    def tearDown(self):
        d._known_nodes_info.clear()
        d._master_failure_streak.clear()
        d._master_backoff.clear()

    def test_unstarted_probe_cannot_age_a_live_socket_toward_restart(self):
        d._known_nodes_info['gpu1'] = {'job_id': '1'}
        with patch.object(d.os.path, 'exists', return_value=True), \
             patch.object(d, '_subprocess_slots', threading.Semaphore(0)):
            for _ in range(d.HEALTH_FAIL_THRESHOLD + 2):
                self.assertEqual(d._health_check_node('gpu1'), 'alive')
        self.assertNotIn('gpu1', d._master_failure_streak)


if __name__ == '__main__':
    unittest.main(verbosity=2)
