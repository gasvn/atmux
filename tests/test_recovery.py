"""Tests for crash-recovery logic in autotmux.cli."""
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli


class ShouldRestartTests(unittest.TestCase):
    def test_allows_when_no_prior_attempts(self):
        self.assertTrue(cli._should_restart([], now=100.0))

    def test_allows_under_limit(self):
        self.assertTrue(cli._should_restart([99.0, 98.0], now=100.0))

    def test_blocks_at_limit_within_window(self):
        self.assertFalse(cli._should_restart([99.0, 98.0, 97.0], now=100.0))

    def test_old_attempts_outside_window_do_not_count(self):
        # three attempts but all older than the 60s window
        self.assertTrue(cli._should_restart([10.0, 20.0, 30.0], now=200.0))

    def test_mixed_window_counts_only_recent(self):
        # two recent (within 60s of now=100), one old → under limit of 3
        self.assertTrue(cli._should_restart([10.0, 70.0, 80.0], now=100.0))


class OffloadShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_offload_does_not_create_default_executor(self):
        import asyncio
        loop = asyncio.get_running_loop()
        self.assertIsNone(loop._default_executor)
        self.assertEqual(await cli._offload(lambda: 42), 42)
        self.assertIsNone(loop._default_executor,
                          'default executor would be joined during app exit')

    async def test_interactive_offload_has_a_hard_user_deadline(self):
        import asyncio
        import threading
        release = threading.Event()
        started = time.monotonic()
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await cli._offload_for(0.05, release.wait, 2)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            await asyncio.sleep(0.05)

    async def test_sync_subprocess_cleanup_has_a_second_hard_deadline(self):
        import asyncio
        import threading
        release = threading.Event()
        slots = threading.Semaphore(1)

        def stuck_run(*_args, **_kwargs):
            release.wait(2)
            return SimpleNamespace(returncode=0)

        try:
            with mock.patch.object(cli.subprocess, 'run', side_effect=stuck_run), \
                 mock.patch.object(cli, '_FRONTEND_COMMAND_CLEANUP_GRACE', 0.01):
                started = time.monotonic()
                with self.assertRaises(cli.subprocess.TimeoutExpired):
                    cli._hard_subprocess_run(
                        ['scontrol'], timeout=0.05, slots=slots)
                self.assertLess(time.monotonic() - started, 0.5)
                with self.assertRaises(cli._FrontendCommandCapacityExhausted):
                    cli._hard_subprocess_run(
                        ['scontrol'], timeout=0.05, slots=slots)
        finally:
            release.set()
            await asyncio.sleep(0.05)

    async def test_preview_cleanup_reaps_after_kill_race(self):
        class ExitedProcess:
            returncode = None

            def __init__(self):
                self.waited = False

            def kill(self):
                raise ProcessLookupError('already exited')

            async def wait(self):
                self.waited = True
                return 0

        proc = ExitedProcess()
        await cli.AutotmuxApp._stop_async_process(proc)
        self.assertTrue(proc.waited)


class InteractiveCommandTests(unittest.TestCase):
    def test_missing_binary_returns_clean_error_instead_of_raising(self):
        missing = FileNotFoundError(2, 'No such file or directory', 'tmux')
        with mock.patch.object(cli.subprocess, 'call', side_effect=missing):
            returncode, error = cli._run_user_command(['tmux', 'attach'])
        self.assertEqual(returncode, 127)
        self.assertIn('tmux', error)
        # Not the raw strerror: "tmux: No such file or directory" reads as a
        # missing session, when it is nearly always a PATH problem.
        self.assertIn('not on PATH', error)

    def test_nonzero_status_is_reported_after_terminal_redraw(self):
        app = cli.AutotmuxApp()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs))
        app._report_command_result('attach n:s', 255)
        self.assertEqual(len(notices), 1)
        self.assertIn('255', notices[0][0])
        self.assertEqual(notices[0][1]['severity'], 'warning')


class MaybeRecoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_running = cli._daemon_running
        self._saved_launch = cli._launch_daemon
        cli._daemon_running = lambda: False  # isolate from real system state
        cli._launch_daemon = lambda: (True, '')  # never spawn a real daemon in tests

    def tearDown(self):
        cli._daemon_running = self._saved_running
        cli._launch_daemon = self._saved_launch

    async def test_no_recovery_when_daemon_alive(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._crash_looping = True
            cli._daemon_running = lambda: True
            app._maybe_recover_daemon()
            self.assertEqual(calls, [])
            self.assertFalse(app._crash_looping)

    async def test_restart_dispatched_when_dead(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._crash_looping = False
            app._recovery_inflight = False
            cli._daemon_running = lambda: False
            app._maybe_recover_daemon()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(app._restart_attempts), 1)
            self.assertFalse(app._crash_looping)

    async def test_stops_after_loop_guard_and_sets_banner(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._crash_looping = False
            app._recovery_inflight = False
            cli._daemon_running = lambda: False
            for _ in range(5):
                app._maybe_recover_daemon()
                # Model a completed-but-unsuccessful start before the next
                # timer tick; in-flight ticks themselves are coalesced.
                app._recovery_inflight = False
            self.assertEqual(len(calls), 3)       # capped at limit=3
            self.assertTrue(app._crash_looping)

    async def test_refreshes_coalesce_while_daemon_start_is_inflight(self):
        app = cli.AutotmuxApp()
        async with app.run_test():
            calls = []
            app._dispatch_restart = lambda: calls.append(1)
            app._restart_attempts = []
            app._recovery_inflight = False
            for _ in range(6):
                app._maybe_recover_daemon()
            self.assertEqual(calls, [1])
            self.assertEqual(len(app._restart_attempts), 1)

    async def test_daemon_start_failure_is_shown_to_the_user(self):
        app = cli.AutotmuxApp()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs))
        cli._launch_daemon = lambda: (False, 'permission denied')
        app._recovery_inflight = True
        await app._restart_daemon_async()
        self.assertFalse(app._recovery_inflight)
        self.assertIn('permission denied', notices[0][0])
        self.assertEqual(notices[0][1]['severity'], 'error')
        self.assertFalse(notices[0][1]['markup'])


class LaunchDaemonTests(unittest.TestCase):
    def test_nonzero_start_returns_bounded_visible_error(self):
        result = SimpleNamespace(returncode=1, stdout='', stderr='bad lock\n')
        with mock.patch.object(cli, '_daemon_running', return_value=False), \
             mock.patch.object(cli.subprocess, 'run', return_value=result) as run:
            self.assertEqual(cli._launch_daemon(), (False, 'bad lock'))
        self.assertEqual(
            run.call_args.args[0],
            [cli.sys.executable, '-m', 'autotmux.daemon', 'start'])

    def test_zero_exit_waits_for_detached_child_readiness(self):
        result = SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(cli, '_daemon_running', return_value=False), \
             mock.patch.object(cli.subprocess, 'run', return_value=result), \
             mock.patch.object(
                 cli.lifecycle, 'active_runtime_base',
                 side_effect=[None, '/tmp/ready-autotmux']), \
             mock.patch.object(cli, '_sync_active_runtime_paths') as sync, \
             mock.patch.object(cli.time, 'sleep'):
            # The first post-detach probe has no metadata. Model the lock as
            # still held for the following loop rather than treating it as a
            # child crash.
            cli._daemon_running.side_effect = [False, True]
            self.assertEqual(cli._launch_daemon(), (True, ''))
        sync.assert_called_once_with()

    def test_runtime_paths_follow_live_daemon_metadata(self):
        old_enabled = cli._RUNTIME_DISCOVERY_ENABLED
        old_paths = (cli.STATE_FILE, cli.SNAPSHOT_FILE, cli.PREVIEW_SOCKET,
                     cli.WARM_DIR, cli.PID_FILE, cli.LOCK_FILE, cli.CTL_DIR)
        with unittest.mock.patch.object(
                cli.lifecycle, 'active_runtime_base',
                return_value='/tmp/active-autotmux'):
            try:
                cli._RUNTIME_DISCOVERY_ENABLED = True
                self.assertTrue(cli._sync_active_runtime_paths())
                self.assertEqual(
                    cli.STATE_FILE, '/tmp/active-autotmux/daemon.json')
                self.assertEqual(
                    cli.CTL_DIR, '/tmp/active-autotmux/ctl')
                self.assertEqual(
                    cli.PREVIEW_SOCKET, '/tmp/active-autotmux/preview.sock')
                self.assertEqual(
                    cli.WARM_DIR, '/tmp/active-autotmux/warm')
            finally:
                cli._RUNTIME_DISCOVERY_ENABLED = old_enabled
                (cli.STATE_FILE, cli.SNAPSHOT_FILE, cli.PREVIEW_SOCKET,
                 cli.WARM_DIR, cli.PID_FILE, cli.LOCK_FILE,
                 cli.CTL_DIR) = old_paths


