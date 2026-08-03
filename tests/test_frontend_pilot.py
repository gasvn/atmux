"""Headless tests of the AutotmuxApp via Textual's Pilot framework.

These don't need a running daemon — they point STATE_FILE at a temp
file with synthetic content and exercise the UI logic directly.
"""
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux

# Synthetic state with a known shape — two alive nodes, one offline.
SYNTH_STATE = {
    'pid': 1234,
    'user': 'tester',
    'updated': '2026-01-01 00:00:00',
    'squeue_long': 'JOBID NAME\n1 t1\n2 t2',
    'squeue_pending': 'JOBID NAME\n3 pending',
    'squeue_updated': '2026-01-01 00:00:00',
    'nodes': {
        'localhost': {
            'alive': True,
            'socket': '',
            'info': {'time': '-', 'state': 'LOCAL'},
            'sessions': [['main', '2']],
            'last_error': '',
        },
        'gpu1': {
            'alive': True,
            'socket': '/tmp/whatever',
            'info': {'time': '1-00:00:00', 'state': 'RUNNING'},
            'sessions': [['train', '1'], ['debug', '1']],
            'last_error': '',
        },
        'gpu2': {
            'alive': False,
            'socket': '/tmp/whatever2',
            'info': {'time': '0:30:00', 'state': 'RUNNING'},
            'sessions': [],
            'last_error': 'connect timeout',
        },
    }
}


def _setup_state(tmpdir):
    state_path = os.path.join(tmpdir, 'state.json')
    snap_path = os.path.join(tmpdir, 'snap.json')
    with open(state_path, 'w') as f:
        json.dump(SYNTH_STATE, f)
    with open(snap_path, 'w') as f:
        json.dump({}, f)
    autotmux.STATE_FILE = state_path
    autotmux.SNAPSHOT_FILE = snap_path
    return state_path, snap_path


class MouseMotionTrackingTests(unittest.TestCase):
    """Any-motion mouse tracking (1003h) reports an escape sequence per mouse
    move, which floods a slow/remote (SSH) terminal's input and buries
    keystrokes — arrow keys feel dead. atmux needs only clicks, so the driver
    must NOT enable 1003h."""

    def test_driver_omits_any_motion_mouse_tracking(self):
        app = autotmux.AutotmuxApp()
        cls = app.get_driver_class()
        writes = []
        inst = cls.__new__(cls)          # bypass __init__ (needs a real app/tty)
        inst._mouse = True
        inst.write = lambda s: writes.append(s)
        inst.flush = lambda: None
        inst._enable_mouse_support()
        joined = ''.join(writes)
        self.assertIn('\x1b[?1000h', joined, "click tracking should stay enabled")
        self.assertNotIn('\x1b[?1003h', joined,
                         "any-motion mouse tracking must be disabled (it floods SSH input)")


class FrontendPilotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')
        self._saved_prewarm = autotmux.AutotmuxApp._prewarm_interactive_async

        async def no_prewarm(_app, _nodes, _source_pool):
            return None

        autotmux.AutotmuxApp._prewarm_interactive_async = no_prewarm

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch
        autotmux.AutotmuxApp._prewarm_interactive_async = self._saved_prewarm

    async def test_app_starts_and_renders_table(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Three nodes worth of rows: one shell (localhost), two
                # named sessions (gpu1), one offline placeholder (gpu2).
                self.assertEqual(len(app.all_sessions), 4)
                node_names = {r[0] for r in app.all_sessions}
                self.assertEqual(node_names, {'localhost', 'gpu1', 'gpu2'})

    async def test_small_terminal_preserves_session_space_and_full_height_table(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                upper = app.query_one('#upper').region
                table = app.query_one('#left_pane').region
                jobs = app.query_one('#jobs_panel').region
                self.assertEqual(table.height, upper.height)
                self.assertGreaterEqual(upper.height, 12)
                self.assertLessEqual(jobs.height, 8)

    async def test_arrow_keys_change_selection(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                first = (app.selected_node, app.selected_session)
                await pilot.press("down")
                await pilot.pause()
                second = (app.selected_node, app.selected_session)
                self.assertNotEqual(first, second,
                                    "down arrow should move the selection")

    async def test_j_toggles_jobs_view_mode(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.jobs_view_mode, 'long')
                await pilot.press("j")
                await pilot.pause()
                self.assertEqual(app.jobs_view_mode, 'pending')
                await pilot.press("j")
                await pilot.pause()
                self.assertEqual(app.jobs_view_mode, 'long')

    async def test_j_uses_cached_state_without_disk_read(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                with mock.patch.object(
                        autotmux, 'read_state',
                        side_effect=AssertionError('event-loop disk read')):
                    await app.action_toggle_jobs_view()
                self.assertEqual(app.jobs_view_mode, 'pending')

    async def test_connection_manager_discovers_and_saves_ssh_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            settings = dict(autotmux.config.CLIENT_DEFAULTS)
            settings['gateways'] = []
            saved = (dict(settings, gateways=['login2']), None)
            with mock.patch.object(
                    autotmux, '_load_client_config_bounded',
                    return_value=(True, settings)), \
                 mock.patch.object(
                    autotmux.config, 'discover_ssh_aliases',
                    return_value=['login1', 'login2']), \
                 mock.patch.object(
                    app, '_persist_connection_choice',
                    return_value=saved) as persist, \
                 mock.patch.object(app, '_dispatch_warm'), \
                 mock.patch.object(
                    autotmux, '_RUNTIME_DISCOVERY_ENABLED', False), \
                 mock.patch.object(autotmux, '_GATEWAY_POOL', None), \
                 mock.patch.object(
                    autotmux, '_sync_active_runtime_paths', return_value=False):
                async with app.run_test() as pilot:
                    main_screen = app.screen
                    await app.action_manage_connections()
                    await pilot.pause()
                    dialog = app.screen
                    self.assertIsInstance(dialog, autotmux.ConnectionManager)
                    choices = dialog.query_one(
                        '#connection_aliases', autotmux.SelectionList)
                    choices.select('login2')
                    dialog.action_save()
                    await pilot.pause()
                    self.assertIs(app.screen, main_screen)
            result = persist.call_args.args[0]
            self.assertEqual(result['mode'], 'gateway')
            self.assertEqual(result['gateways'], ['login2'])

    async def test_first_run_setup_does_not_start_local_daemon_behind_dialog(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp(offer_connection_setup=True)
            settings = dict(autotmux.config.CLIENT_DEFAULTS)
            with mock.patch.object(
                    autotmux, '_load_client_config_bounded',
                    return_value=(True, settings)), \
                 mock.patch.object(
                    autotmux.config, 'discover_ssh_aliases', return_value=[]), \
                 mock.patch.object(autotmux, '_daemon_running', return_value=False), \
                 mock.patch.object(
                    autotmux, '_launch_daemon',
                    side_effect=AssertionError('setup must decide deployment first')):
                async with app.run_test(size=(60, 20)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(
                        app.screen, autotmux.ConnectionManager)
                    save = app.screen.query_one('#connection_save')
                    self.assertLessEqual(save.region.right, 60)
                    self.assertLessEqual(save.region.bottom, 20)

    async def test_jobs_panel_marks_stale_squeue_data(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                state = dict(SYNTH_STATE)
                state['squeue_updated_monotonic'] = time.monotonic() - 100
                app._refresh_jobs(state)
                self.assertIn('⚠ stale', str(app.jobs_view.render()))

    async def test_manual_refresh_reloads_snapshots_off_loop(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                called = []

                async def reload_async():
                    called.append(True)

                app._reload_snapshots_async = reload_async
                app._reload_snapshots = lambda: self.fail('synchronous snapshot read')
                await app.action_refresh_table()
                self.assertEqual(called, [True])

    async def test_offline_row_skipped_for_attach(self):
        """Pressing Enter on an <offline> row should not attempt to spawn ssh."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Force selection onto the offline gpu2 row.
                app.selected_node = 'gpu2'
                app.selected_session = autotmux._OFFLINE_SESSION
                with mock.patch.object(app, 'notify') as notify:
                    await app.action_attach_session()
                notify.assert_called_once()
                self.assertIn('offline', notify.call_args.args[0])
                self.assertFalse(notify.call_args.kwargs['markup'])

    async def test_start_shell_row_uses_native_remote_transport(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'gpu1'
                app.selected_session = autotmux._START_SHELL_SESSION
                with mock.patch.object(app, 'suspend', return_value=nullcontext()), \
                        mock.patch.object(
                            app._warm_pool, 'shell') as warm_shell, \
                        mock.patch.object(
                            autotmux, '_run_remote_user_command',
                            return_value=(0, '', False)) as native:
                    await app.action_attach_session()
                warm_shell.assert_not_called()
                native.assert_called_once_with('gpu1', None, direct=False)

    async def test_open_shell_action_uses_native_remote_transport(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'gpu1'
                with mock.patch.object(app, 'suspend', return_value=nullcontext()), \
                        mock.patch.object(
                            app._warm_pool, 'shell') as warm_shell, \
                        mock.patch.object(
                            autotmux, '_run_remote_user_command',
                            return_value=(0, '', False)) as native:
                    await app.action_open_shell()
                warm_shell.assert_not_called()
                native.assert_called_once_with('gpu1', None, direct=False)

    async def test_single_click_on_row_triggers_attach(self):
        """A single mouse click on a session row must attach to THAT row.

        Regression: Textual's DataTable only emits RowSelected on a
        *redundant* click of the cell already under the cursor, so a first
        click on a different row merely highlighted it and the attach never
        ran ("click does nothing"). A single click on any row must attach.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                attached = []

                async def fake_attach():
                    attached.append((app.selected_node, app.selected_session))

                app.action_attach_session = fake_attach
                # Cursor starts on row 0; click a *different* row (index 1).
                # Header occupies y=0, so data row 1 is at y=2.
                await pilot.click(app.table, offset=(3, 2))
                await pilot.pause()

                self.assertEqual(len(attached), 1,
                                 "a single click on a row should attach exactly once")
                node, sess = attached[0]
                self.assertIn((node, sess),
                              [(r[0], r[1]) for r in app.all_sessions])

    async def test_enter_key_triggers_attach(self):
        """Pressing Enter on the highlighted row must still attach (the
        click fix also routes through on_data_table_row_selected)."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                attached = []

                async def fake_attach():
                    attached.append((app.selected_node, app.selected_session))

                app.action_attach_session = fake_attach
                app.table.focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(len(attached), 1,
                                 "Enter on a row should attach exactly once")

    async def test_stale_row_selected_event_never_attaches_current_row(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                attached = []

                async def fake_attach():
                    attached.append((app.selected_node, app.selected_session))

                app.action_attach_session = fake_attach
                await app.on_data_table_row_selected(
                    SimpleNamespace(row_key=object()))
                self.assertEqual(attached, [])

    async def test_empty_refresh_clears_stale_selection_and_preview(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(app.selected_node)
                app.log_view.update('old preview')
                app._refresh_table({'updated': 'now', 'nodes': {}})
                self.assertEqual(app.all_sessions, [])
                self.assertEqual(app.selected_node, '')
                self.assertEqual(app.selected_session, '')
                self.assertEqual(str(app.log_view.render()), '')

    async def test_vanished_selected_session_replaces_its_old_preview(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Sorted row 0 is gpu1:debug. Give the replacement row a cache
                # so the expected repaint is deterministic and immediate.
                app.selected_node = 'gpu1'
                app.selected_session = 'debug'
                app.snapshots = {
                    'gpu1:train': {'lines': 'new target preview', 'ts': 'now'}
                }
                app.log_view.update('preview from vanished debug session')
                state = json.loads(json.dumps(SYNTH_STATE))
                state['nodes']['gpu1']['sessions'] = [['train', '1']]
                app._refresh_table(state)
                self.assertEqual(
                    (app.selected_node, app.selected_session), ('gpu1', 'train'))
                rendered = str(app.log_view.render())
                self.assertIn('new target preview', rendered)
                self.assertNotIn('vanished debug', rendered)

    async def test_transient_state_read_keeps_last_good_rows(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                before = list(app.all_sessions)
                selected = (app.selected_node, app.selected_session)
                with mock.patch.object(
                        autotmux, '_read_state_checked', return_value=(False, {})):
                    await app._refresh_async()
                self.assertEqual(app.all_sessions, before)
                self.assertEqual(
                    (app.selected_node, app.selected_session), selected)

    async def test_transient_snapshot_read_keeps_last_good_cache(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.snapshots = {'gpu1:train': {'lines': 'valuable', 'ts': '1'}}
                with mock.patch.object(
                        autotmux, '_read_snapshots_checked', return_value=(False, {})):
                    await app._reload_snapshots_async()
                self.assertIn('gpu1:train', app.snapshots)

    async def test_load_change_updates_cell_in_place(self):
        """When only a volatile field (load average) changes, the table is
        updated in place — the LOAD cell reflects the new value and no full
        structural rebuild happens (keeps the 5s tick smooth)."""
        from textual.coordinate import Coordinate
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                sig_before = app._last_structural_sig
                new_state = json.loads(json.dumps(SYNTH_STATE))
                new_state['nodes']['gpu1']['info']['load'] = '7.77'
                app._refresh_table(new_state)
                await pilot.pause()
                # LOAD is display column index 5; row 0 is a gpu1 row.
                self.assertEqual(str(app.table.get_cell_at(Coordinate(0, 5))), '7.77')
                self.assertEqual(app._last_structural_sig, sig_before,
                                 "load-only change must not trigger a structural rebuild")

    async def test_dynamic_table_cells_are_literal_not_rich_markup(self):
        from textual.coordinate import Coordinate
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                state = {
                    'updated': 'now',
                    'nodes': {
                        'localhost': {
                            'alive': True,
                            'info': {},
                            'sessions': [['[bold]literal[/bold]', '1']],
                            'last_error': '',
                        },
                    },
                }
                app._refresh_table(state)
                cell = app.table.get_cell_at(Coordinate(0, 1))
                self.assertIsInstance(cell, autotmux.rich.text.Text)
                self.assertEqual(cell.plain, '[bold]literal[/bold]')

    async def test_preview_pane_not_focusable_so_arrows_always_move_table(self):
        """The preview pane must stay out of the focus chain. Otherwise a
        stray click/Tab moves focus there and up/down scroll the preview
        instead of moving the session cursor ("can't move up/down")."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                chain = [w.id for w in app.screen.focus_chain]
                self.assertEqual(chain, ['left_pane'],
                                 "only the DataTable should be focusable")
                # Even after trying to focus the preview pane, arrows move the table.
                app.set_focus(app.query_one("#right_pane_scroll"))
                await pilot.pause()
                before = app.selected_session
                await pilot.press("down")
                await pilot.pause()
                self.assertNotEqual(app.selected_session, before,
                                    "down must still move the table cursor")

    async def test_preview_capture_uses_daemon_ipc_and_never_spawns_ssh(self):
        """The TUI must not own network subprocesses or terminal stdin."""
        app = autotmux.AutotmuxApp()
        response = {'ok': True, 'content': 'pane output'}
        with mock.patch.object(autotmux.ipc, 'request',
                               return_value=response) as request, \
             mock.patch.object(autotmux.asyncio,
                               'create_subprocess_exec') as spawn:
            actual = await app._spawn_preview_capture('gpu1', 'train')

        self.assertEqual(actual, response)
        request.assert_called_once()
        path, payload, timeout = request.call_args.args
        self.assertEqual(path, autotmux.PREVIEW_SOCKET)
        self.assertEqual(payload, {
            'action': 'preview', 'node': 'gpu1', 'session': 'train'})
        self.assertGreater(timeout, 0)
        spawn.assert_not_called()

    async def test_preview_backoff_replaces_stuck_loading_message(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'gpu1'
                app.selected_session = 'train'
                app.snapshots = {}
                app.log_view.update('Loading preview…')
                with mock.patch.object(app, 'notify') as notify:
                    app._report_preview_backoff(
                        'gpu1', 'train', 'preview command timed out')
                self.assertIn('Retrying in 60s', str(app.log_view.render()))
                notify.assert_called_once()
                self.assertFalse(notify.call_args.kwargs['markup'])

    async def test_new_window_passes_one_quoted_shell_command_to_tmux(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'localhost'
                app.selected_session = 'main'
                with mock.patch.dict(os.environ, {'TMUX': '/tmp/tmux,1,0'}), \
                        mock.patch.object(autotmux, '_tmux', return_value=True) as run:
                    await app.action_new_window()
                run.assert_called_once_with(
                    'new-window', '-n', 'localhost-main',
                    shlex.join([
                        sys.executable, '-m', 'autotmux.cli', '--attach',
                        'localhost:main',
                    ]))

    async def test_remote_tmux_new_window_uses_passthrough_attach_helper(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'gpu1'
                app.selected_session = 'train'
                with mock.patch.dict(os.environ, {'TMUX': '/tmp/tmux,1,0'}), \
                        mock.patch.object(autotmux, '_tmux', return_value=True) as run:
                    await app.action_new_window()
                run.assert_called_once_with(
                    'new-window', '-n', 'gpu1-train',
                    shlex.join([
                        sys.executable, '-m', 'autotmux.cli', '--attach',
                        'gpu1:train',
                    ]))

    async def test_new_shell_window_falls_back_when_shell_env_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'localhost'
                app.selected_session = autotmux._START_SHELL_SESSION
                with mock.patch.dict(
                        os.environ, {'TMUX': '/tmp/tmux,1,0', 'SHELL': ''}), \
                        mock.patch.object(autotmux, '_tmux', return_value=True) as run:
                    await app.action_new_window()
                run.assert_called_once_with(
                    'new-window', '-n', 'localhost-shell', '/bin/bash')

    async def test_remote_shell_window_uses_resilient_helper(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.selected_node = 'gpu1'
                app.selected_session = autotmux._START_SHELL_SESSION
                with mock.patch.dict(os.environ, {'TMUX': '/tmp/tmux,1,0'}), \
                        mock.patch.object(autotmux, '_tmux',
                                          return_value=True) as run:
                    await app.action_new_window()
                run.assert_called_once_with(
                    'new-window', '-n', 'gpu1-shell',
                    shlex.join([
                        sys.executable, '-m', 'autotmux.cli', '--shell',
                        'gpu1',
                    ]))

    async def test_missing_state_file_doesnt_crash(self):
        with tempfile.TemporaryDirectory() as td:
            autotmux.STATE_FILE = os.path.join(td, 'missing.json')
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'missing_snap.json')
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # No rows but the app itself should still be alive.
                self.assertEqual(app.all_sessions, [])
                self.assertIn('waiting for daemon', str(app.sub_title))

    async def test_malformed_state_file_doesnt_crash(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, 'state.json')
            snap_path = os.path.join(td, 'snap.json')
            with open(state_path, 'w') as f:
                f.write('not json {{{ ')
            with open(snap_path, 'w') as f:
                f.write('{}')
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = snap_path
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.all_sessions, [])

    async def test_legacy_snapshot_format_is_ignored(self):
        """v0.3.x stored snapshot values as lists; current readers must
        not crash on them."""
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, 'state.json')
            snap_path = os.path.join(td, 'snap.json')
            with open(state_path, 'w') as f:
                json.dump(SYNTH_STATE, f)
            with open(snap_path, 'w') as f:
                json.dump({'localhost:main': ['legacy', 'list', 'format']}, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = snap_path
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # _show_cached_snapshot should refuse to render a list.
                ok = app._show_cached_snapshot('localhost', 'main')
                self.assertFalse(ok)

    async def test_rendered_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, 'state.json')
            snap_path = os.path.join(td, 'snap.json')
            snaps = {f'h{i}:s{i}': {'lines': f'content {i}', 'ts': str(i)}
                     for i in range(80)}
            with open(state_path, 'w') as f:
                json.dump(SYNTH_STATE, f)
            with open(snap_path, 'w') as f:
                json.dump(snaps, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = snap_path
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Force-render all 80 snapshots; cache must stay capped.
                for i in range(80):
                    app._show_cached_snapshot(f'h{i}', f's{i}')
                self.assertLessEqual(len(app._rendered_cache), 64)


KA_STATE = {
    'updated': '2026-01-01 00:00:00',
    'nodes': {
        'gpu1': {
            'alive': True,
            'socket': '/tmp/whatever',
            'info': {'time': '1-00:00:00', 'state': 'RUNNING',
                     'job_name': 'train_job', 'job_id': '999'},
            'sessions': [['train', '1']],
            'last_error': '',
        },
    },
    'keepalive': {},
}


class KeepAliveToggleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')
        self._saved_ka_path = autotmux.config.KEEPALIVE_PATH

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch
        autotmux.config.KEEPALIVE_PATH = self._saved_ka_path

    async def test_repeated_k_reports_existing_update_without_second_read(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, 'state.json')
            with open(state_path, 'w') as f:
                json.dump(KA_STATE, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'snap.json')
            with open(autotmux.SNAPSHOT_FILE, 'w') as f:
                json.dump({}, f)
            autotmux.config.KEEPALIVE_PATH = os.path.join(td, 'keepalive.json')
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                notices = []
                app.notify = lambda message, **_kwargs: notices.append(message)
                app._ka_inflight.add('job:999')
                with mock.patch.object(
                        app, '_ka_registry_entries') as registry_read:
                    await app.action_toggle_keepalive()
                registry_read.assert_not_called()
                self.assertTrue(any('already in progress' in str(message)
                                    for message in notices))

    async def test_k_toggles_registry_and_marker(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, 'state.json')
            with open(state_path, 'w') as f:
                json.dump(KA_STATE, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'snap.json')
            with open(autotmux.SNAPSHOT_FILE, 'w') as f:
                json.dump({}, f)
            ka_path = os.path.join(td, 'keepalive.json')
            autotmux.config.KEEPALIVE_PATH = ka_path

            app = autotmux.AutotmuxApp()
            # Avoid real scontrol — pretend it's a batch job.
            app._scontrol_job = lambda jid: {
                'batch': True, 'command': '/home/x/train_job',
                'workdir': '/home/x', 'job_name': 'train_job'}
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.selected_node, 'gpu1')
                # Toggle ON
                await pilot.press('k')
                for _ in range(20):
                    await pilot.pause()
                    if autotmux.keepalive.load_registry(ka_path):
                        break
                entries = autotmux.keepalive.load_registry(ka_path)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]['job_name'], 'train_job')
                self.assertEqual(entries[0]['job_id'], '999')
                self.assertTrue(entries[0]['entry_id'])
                self.assertEqual(entries[0]['command'], '/home/x/train_job')
                # Marker shows in the decorated STATUS cell.
                row = [r for r in app.all_sessions if r[1] == 'train'][0]
                self.assertIn('keep-alive', row[4])
                # Toggle OFF
                await pilot.press('k')
                await pilot.pause()
                self.assertEqual(autotmux.keepalive.load_registry(ka_path), [])

    async def test_duplicate_job_names_mark_only_the_selected_job_id(self):
        with tempfile.TemporaryDirectory() as td:
            state = {
                'updated': '2026-01-01 00:00:00',
                'nodes': {
                    'gpu1': {
                        'alive': True,
                        'info': {'job_name': 'h100x2', 'job_id': '101'},
                        'sessions': [['one', '1']], 'last_error': '',
                    },
                    'gpu2': {
                        'alive': True,
                        'info': {'job_name': 'h100x2', 'job_id': '202'},
                        'sessions': [['two', '1']], 'last_error': '',
                    },
                },
                'keepalive': {},
            }
            state_path = os.path.join(td, 'state.json')
            with open(state_path, 'w') as f:
                json.dump(state, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'snap.json')
            with open(autotmux.SNAPSHOT_FILE, 'w') as f:
                json.dump({}, f)
            ka_path = os.path.join(td, 'keepalive.json')
            autotmux.config.KEEPALIVE_PATH = ka_path
            autotmux.keepalive.set_entry_enabled(
                ka_path, 'h100x2', True, '/x/job', '/w',
                job_id='101', entry_id='only-one')

            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                for _ in range(20):
                    if app._ka_entries:
                        break
                    await pilot.pause()
                app._refresh_table(state)
                rows = {row[0]: row for row in app.all_sessions}
                self.assertIn('keep-alive', rows['gpu1'][4])
                self.assertNotIn('keep-alive', rows['gpu2'][4])

    async def test_start_shell_row_can_enable_keepalive_for_its_slurm_job(self):
        with tempfile.TemporaryDirectory() as td:
            state = {
                'updated': '2026-01-01 00:00:00',
                'nodes': {
                    'gpu1': {
                        'alive': True,
                        'info': {'job_name': 'empty-job', 'job_id': '707'},
                        'sessions': [], 'last_error': '',
                    },
                },
                'keepalive': {},
            }
            state_path = os.path.join(td, 'state.json')
            with open(state_path, 'w') as f:
                json.dump(state, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'snap.json')
            with open(autotmux.SNAPSHOT_FILE, 'w') as f:
                json.dump({}, f)
            ka_path = os.path.join(td, 'keepalive.json')
            autotmux.config.KEEPALIVE_PATH = ka_path
            app = autotmux.AutotmuxApp()
            app._scontrol_job = lambda _job_id: {
                'batch': True, 'command': '/x/empty-job',
                'workdir': '/x', 'job_name': 'empty-job',
            }
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.selected_session,
                                 autotmux._START_SHELL_SESSION)
                await app.action_toggle_keepalive()
                for _ in range(30):
                    entries = autotmux.keepalive.load_registry(ka_path)
                    if entries:
                        break
                    await pilot.pause()
                self.assertEqual(entries[0]['job_id'], '707')
                row = app.all_sessions[0]
                self.assertEqual(row[1], autotmux._START_SHELL_SESSION)
                self.assertIn('keep-alive', row[4])

    async def test_offline_row_still_shows_and_can_disable_slurm_keepalive(self):
        with tempfile.TemporaryDirectory() as td:
            state = {
                'updated': '2026-01-01 00:00:00',
                'nodes': {
                    'gpu1': {
                        'alive': False,
                        'info': {'job_name': 'offline-job', 'job_id': '808'},
                        'sessions': [], 'last_error': 'ssh unavailable',
                    },
                },
                'keepalive': {},
            }
            state_path = os.path.join(td, 'state.json')
            with open(state_path, 'w') as f:
                json.dump(state, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'snap.json')
            with open(autotmux.SNAPSHOT_FILE, 'w') as f:
                json.dump({}, f)
            ka_path = os.path.join(td, 'keepalive.json')
            autotmux.config.KEEPALIVE_PATH = ka_path
            autotmux.keepalive.set_entry_enabled(
                ka_path, 'offline-job', True, '/x/job', '/x',
                job_id='808', entry_id='offline-entry')
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                for _ in range(20):
                    await pilot.pause()
                    if app._ka_entries:
                        break
                app._refresh_table(state)
                self.assertEqual(app.all_sessions[0][1],
                                 autotmux._OFFLINE_SESSION)
                self.assertIn('keep-alive', app.all_sessions[0][4])
                await app.action_toggle_keepalive()
                self.assertEqual(
                    autotmux.keepalive.load_registry(ka_path), [])

    async def test_k_on_localhost_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, 'state.json')
            with open(state_path, 'w') as f:
                json.dump(SYNTH_STATE, f)
            autotmux.STATE_FILE = state_path
            autotmux.SNAPSHOT_FILE = os.path.join(td, 'snap.json')
            with open(autotmux.SNAPSHOT_FILE, 'w') as f:
                json.dump({}, f)
            ka_path = os.path.join(td, 'keepalive.json')
            autotmux.config.KEEPALIVE_PATH = ka_path
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Move to the localhost row and press k — must not register.
                app.selected_node = 'localhost'
                app.selected_session = 'main'
                await app.action_toggle_keepalive()
                await pilot.pause()
                self.assertEqual(autotmux.keepalive.load_registry(ka_path), [])

    async def test_successful_enable_does_not_require_an_nfs_reread(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            ka_path = os.path.join(td, 'keepalive.json')
            autotmux.config.KEEPALIVE_PATH = ka_path
            app = autotmux.AutotmuxApp()
            app._scontrol_job = lambda _job_id: {
                'batch': True,
                'command': '/home/x/train_job',
                'workdir': '/home/x',
                'job_name': 'train_job',
            }
            async with app.run_test() as pilot:
                await pilot.pause()
                notices = []
                app.notify = lambda message, **kwargs: notices.append(message)
                app._ka_registry_names = mock.Mock(
                    side_effect=AssertionError('post-write reread is unsafe'))
                app._ka_inflight.add('job:999')
                await app._enable_keepalive_async(
                    'gpu1', '999', 'train_job')
            entries = autotmux.keepalive.load_registry(ka_path)
            self.assertEqual([e['job_name'] for e in entries], ['train_job'])
            self.assertEqual([e['job_id'] for e in entries], ['999'])
            self.assertIn('train_job', app._ka_names)
            self.assertTrue(any('keep-alive ON' in notice for notice in notices))
            app._ka_registry_names.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