class StatusSubtitleTests(unittest.TestCase):
    def _app(self):
        app = cli.AutotmuxApp()
        app._crash_looping = False
        return app

    def test_crash_loop_banner(self):
        app = self._app()
        app._crash_looping = True
        out = app._status_subtitle({'nodes': {'n': {}}}, [('n', 's')],
                                   '2024-01-01 00:00:00')
        self.assertIn('crash-looping', out)

    def test_no_nodes_shows_waiting(self):
        app = self._app()
        out = app._status_subtitle({}, [], '?')
        self.assertIn('waiting for daemon', out)

    def test_hung_but_alive_shows_stale_banner(self):
        app = self._app()
        # a very old 'updated' timestamp → age > 30s → stale banner
        out = app._status_subtitle({'nodes': {'n': {}}}, [('n', 's')],
                                   '2000-01-01 00:00:00')
        self.assertIn('stale', out)

    def test_monotonic_age_detects_stale_despite_future_wall_clock(self):
        app = self._app()
        state = {
            'nodes': {'n': {}},
            'updated_monotonic': time.monotonic() - 45,
        }
        out = app._status_subtitle(
            state, [('n', 's')], '2999-01-01 00:00:00')
        self.assertIn('stale', out)

    def test_fresh_update_shows_session_count(self):
        app = self._app()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        out = app._status_subtitle({'nodes': {'n': {}}}, [('n', 's')], now)
        self.assertIn('session', out)

    def test_missing_state_timestamp_is_visible(self):
        app = self._app()
        out = app._status_subtitle(
            {'nodes': {'n': {}}}, [('n', 's')], '?')
        self.assertIn('timestamp unavailable', out)

    def test_malformed_keepalive_state_is_ignored(self):
        app = self._app()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        out = app._status_subtitle(
            {'nodes': {'n': {}}, 'keepalive': ['not', 'a', 'mapping']},
            [('n', 's')], now)
        self.assertIn('1 session', out)
        self.assertEqual(app._ka_suffix('not-a-dict'), ' · ⟳ keep-alive')

    def test_paused_keepalive_suffix_includes_last_error(self):
        app = self._app()
        suffix = app._ka_suffix({
            'state': 'paused', 'attempts': 3, 'last_error': 'sbatch denied',
        })
        self.assertIn('PAUSED', suffix)
        self.assertIn('sbatch denied', suffix)

    def test_late_async_state_read_cannot_roll_the_table_backward(self):
        app = self._app()
        current = {'updated_monotonic': 200.0, 'nodes': {'new': {}}}
        older = {'updated_monotonic': 100.0, 'nodes': {'old': {}}}
        newer = {'updated_monotonic': 300.0, 'nodes': {'newer': {}}}
        self.assertTrue(app._state_is_older(older, current))
        self.assertFalse(app._state_is_older(newer, current))
        # Once the current schema has a monotonic revision, an older legacy
        # inode lacking it must not overwrite that state either.
        self.assertTrue(app._state_is_older(
            {'updated': '2099-01-01 00:00:00'}, current))

    def test_state_order_falls_back_to_wall_time_across_host_boots(self):
        app = self._app()
        current = {
            'monotonic_clock_id': 'host:new-boot',
            'updated_monotonic': 10.0,
            'updated': '2026-07-29 12:00:00',
        }
        old_boot_state = {
            'monotonic_clock_id': 'host:old-boot',
            'updated_monotonic': 999999.0,
            'updated': '2026-07-29 11:59:59',
        }
        next_boot_state = {
            'monotonic_clock_id': 'host:next-boot',
            'updated_monotonic': 1.0,
            'updated': '2026-07-29 12:00:01',
        }
        self.assertTrue(app._state_is_older(old_boot_state, current))
        self.assertFalse(app._state_is_older(next_boot_state, current))

    def test_foreign_boot_monotonic_timestamp_is_not_used_for_age(self):
        app = self._app()
        with mock.patch.object(cli, '_CLOCK_ID', 'host:this-boot'), \
             mock.patch.object(cli.time, 'monotonic', return_value=1000):
            age = app._daemon_age_seconds(
                '2000-01-01 00:00:00', 995, 'host:old-boot')
        self.assertGreater(age, 1000)

    def test_stalled_keepalive_worker_is_visible_even_while_daemon_is_fresh(self):
        app = self._app()
        app._ka_entries = [{'entry_id': 'x', 'job_id': '1',
                            'job_name': 'train', 'enabled': True}]
        now = time.monotonic()
        state = {
            'nodes': {'n': {}},
            'keepalive': {},
            'keepalive_health': {
                'enabled': True, 'in_progress': True,
                'last_attempt_monotonic': now - 200,
                'last_success_monotonic': now - 200,
                'last_error': '', 'interval': 30,
            },
        }
        out = app._status_subtitle(
            state, [('n', 's')], time.strftime('%Y-%m-%d %H:%M:%S'))
        self.assertIn('keep-alive check stalled', out)

    def test_disabled_keepalive_config_is_visible_when_intent_exists(self):
        app = self._app()
        app._ka_entries = [{'entry_id': 'x', 'job_id': '1',
                            'job_name': 'train', 'enabled': True}]
        state = {
            'nodes': {'n': {}}, 'keepalive': {},
            'keepalive_health': {'enabled': False},
        }
        out = app._status_subtitle(
            state, [('n', 's')], time.strftime('%Y-%m-%d %H:%M:%S'))
        self.assertIn('disabled in config', out)


class SnapshotShapeTests(unittest.TestCase):
    def test_non_string_snapshot_fields_are_ignored(self):
        app = cli.AutotmuxApp()
        app.snapshots = {'n:s': {'lines': ['not', 'text'], 'ts': []}}
        self.assertFalse(app._show_cached_snapshot('n', 's'))


class KeepaliveCacheTests(unittest.TestCase):
    def test_transient_registry_read_preserves_cache_and_retries(self):
        app = cli.AutotmuxApp()
        cached = ({'job_name': 'important', 'enabled': True},)
        app._ka_reg_cache = (('old',), cached)
        stat = SimpleNamespace(st_mtime_ns=1, st_mtime=0, st_size=2, st_ino=3)
        with mock.patch.object(cli.os, 'stat', return_value=stat), \
             mock.patch.object(cli.keepalive, '_load_registry_checked',
                               return_value=(False, [])):
            self.assertEqual(app._ka_registry_names(), {'important'})
        self.assertEqual(app._ka_reg_cache, (('old',), cached))

    def test_stuck_registry_read_is_single_flight(self):
        app = cli.AutotmuxApp()
        cached = ({'entry_id': 'one', 'job_id': '101',
                   'job_name': 'important', 'enabled': True},)
        app._ka_reg_cache = (('old',), cached)
        app._ka_registry_read_lock.acquire()
        try:
            self.assertEqual(app._ka_registry_entries(), [cached[0]])
            with self.assertRaisesRegex(RuntimeError, 'still in progress'):
                app._ka_registry_entries(require_fresh=True)
        finally:
            app._ka_registry_read_lock.release()


if __name__ == '__main__':
    unittest.main()
