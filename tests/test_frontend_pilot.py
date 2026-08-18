"""Headless tests of the AutotmuxApp via Textual's Pilot framework.

These don't need a running daemon — they point STATE_FILE at a temp
file with synthetic content and exercise the UI logic directly.
"""
import asyncio
import copy
import inspect
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from textual.coordinate import Coordinate

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


def _setup_state(tmpdir, sessions=None):
    """The synthetic state, optionally with a different session list on gpu1.

    `sessions` is for the one question the fixed fixture cannot answer: what
    a layout does when the table has more rows than the screen.
    """
    state = SYNTH_STATE
    if sessions is not None:
        state = copy.deepcopy(SYNTH_STATE)
        node = state['nodes']['gpu1']
        node['sessions'] = [list(s) for s in sessions]
        node['info']['sessions'] = [list(s) for s in sessions]
    state_path = os.path.join(tmpdir, 'state.json')
    snap_path = os.path.join(tmpdir, 'snap.json')
    with open(state_path, 'w') as f:
        json.dump(state, f)
    with open(snap_path, 'w') as f:
        json.dump({}, f)
    autotmux.STATE_FILE = state_path
    autotmux.SNAPSHOT_FILE = snap_path
    return state_path, snap_path


def table_row(app, session):
    """Which table row a session sits on.

    Not its index in all_sessions, and not a constant: the table carries
    band headings of its own, so row 0 is a heading rather than the first
    session. Tests that hard-code a row number are testing the layout of
    the day they were written.
    """
    for i, r in enumerate(app.row_targets):
        if r is not None and r[1] == session:
            return i
    raise AssertionError(f'{session!r} is not on the table')


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
        # The layout is persisted on every `z`. Without this the suite would
        # rewrite the developer's own ~/.config/autotmux/layout.json.
        self._layout_dir = tempfile.TemporaryDirectory()
        self._saved_layout_path = autotmux.config.LAYOUT_PATH
        autotmux.config.LAYOUT_PATH = os.path.join(
            self._layout_dir.name, 'layout.json')

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch
        autotmux.AutotmuxApp._prewarm_interactive_async = self._saved_prewarm
        autotmux.config.LAYOUT_PATH = self._saved_layout_path
        self._layout_dir.cleanup()

    async def test_app_starts_and_renders_table(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Three nodes: two named sessions on gpu1, an offline
                # placeholder for gpu2, and a "start something here" row for
                # each of the two reachable nodes -- a machine you hold is
                # somewhere you can start something whether or not something
                # is already running on it.
                self.assertEqual(len(app.all_sessions), 6)
                kinds = [r[1] for r in app.all_sessions]
                self.assertEqual(
                    kinds.count(autotmux._START_SHELL_SESSION), 2)
                node_names = {r[0] for r in app.all_sessions}
                self.assertEqual(node_names, {'localhost', 'gpu1', 'gpu2'})

    async def test_small_terminal_preserves_session_space_and_full_height_table(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                upper = app.query_one('#upper').region
                # The column, not the table: the queue is the table's sibling
                # inside it now, so the two of them share this height.
                column = app.query_one('#left_column').region
                table = app.query_one('#left_pane').region
                jobs = app.query_one('#jobs_scroll').region
                self.assertEqual(column.height, upper.height)
                self.assertEqual(table.height + jobs.height, column.height)
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
                    # Every control has to be reachable, not just Save: the
                    # cluster row was added above the list and pushed the
                    # buttons off a 20-row terminal until the help text
                    # earned its space back.
                    for widget in ('#connection_cluster', '#connection_add',
                                   '#connection_remove', '#connection_aliases'):
                        element = app.screen.query_one(widget)
                        self.assertLessEqual(element.region.bottom, 20, widget)
                        self.assertLessEqual(element.region.right, 60, widget)

    async def test_jobs_panel_marks_stale_squeue_data(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                state = dict(SYNTH_STATE)
                state['squeue_updated_monotonic'] = time.monotonic() - 100
                app._refresh_jobs(state)
                # The mark, and how far out of date -- the word "stale" went
                # with the absolute timestamp beside it. ⚠ plus an age says
                # the same thing in a title that is the widest line here.
                head = str(app.jobs_view.render()).splitlines()[0]
                self.assertIn('⚠', head)
                self.assertRegex(head, r'\d+[smhd] old')

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
                # Computed, not counted. There is no header row any more and
                # the bands take rows of their own, so which y is a session
                # is a question only the table can answer.
                want = [i for i, r in enumerate(app.row_targets)
                        if r is not None][-1]
                await pilot.click(app.table, offset=(3, want))
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
                # The fallback selection is row 0, so drop the offline node:
                # attention ordering would otherwise put it there and this
                # test is about the preview, not the sort.
                state['nodes'].pop('gpu2', None)
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

    async def test_a_tick_of_the_clock_updates_in_place(self):
        """The in-place path exists because clear()+add_row resets the
        cursor, and something on these rows changes every five seconds. What
        it rewrites is the whole line now -- one cell, composed -- rather
        than four cells of a grid.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause()
                state = {'updated': 'now', 'nodes': {'gpu1': {
                    'alive': True, 'socket': '/tmp/x', 'last_error': '',
                    'info': {'time': '1-00:00:00'},
                    'sessions': [['train', '1', 400]]}}}
                app._refresh_table(state)
                await pilot.pause()
                before = str(app.table.get_cell_at(
                    Coordinate(table_row(app, 'train'), 0)))
                state['nodes']['gpu1']['sessions'][0][2] = 500
                app._refresh_table(state)
                await pilot.pause()
                after = str(app.table.get_cell_at(
                    Coordinate(table_row(app, 'train'), 0)))
                self.assertNotEqual(before, after)
                self.assertIn('train', after)
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
                cell = app.table.get_cell_at(
                    Coordinate(table_row(app, '[bold]literal[/bold]'), 0))
                self.assertIsInstance(cell, autotmux.rich.text.Text)
                # Composed, so the markup has to survive inside the line
                # rather than as a cell of its own -- Text.append() never
                # parses it, which is the property being guarded.
                self.assertIn('[bold]literal[/bold]', cell.plain)

    async def test_the_arrows_move_the_table_while_the_table_has_the_keyboard(self):
        """The concern that once emptied the focus chain, kept.

        Both scrollers used to be unfocusable so that a stray click or Tab
        could not leave up/down scrolling a preview while the reader was
        trying to move the session cursor. It worked, and it cost more than it
        saved: over SSH mouse reporting is off by default -- which is what
        this tool is for -- so with the focus chain empty the preview and the
        queue could not be scrolled by any means at all. Measured before:
        fourteen keys tried against 575 hidden rows of preview, nothing moved.

        The real fault was that focus was silent, so it is drawn now (see the
        :focus rules) and there is one key back. What must not regress is
        this: while the table has the keyboard, the arrows are the table's.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertTrue(app.table.has_focus,
                                'the list must start with the keyboard')
                preview = app.query_one('#right_pane_scroll')
                jobs = app.query_one('#jobs_scroll')
                before = app.selected_session
                where = (preview.scroll_y, jobs.scroll_y)
                await pilot.press('down')
                await pilot.pause()
                self.assertNotEqual(app.selected_session, before,
                                    'down must move the table cursor')
                self.assertEqual((preview.scroll_y, jobs.scroll_y), where,
                                 'a pane scrolled while the table was steering')

    async def test_every_pane_on_screen_can_be_reached_by_keyboard(self):
        """The bug, stated as the rule it broke: a pane holding more than it
        shows, with no key that reaches the rest, is a pane that is not there.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                # The order the grid reads: the left column top to bottom,
                # then the column beside it.
                self.assertEqual([w.id for w in app.screen.focus_chain],
                                 ['left_pane', 'jobs_scroll',
                                  'right_pane_scroll'],
                                 'every visible pane, in the order it is read')
                # Tab walks them and comes back round.
                seen = []
                for _ in range(3):
                    await pilot.press('tab')
                    await pilot.pause()
                    seen.append(app.focused.id)
                self.assertEqual(seen, ['jobs_scroll', 'right_pane_scroll',
                                        'left_pane'])
                # And the digits go straight there, which is what you want the
                # second time.
                # The digits agree with Tab, which is the point of them.
                for key, want in (('2', 'jobs_scroll'),
                                  ('3', 'right_pane_scroll'),
                                  ('1', 'left_pane')):
                    with self.subTest(key=key):
                        await pilot.press(key)
                        await pilot.pause()
                        self.assertEqual(app.focused.id, want)

    async def test_the_cursor_never_rests_on_a_band_heading(self):
        """A heading is a table row because a DataTable has no other way to
        put one between rows -- and it is not somewhere the cursor may stop:
        attach, preview and `k` all read the selected row, and a heading is
        not a row you can act on.

        The ends are where this goes wrong. The first row of the table is a
        heading, so `up` from the row below lands on one with nothing above.
        Measured before the fix: seven presses of `up` all rested on row 0.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                heads = set(app.table.heading_rows)
                self.assertTrue(heads, 'no bands, so nothing is proved')
                for key in ('down', 'up'):
                    for _ in range(app.table.row_count + 3):
                        await pilot.press(key)
                        await pilot.pause()
                        with self.subTest(key=key, row=app.table.cursor_row):
                            self.assertNotIn(app.table.cursor_row, heads)
                            self.assertTrue(app.selected_session)

    async def test_a_heading_is_not_a_target_even_if_something_lands_on_it(self):
        """Belt as well as braces: the cursor is kept off them, and asking
        what one points at answers nothing rather than the adjacent row."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                keys = list(app.table.rows)
                for row in sorted(app.table.heading_rows):
                    with self.subTest(row=row):
                        self.assertIsNone(app._row_target(keys[row]))

    async def test_the_bands_are_drawn_and_the_offers_come_last(self):
        """The ranks have always sorted this table and were never drawn."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                titles = [str(app.table.get_cell_at(Coordinate(r, 0)))
                          .replace('── ', '')
                          for r in sorted(app.table.heading_rows)]
                self.assertTrue(titles)
                # Every heading counts what is under it, so "just stopped 4"
                # can be read without counting rows.
                for title in titles:
                    with self.subTest(title=title):
                        # The count, then whatever keys that band advertises.
                        self.assertRegex(title, r' \d+(\s|$)')
                # The bands keep the order the ranks already chose; they must
                # not invent one. Asserted as an ordering rather than as a
                # fixed list, because which bands exist depends on what is
                # running -- an empty band is never drawn.
                order = [t for t, _ in autotmux.model.BANDS]
                order = [autotmux.model.BAND_TITLES[r] for r in order]
                order.append(autotmux.model.OFFER_TITLE)
                seen = [next(i for i, o in enumerate(order)
                             if t.startswith(o)) for t in titles]
                self.assertEqual(seen, sorted(seen),
                                 f'bands out of order: {titles}')

    async def test_the_preview_says_whose_output_it_is_showing(self):
        """It showed a screenful of someone's output with nothing to say
        whose. The only clue was the table cursor beside it -- and that clue
        got weaker the moment the keyboard could move into this pane and the
        cursor dimmed.

        The queue below has carried its own title, and its own key, all
        along. This is the same, so the two secondary panes read as siblings
        rather than as one titled panel and one anonymous slab.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.snapshots['gpu1:train'] = {
                    'lines': 'epoch 1\nepoch 2\n', 'ts': '12:00:00',
                    'captured_monotonic': autotmux.time.monotonic(),
                    'monotonic_clock_id': autotmux._CLOCK_ID,
                }
                self.assertTrue(app._show_cached_snapshot('gpu1', 'train'))
                await pilot.pause()
                text = str(app.log_view.render())
                head = text.splitlines()[0]
                self.assertIn('gpu1:train', head)
                # The key belongs beside the thing it acts on, which is the
                # rule the queue's own title already follows.
                self.assertIn('3: scroll', head)
                self.assertIn('epoch 1', text)

    async def test_every_state_of_the_preview_carries_the_same_heading(self):
        """A heading that appears when the first snapshot lands, and is
        absent before it, is a heading that moves the text under it."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.selected_node, app.selected_session = 'gpu1', 'train'
                app.snapshots.clear()
                app._render_preview_now()
                await pilot.pause()
                text = str(app.log_view.render())
                self.assertIn('gpu1:train', text)
                self.assertIn('connecting', text)

    async def test_ages_are_read_at_a_glance_rather_than_converted(self):
        """Every age on this screen was raw seconds, which is fine for the
        first minute and unreadable after it: a stale daemon reported
        "stale (19313589s old)", a number nobody converts in their head and
        which reads as a fault in the display rather than in the daemon.

        The browser client has always said "5m ago" for the same quantity, so
        the two halves of one tool disagreed about how to say one thing.
        """
        cases = [(0, '0s'), (45, '45s'), (90, '2m'), (300, '5m'),
                 (3600, '1h'), (7200, '2h'), (86400, '1d'),
                 (19313589, '224d'), (None, '?'), (float('nan'), '?')]
        for seconds, want in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(autotmux.fmt_age(seconds), want)
        # Negative clock skew is not a duration; it must not print one.
        self.assertEqual(autotmux.fmt_age(-5), '0s')

    async def test_the_queue_says_how_old_rather_than_when(self):
        """An absolute timestamp and a relative age said the same thing twice
        in the widest line of the panel, and "updated 2026-01-01 00:00:00" is
        19 columns spent on the form of the answer nobody wanted. The
        question is only ever "is this current"."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app._refresh_jobs(app._last_state or {})
                await pilot.pause()
                text = str(app.jobs_view.render())
                head = text.splitlines()[0]
                self.assertNotIn('updated 2026-01-01', head)
                self.assertRegex(head, r'\d+[smhd] old')
                # And the key still rides its own title.
                self.assertIn('j: switch', head)

    async def test_a_pane_with_the_keyboard_actually_scrolls(self):
        """Reaching it is only half. Measured after: the preview takes down,
        pagedown, end and home; the queue takes them too, and ← → for the
        tail of a 95-column squeue row that no other key could reach.
        """
        long_text = '\n'.join(f'line {n:03d}' for n in range(200))
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.query_one('#right_pane').update(long_text)
                await pilot.pause()
                preview = app.query_one('#right_pane_scroll')
                self.assertGreater(preview.max_scroll_y, 20,
                                   'nothing hidden, so nothing proved')
                await pilot.press('3')
                await pilot.pause()
                await pilot.press('down')
                await pilot.pause()
                self.assertEqual(preview.scroll_y, 1)
                await pilot.press('end')
                await pilot.pause()
                self.assertEqual(preview.scroll_y, preview.max_scroll_y)
                await pilot.press('home')
                await pilot.pause()
                self.assertEqual(preview.scroll_y, 0)

    async def test_escape_always_returns_the_keyboard_to_the_list(self):
        """Focus without a way back is a maze, and being a maze is what made
        it worth removing the first time. One key, from any pane."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                for key in ('2', '3'):
                    with self.subTest(from_pane=key):
                        await pilot.press(key)
                        await pilot.pause()
                        self.assertNotEqual(app.focused.id, 'left_pane')
                        await pilot.press('escape')
                        await pilot.pause()
                        self.assertEqual(app.focused.id, 'left_pane')

    async def test_the_keyboard_never_stays_on_a_pane_that_left_the_screen(self):
        """`z` can take the pane you are steering off the screen. The keys
        would then go somewhere invisible, and the way back is a key nobody
        would think to press."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press('3')
                await pilot.pause()
                self.assertEqual(app.focused.id, 'right_pane_scroll')
                app.layout_mode = 'table'          # preview and queue gone
                app._apply_layout()
                await pilot.pause()
                self.assertEqual(app.focused.id, 'left_pane')
                self.assertFalse(app.query_one('#right_pane_scroll').can_focus)

    async def test_asking_for_a_pane_this_layout_hides_says_so(self):
        """A key that silently does nothing is the complaint this section
        exists to answer, so the one case where it legitimately cannot act
        has to say why."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.layout_mode = 'table'
                app._apply_layout()
                await pilot.pause()
                said = []
                app.notify = lambda msg, **kw: said.append(msg)
                await pilot.press('3')
                await pilot.pause()
                self.assertTrue(said, 'the key did nothing, silently')
                self.assertIn('z', said[0], 'no way out was offered')

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
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(autotmux, '_daemon_running',
                                  return_value=True):
            # Pin the daemon as up. Otherwise the app starts its recovery path
            # and reports "starting daemon…", so the assertion below would
            # depend on whether a real daemon happens to run on the machine
            # running the tests.
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

    async def _row_with_marker(self, pilot, pick, limit=40):
        """Wait for the *table* to carry the keep-alive marker.

        Writing the registry and re-rendering the row are separate async
        steps, so polling the file and then asserting on the row is a race.
        It lost exactly once -- on CI, on 3.11 only -- with
        `'keep-alive' not found in 'No sessions'`.
        """
        row = None
        for _ in range(limit):
            row = pick()
            if row is not None and 'keep-alive' in row[4]:
                return row
            await pilot.pause()
        return row

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
            app._scontrol_job = lambda jid, node=None: {
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
                row = await self._row_with_marker(
                    pilot,
                    lambda: next((r for r in app.all_sessions
                                  if r[1] == 'train'), None))
                self.assertIsNotNone(row)
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
            app._scontrol_job = lambda _job_id, _node=None: {
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
                row = await self._row_with_marker(
                    pilot,
                    lambda: app.all_sessions[0] if app.all_sessions else None)
                self.assertIsNotNone(row)
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
            app._scontrol_job = lambda _job_id, _node=None: {
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


JOB_STATE = {
    'updated': '2026-08-04 12:00:00',
    'nodes': {
        'gpu1': {'alive': True, 'socket': '/tmp/x', 'last_error': '',
                 'info': {'job_id': '4172318', 'job_name': 'train',
                          'state': 'RUNNING', 'time': '0:45:00'},
                 'sessions': [['work', '1']]},
        'gpu2': {'alive': True, 'socket': '/tmp/y', 'last_error': '',
                 'info': {'job_id': '999', 'job_name': 'long',
                          'state': 'RUNNING', 'time': '8:00:00'},
                 'sessions': [['other', '1']]},
    },
}


class ExpiringJobWarningTests(unittest.IsolatedAsyncioTestCase):
    """A job nearing its limit is announced on the machine running the TUI,
    once, and not again after a restart."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.warned_file = os.path.join(self.temp.name, 'warned.json')
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')
        self.patchers = [
            mock.patch.object(autotmux, '_WARNED_JOBS_FILE', self.warned_file),
            mock.patch.object(autotmux, '_daemon_running', return_value=True),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        autotmux._launch_daemon = self._saved_launch
        self.temp.cleanup()

    async def _run(self, state, cfg=None):
        """Refresh once and report (desktop calls, in-app toasts, app)."""
        desktop, toasts = [], []
        with mock.patch.object(
                autotmux.notify, 'local_notify',
                side_effect=lambda *a, **k: desktop.append(a) or True):
            app = autotmux.AutotmuxApp()
            if cfg is not None:
                app._notify_cfg = cfg
            async with app.run_test() as pilot:
                app.notify = lambda message, **kwargs: toasts.append(message)
                app._refresh_table(state)
                await pilot.pause()
                app._refresh_table(state)   # a second tick must stay quiet
                await pilot.pause()
        return desktop, toasts, app

    async def test_a_job_near_its_limit_is_announced_once(self):
        desktop, toasts, app = await self._run(JOB_STATE)
        self.assertEqual(len(desktop), 1)
        self.assertEqual(len(toasts), 1)
        self.assertIn('4172318', toasts[0])
        self.assertIn('45m', toasts[0])
        self.assertEqual(app._warned_jobs, {'4172318'})

    async def test_a_job_with_hours_left_is_not_announced(self):
        state = {'nodes': {'gpu2': JOB_STATE['nodes']['gpu2']}}
        desktop, toasts, _ = await self._run(state)
        self.assertEqual(desktop, [])
        self.assertEqual(toasts, [])

    async def test_a_restart_does_not_re_announce(self):
        await self._run(JOB_STATE)
        desktop, toasts, _ = await self._run(JOB_STATE)
        self.assertEqual(desktop, [])
        self.assertEqual(toasts, [])

    async def test_desktop_off_still_shows_the_in_app_toast(self):
        """`desktop` silences the OS popup only; the dashboard's own banner is
        part of the UI the user is already looking at."""
        cfg = dict(autotmux.config.NOTIFY_DEFAULTS)
        cfg['desktop'] = False
        desktop, toasts, _ = await self._run(JOB_STATE, cfg)
        self.assertEqual(desktop, [])
        self.assertEqual(len(toasts), 1)
        self.assertIn('4172318', toasts[0])

    async def test_master_switch_silences_everything(self):
        cfg = dict(autotmux.config.NOTIFY_DEFAULTS)
        cfg['enabled'] = False
        desktop, toasts, _ = await self._run(JOB_STATE, cfg)
        self.assertEqual(desktop, [])
        self.assertEqual(toasts, [])

    async def test_a_broken_notifier_never_breaks_the_refresh(self):
        with mock.patch.object(autotmux.notify, 'jobs_from_state',
                               side_effect=RuntimeError('boom')):
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                app._refresh_table(JOB_STATE)      # must not raise
                await pilot.pause()
                self.assertTrue(app.all_sessions)


class ComposedRowTests(unittest.IsolatedAsyncioTestCase):
    """Each row is one composed line, not six cells.

    A grid is the wrong shape for rows meant to read as sentences: every
    column is shared by everything in it, so a band heading had to be
    squeezed into one of them, and one wide value set a column's width for
    every row. Composing the line lets the last field take whatever is left
    -- which is where the output goes, and the output is the thing that says
    whether a stopped session finished or broke.
    """

    IDLE_STATE = {
        'updated': '2026-01-01 00:00:00',
        'nodes': {'gpu1': {
            'alive': True, 'socket': '/tmp/x', 'last_error': '',
            'info': {'time': '1-00:00:00'},
            'sessions': [['quiet', '1', 900], ['old', '1', 7200],
                         ['busy', '1', 5]]}},
    }

    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch

    async def _lines(self, state, snapshots=None):
        """The composed line for each session, by session name."""
        app = autotmux.AutotmuxApp()
        async with app.run_test(size=(120, 30)) as pilot:
            if snapshots:
                app.snapshots = snapshots
            app._refresh_table(state)
            await pilot.pause()
            out = {}
            for i, r in enumerate(app.row_targets):
                if r is None:
                    continue
                out[r[1]] = app.table.get_cell_at(Coordinate(i, 0))
            return out

    async def test_the_table_is_one_column_with_no_header(self):
        """A header names columns, and there are none to name. The bands say
        what each group is, which is the heading that was actually missing."""
        app = autotmux.AutotmuxApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(len(app.table.columns), 1)
            self.assertFalse(app.table.show_header)

    async def test_the_idle_marker_leads_and_is_coloured_by_tier(self):
        lines = await self._lines(self.IDLE_STATE)
        self.assertTrue(str(lines['quiet']).startswith('● 15m'))
        # The token, not the literal. Which colour "warn" is depends on
        # whether the terminal admits to truecolour; that it escalates, and
        # that the two differ, does not.
        self.assertEqual(str(lines['quiet'].spans[0].style),
                         autotmux.tone('warn'))
        self.assertEqual(str(lines['old'].spans[0].style),
                         autotmux.tone('danger'))
        self.assertNotEqual(autotmux.tone('warn'), autotmux.tone('danger'))

    async def test_a_busy_session_leads_with_nothing(self):
        """Silence is the healthy state, so it says nothing at all."""
        lines = await self._lines(self.IDLE_STATE)
        self.assertTrue(str(lines['busy']).startswith(' '))
        self.assertNotIn('●', str(lines['busy']))

    async def test_the_name_comes_before_the_machine(self):
        """It is what the reader navigates by. It used to be third, behind a
        dot, a duration and a machine name."""
        lines = await self._lines(self.IDLE_STATE)
        text = str(lines['quiet'])
        self.assertLess(text.index('quiet'), text.index('gpu1'))

    async def test_the_line_does_not_repeat_the_preview(self):
        """The pane's last line used to end every row. With the preview
        beside the list it is the same answer twice, and read down a column
        it is a stack of unrelated sentences -- clutter rather than
        information. The preview says it for the row you are on, which is
        the row you are asking about.
        """
        lines = await self._lines(self.IDLE_STATE, snapshots={
            'gpu1:quiet': {'lines': 'collected 214 items\n1264 passed\n'},
        })
        self.assertNotIn('1264 passed', str(lines['quiet']))
        self.assertNotIn('collected', str(lines['quiet']))

    async def test_a_session_with_no_snapshot_yet_says_nothing(self):
        """Rather than an apology. The snapshot loop fills in on its own."""
        lines = await self._lines(self.IDLE_STATE)
        # The rail is always there now; what a row without a snapshot must
        # not carry is a pane line, not a walltime.
        self.assertIn('busy', str(lines['busy']))
        self.assertIn('gpu1', str(lines['busy']))

    async def test_a_troubled_row_reports_instead_of_its_output(self):
        """Quieting the healthy baseline must not quiet the rows that
        matter: DEGRADED is worth more than the last line of the pane."""
        state = json.loads(json.dumps(self.IDLE_STATE))
        state['nodes']['gpu1']['last_error'] = 'connect timeout'
        lines = await self._lines(state)
        self.assertIn('DEGRADED', str(lines['quiet']))
        self.assertIn('connect timeout', str(lines['quiet']))

    async def test_an_offline_node_names_the_machine_not_a_placeholder(self):
        """`<offline>` under a heading that reads "not reachable" is the
        repetition the bands were meant to remove. For these rows the
        machine is the identity: there is no session to name."""
        state = json.loads(json.dumps(self.IDLE_STATE))
        state['nodes']['gpu1']['alive'] = False
        state['nodes']['gpu1']['last_error'] = 'master down'
        lines = await self._lines(state)
        line = next(v for k, v in lines.items() if 'offline' in k)
        self.assertIn('gpu1', str(line))
        self.assertNotIn('<offline>', str(line))
        self.assertIn('OFFLINE', str(line))

    async def test_the_top_line_answers_the_question_the_screen_is_for(self):
        """Textual's stock Header draws a name, a clock space and a subtitle,
        so the widest line on the screen said `atmux` and then, usually,
        nothing -- and it read as a Textual app rather than as this one.

        What belongs there is whether anything is wrong. Counted off the same
        plan the table is built from, so the header and the bands under it
        cannot disagree about how many things stopped.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                line = str(app.query_one('#statusline').render())
                self.assertIn('atmux', line)
                bands = [str(app.table.get_cell_at(Coordinate(r, 0)))
                         for r in sorted(app.table.heading_rows)]
                for band in bands:
                    title = band.replace('── ', '').rsplit(' ', 1)[0].strip()
                    if title in autotmux.AutotmuxApp._BAND_TONES:
                        with self.subTest(band=title):
                            self.assertIn(title, line)

    async def test_a_key_is_never_hidden_by_being_switched_off(self):
        """Filtering the footer by focus took `z`, `s` and `g` out of service
        whenever the keyboard was on the list, because Textual's False means
        disabled as well as hidden and there is no "works but is not
        advertised". A key that stops working because you looked at another
        pane is a worse bug than a long footer.

        So the footer is short by demotion, and check_action only ever
        retires a key that would genuinely do nothing where it is.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                for pane in ('1', '2', '3'):
                    await pilot.press(pane)
                    await pilot.pause()
                    for action in ('cycle_layout', 'open_shell',
                                   'manage_connections', 'toggle_jobs_view'):
                        with self.subTest(pane=pane, action=action):
                            self.assertIsNot(
                                app.check_action(action, ()), False,
                                f'{action} was switched off in pane {pane}')

    async def test_escape_is_retired_only_where_it_would_do_nothing(self):
        """There is no back from the list; everywhere else it is the way
        out, and the footer says so there and not here."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press('1')
                await pilot.pause()
                self.assertFalse(app.check_action('focus_table', ()))
                await pilot.press('2')
                await pilot.pause()
                self.assertTrue(app.check_action('focus_table', ()))

    async def test_each_number_says_what_it_is(self):
        """The rail was `1d16h  c▌ 13%  g███▉ 97%` and unreadable, which is
        fair: the header row went when the table became one column, so
        nothing on screen said what `c` or `g` meant -- and the bar and the
        percentage beside it were one fact in nine columns, said twice.

        There are also two durations on the line meaning opposite things:
        the dot at the front is how long this has been quiet, and the other
        is how long is left. Bare, they were the same shape twice.
        """
        row = ('gpu1', 'a', '1', '2:00:00', '', '96', '48.0',
               '61 40000 81559 2')
        rail = autotmux._row_rail(row).plain
        self.assertIn('cpu', rail)
        self.assertIn('gpu', rail)
        self.assertIn('⧗', rail)        # and this one is time remaining
        self.assertIn('50%', rail)
        self.assertIn('61%', rail)
        # One fact once: no bar beside the number it repeats.
        self.assertNotIn('█', rail)
        # And the word runs straight into its right-justified number, so a
        # column of them lines up without either spending a cell on a gap.
        self.assertEqual(autotmux._meter(0.13, 'cpu').plain, 'cpu 13%')
        self.assertEqual(autotmux._meter(1.0, 'cpu').plain, 'cpu100%')
        self.assertEqual(len(autotmux._meter(0.05, 'gpu').plain),
                         len(autotmux._meter(1.0, 'gpu').plain))

    async def test_the_design_survives_sixteen_colours(self):
        """Over SSH into a cluster, sixteen is often all there is -- and if
        stripping the colour breaks the design, the design was leaning on
        decoration. Each token carries both, and the terminal decides."""
        for token in ('ok', 'warn', 'danger', 'quiet', 'accent'):
            with self.subTest(token=token):
                rich_, plain = autotmux._TOKENS[token]
                self.assertTrue(rich_.startswith('#'))
                self.assertFalse(plain.startswith('#'))
        self.assertNotEqual(autotmux._TOKENS['warn'][1],
                            autotmux._TOKENS['danger'][1])
        self.assertEqual(autotmux.tone('nonsense'), '')

    async def test_the_reserved_width_matches_what_is_drawn(self):
        """Counted by hand it was two columns short -- `cpu 13%` is eight
        cells, not seven -- so the GPU was silently dropped from rows that
        had room for it. Measured from the renderer instead."""
        row = ('gpu1', 'a', '1', '2:00:00', '', '96', '48.0',
               '61 40000 81559 2')
        self.assertLessEqual(autotmux._row_rail(row).cell_len, autotmux._RAIL)
        # And not wastefully wide either: the reservation is what a full
        # rail actually needs.
        self.assertEqual(autotmux._row_rail(row).cell_len, autotmux._RAIL)

    async def test_a_narrow_rail_drops_fields_rather_than_cutting_them(self):
        """A number cut in half reads as a smaller number -- `cpu   1` was
        really 13% -- which is worse than not showing it. The walltime is
        the one that cannot be undone, so it is the one that stays."""
        row = ('gpu1', 'a', '1', '2:00:00', '', '96', '48.0',
               '61 40000 81559 2')
        full = autotmux._row_rail(row, 999).plain
        self.assertIn('gpu', full)
        # Room for the walltime and one meter, not two.
        tight = autotmux._row_rail(row, 16).plain
        self.assertIn('⧗', tight)
        self.assertIn('cpu', tight)
        self.assertNotIn('gpu', tight)
        # Whatever is kept is whole.
        self.assertTrue(tight.rstrip().endswith('%'))
        # And with room for neither, the walltime alone survives.
        barest = autotmux._row_rail(row, 8).plain
        self.assertIn('⧗', barest)
        self.assertNotIn('cpu', barest)

    async def test_a_row_never_composes_wider_than_its_budget(self):
        """One cell over is one cell the table clips, and at the widths
        where it happened the idle marker and the rail looked welded
        together.

        The gap before the rail was appended as `max(1, room - rail)` but
        never budgeted, so whenever the rail exactly filled the room the line
        came out a cell too long. Swept 30..140 columns across a session, an
        offer, an offline row and a bare one: 52, 48 and 40 were each one
        over before, and none are now.
        """
        rows = [
            ('gpu1', 'newclaw', '1', '1-16:00:00', '● 54m Active',
             '96', '12.6', '97 78000 81559 1'),
            ('login--a', autotmux._START_SHELL_SESSION, '-', '-', '',
             '32', '182', ''),
            ('gpu9', autotmux._OFFLINE_SESSION, '-', '30:00',
             'OFFLINE: connect timeout', '', '', ''),
        ]
        for total in range(30, 141):
            widths = autotmux._row_widths(rows, total)
            for row in rows:
                line = autotmux._compose_session(row, widths, '', total)
                with self.subTest(total=total, session=row[1][:10]):
                    self.assertLessEqual(line.cell_len, total)

    async def test_the_row_width_asks_the_widget_for_its_chrome(self):
        """A line composed to 54 came out clipped at 52 on a 58-column
        screen: the DataTable takes cell_padding on each side of its one
        column, and a scrollbar gutter when it has one. Guessing at that is
        what made the numbers lie."""
        source = inspect.getsource(autotmux.AutotmuxApp._row_width)
        self.assertIn('cell_padding', source)
        self.assertIn('scrollbar_size_vertical', source)

    async def test_the_rail_is_always_there(self):
        """Hiding the walltime and the load until something was wrong was my
        idea and it was wrong: on a Slurm workflow "how long have I got" and
        "is the machine actually working" are what a person watches, not what
        they wait to be told. A number that only appears once it is too late
        is not a number anybody was watching.
        """
        def row(node, session, left, load, cpu, gpu=''):
            return (node, session, '1', left, '', cpu, load, gpu)
        healthy = autotmux._row_rail(
            row('gpu1', 'a', '2-00:00:00', '4.0', '96')).plain
        self.assertIn('2d', healthy)
        self.assertIn('%', healthy)
        # And the GPU beside it where there is one.
        withgpu = autotmux._row_rail(
            row('gpu1', 'a', '0:22:14', '94.0', '96', '61 40000 81559 2')).plain
        self.assertIn('22m', withgpu)
        self.assertEqual(withgpu.count('%'), 2, 'cpu and gpu both')

    async def test_a_node_with_no_cards_shows_no_gpu(self):
        """Rather than a zero, which reads as a card doing nothing."""
        rail = autotmux._row_rail(
            ('gpu1', 'a', '1', '2:00:00', '', '96', '4.0', '')).plain
        self.assertEqual(rail.count('%'), 1)

    async def test_neither_number_means_anything_on_a_placeholder(self):
        """An unreachable node's walltime is whatever it last said before it
        went, and a login node runs at five times its core count all day --
        drawing that in red on every one of them is a warning about the
        normal state of the machine."""
        for session in (autotmux._OFFLINE_SESSION,
                        autotmux._START_SHELL_SESSION):
            with self.subTest(session=session):
                rail = autotmux._row_rail(
                    ('login--a', session, '1', '0:10:00', '', '32', '182.0'))
                self.assertEqual(rail.plain, '')

    async def test_one_long_name_does_not_widen_the_column(self):
        """`tend-mgr-tendsystemoptimizer20260810-25eda74d` is a real session
        name, and at 45 characters it set the column for every row beside it
        -- so four-letter names sat in a 45-cell trough."""
        long_name = 'tend-mgr-tendsystemoptimizer20260810-25eda74d'
        rows = [('gpu1', long_name, '1', '-', '', '', '', ''),
                ('gpu1', '4gpu', '1', '-', '', '', '', '')]
        _lead, name, _where = autotmux._row_widths(rows, 120)
        self.assertLessEqual(name, autotmux._NAME_CAP)

    async def test_a_clipped_name_says_which_end_is_hidden(self):
        """An ellipsis on whichever side is hiding something, so the row
        says there is more rather than merely ending early. The head is kept
        when nothing has scrolled: session names put the purpose first and
        the tail is often a date or a hash -- which is the opposite of a
        machine name, whose end is what distinguishes it."""
        text = 'tend-mgr-tendsystemoptimizer20260810-25eda74d'
        self.assertEqual(autotmux._window(text, 20, 0),
                         'tend-mgr-tendsystem…')
        middle = autotmux._window(text, 20, 14)
        self.assertTrue(middle.startswith('…') and middle.endswith('…'))
        end = autotmux._window(text, 20, 99)      # clamped to the tail
        self.assertTrue(end.startswith('…'))
        self.assertTrue(end.endswith('25eda74d'))
        # A name that fits is padded, never marked.
        self.assertEqual(autotmux._window('4gpu', 8, 0), '4gpu    ')

    async def test_the_scroll_walks_to_the_end_and_then_stops(self):
        """Bounded on purpose. This file already refuses to run a clock in
        the header, because an idle repaint over SSH is a trickle of traffic
        for nothing -- and a marquee that loops for ever is that trickle
        with a reason attached. It walks once, and only on the row the
        cursor is on, and only when something is genuinely hidden.
        """
        long_name = 'tend-mgr-tendsystemoptimizer20260810-25eda74d'
        state = {'updated': 'now', 'nodes': {'gpu1': {
            'alive': True, 'socket': '/tmp/x', 'last_error': '',
            'info': {'time': '2:00:00'},
            'sessions': [[long_name, '1', 10], ['4gpu', '1', 10]]}}}
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            with open(autotmux.STATE_FILE, 'w') as handle:
                json.dump(state, handle)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(90, 20)) as pilot:
                await pilot.pause()
                app._refresh_table(state)
                await pilot.pause()
                # A short name is fully visible, so nothing ticks for it.
                app.table.move_cursor(row=table_row(app, '4gpu'))
                await pilot.pause()
                self.assertIsNone(app._marquee_timer)
                # A long one walks.
                app.table.move_cursor(row=table_row(app, long_name))
                await pilot.pause()
                self.assertIsNotNone(app._marquee_timer)
                for _ in range(120):
                    await asyncio.sleep(0.05)
                    await pilot.pause()
                    if app._marquee_timer is None:
                        break
                self.assertIsNone(app._marquee_timer, 'it never stopped')
                shown = str(app.table.get_cell_at(
                    Coordinate(table_row(app, long_name), 0)))
                self.assertIn('25eda74d', shown, 'the tail was never reached')

    async def test_the_scroll_is_keyed_to_the_session_not_the_row(self):
        """A refresh rebuilds the table every few seconds and re-emits a
        highlight for whatever the cursor lands back on. Keyed by row, or
        treating that as a move, sent a name you were part-way through
        reading back to its head every five seconds."""
        source = inspect.getsource(autotmux.AutotmuxApp._marquee_reset)
        self.assertIn('key == self._marquee_key', source)
        self.assertIn('_marquee_key', inspect.getsource(
            autotmux.AutotmuxApp._marquee_tick))

    async def test_a_narrow_screen_keeps_the_part_of_a_name_that_differs(self):
        """Cut from the right, holygpu8a15104 / 15304 / 15602 all become
        `holygpu8a1` -- the part every node in the cluster shares, and none
        of the part that says which one. Machine names distinguish at the
        end, so that is the end to keep."""
        self.assertEqual(autotmux._fit_node('holygpu8a15304', 10), '…pu8a15304')
        self.assertEqual(autotmux._fit_node('localhost', 12), 'localhost   ')
        # Distinguishable is the whole point: two nodes must not collide.
        cut = {autotmux._fit_node(n, 10) for n in
               ('holygpu8a15104', 'holygpu8a15304', 'holygpu8a15602')}
        self.assertEqual(len(cut), 3)

    async def test_updates_in_place_keep_the_line_correct(self):
        """The in-place path exists because a rebuild every five seconds
        resets the cursor, and the load average ticks that often."""
        app = autotmux.AutotmuxApp()
        async with app.run_test(size=(120, 30)) as pilot:
            app._refresh_table(self.IDLE_STATE)
            await pilot.pause()
            moved = json.loads(json.dumps(self.IDLE_STATE))
            moved['nodes']['gpu1']['sessions'][0][2] = 7200   # quiet -> stale
            app._refresh_table(moved)
            await pilot.pause()
            row = table_row(app, 'quiet')
            self.assertTrue(
                str(app.table.get_cell_at(Coordinate(row, 0))).startswith('● 2h'))

class LayoutModeTests(unittest.IsolatedAsyncioTestCase):
    """`z` trades panes for room.

    The default split spends 44% of the width on the live preview and up to 14
    lines on the queue. That is the right default and the wrong shape on an
    80x24 terminal, or whenever the answer is in the table.
    """

    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')
        self._saved_prewarm = autotmux.AutotmuxApp._prewarm_interactive_async

        async def no_prewarm(_app, _nodes, _source_pool):
            return None

        autotmux.AutotmuxApp._prewarm_interactive_async = no_prewarm
        self._layout_dir = tempfile.TemporaryDirectory()
        self._saved_layout_path = autotmux.config.LAYOUT_PATH
        autotmux.config.LAYOUT_PATH = os.path.join(
            self._layout_dir.name, 'layout.json')

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch
        autotmux.AutotmuxApp._prewarm_interactive_async = self._saved_prewarm
        autotmux.config.LAYOUT_PATH = self._saved_layout_path
        self._layout_dir.cleanup()

    async def test_z_walks_the_cycle_and_comes_back(self):
        """No second key to undo it: pressing z enough times always restores
        the view you started from."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                seen = [app.layout_mode]
                for _ in autotmux.config.LAYOUT_MODES:
                    await pilot.press('z')
                    await pilot.pause()
                    seen.append(app.layout_mode)
                self.assertEqual(seen[:-1], list(autotmux.config.LAYOUT_MODES))
                self.assertEqual(seen[-1], seen[0])

    async def test_each_mode_shows_exactly_the_panes_it_names(self):
        expected = {
            #             table  preview  jobs
            'split': (True, True, True),
            'wide': (True, False, True),
            'table': (True, False, False),
            'jobs': (False, False, True),
        }
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            # Wide enough for the split to be worth making; the narrow case is
            # its own behaviour and has its own tests below.
            async with app.run_test(size=(130, 40)) as pilot:
                await pilot.pause()
                for mode, (table, preview, jobs) in expected.items():
                    with self.subTest(mode=mode):
                        app.layout_mode = mode
                        app._apply_layout()
                        await pilot.pause()
                        self.assertEqual(
                            bool(app.query_one('#left_pane').display), table)
                        self.assertEqual(
                            bool(app.query_one('#right_pane_scroll').display),
                            preview)
                        self.assertEqual(
                            bool(app.query_one('#jobs_scroll').display), jobs)
                        # An empty #upper still reserves 1fr of the body --
                        # and the queue is inside it now, so it counts.
                        self.assertEqual(
                            bool(app.query_one('#upper').display),
                            table or preview or jobs)

    async def test_a_table_with_no_preview_uses_the_whole_width(self):
        """Hiding the preview is pointless if the table keeps its 56%."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(130, 30)) as pilot:
                await pilot.pause()
                split = app.query_one('#left_pane').region.width
                await pilot.press('z')          # -> wide
                await pilot.pause()
                wide = app.query_one('#left_pane').region.width
                self.assertGreater(wide, split)
                self.assertGreaterEqual(wide, 129)

    async def test_expanded_jobs_outgrow_the_fourteen_line_cap(self):
        """The cap protects the table. With the table gone it only wastes
        most of the terminal."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                normal = app.query_one('#jobs_scroll').region.height
                self.assertLessEqual(normal, 14)
                app.layout_mode = 'jobs'
                app._apply_layout()
                await pilot.pause()
                self.assertGreater(
                    app.query_one('#jobs_scroll').region.height, 14)

    async def test_jobs_only_hands_the_arrow_keys_to_the_queue(self):
        """A long squeue is unreadable if only the mouse can scroll it.

        That was the reasoning, and it was applied to one layout out of four
        -- so in the other three the queue was exactly as unreadable, and the
        preview beside it never got the argument at all. The queue is
        reachable wherever it is drawn now; what stays true of *this* mode is
        that with nothing else on screen it should already have the keyboard,
        without anyone having to ask.
        """
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                jobs = app.query_one('#jobs_scroll')
                # Reachable beside the table, but not holding the keyboard:
                # the list is what a person came here to read.
                self.assertTrue(jobs.can_focus)
                self.assertTrue(app.table.has_focus)

                app.layout_mode = 'jobs'
                app._apply_layout()
                await pilot.pause()
                self.assertTrue(jobs.can_focus)
                self.assertTrue(jobs.has_focus,
                                'the only pane on screen must start with the '
                                'keyboard')

                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                self.assertTrue(jobs.can_focus)
                self.assertTrue(app.table.has_focus,
                                'coming back, the list takes the arrows again')

    async def test_a_hidden_preview_stops_costing_ssh_round_trips(self):
        """Capturing a pane nobody can see is a per-tick SSH round trip spent
        on nothing -- over a slow login gateway that is the expensive one."""
        calls = []

        async def record(node, session):
            calls.append((node, session))
            return {'ok': True, 'content': 'hello'}

        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app._spawn_preview_capture = record
                # Pin the selection: the attention ordering puts an offline
                # placeholder first, and placeholders are never captured.
                app.selected_node = 'gpu1'
                app.selected_session = 'train'
                app._selection_changed_at = 0.0

                app.layout_mode = 'table'
                app._apply_layout()
                calls.clear()
                for _ in range(20):
                    await asyncio.sleep(0.05)
                    await pilot.pause()
                self.assertEqual(calls, [], 'hidden preview kept fetching')

                app.layout_mode = 'split'
                app._apply_layout()
                for _ in range(60):
                    await asyncio.sleep(0.05)
                    await pilot.pause()
                    if calls:
                        break
                self.assertTrue(calls, 'preview never resumed once shown again')

    async def test_a_phone_gets_the_whole_width_for_the_table(self):
        """Measured on an iPhone: 58 columns. The split view hands 44% of
        those to a preview too narrow to read anything in, and the table is
        left with 32 -- `tu_improve` renders as `tu_impr` and LEFT, LOAD and
        STATUS disappear. That is the desktop layout on the wrong screen."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(58, 30)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                self.assertFalse(app.query_one('#right_pane_scroll').display)
                self.assertEqual(app.query_one('#left_pane').region.width, 58)

    async def test_a_wide_terminal_still_gets_its_preview(self):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(130, 30)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                self.assertTrue(app.query_one('#right_pane_scroll').display)

    async def test_the_threshold_leaves_the_table_the_columns_it_needs(self):
        """The number is the one already in the CSS -- ~66 cells -- over the
        56% the table gets. Below it STATUS is cut mid-word."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(
                    size=(autotmux._MIN_SPLIT_WIDTH, 30)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                self.assertTrue(app.query_one('#right_pane_scroll').display)
                self.assertGreaterEqual(
                    app.query_one('#left_pane').region.width, 66)

    async def test_rotating_the_phone_moves_the_preview_rather_than_losing_it(
            self):
        """A phone changes shape without anyone pressing a key, and the two
        shapes have room in different directions: upright there are rows to
        spare and no columns, on its side the opposite."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(58, 40)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                upper = app.query_one('#upper')
                self.assertTrue(app.query_one('#right_pane_scroll').display)
                self.assertTrue(upper.has_class('-stacked'))
                # Underneath means the table keeps the whole width.
                self.assertTrue(app.query_one('#left_column').has_class('-full'))
                await pilot.resize_terminal(130, 24)
                await pilot.pause()
                self.assertTrue(app.query_one('#right_pane_scroll').display)
                self.assertFalse(upper.has_class('-stacked'))
                self.assertFalse(
                    app.query_one('#left_column').has_class('-full'))

    async def test_a_screen_with_room_in_neither_direction_keeps_the_table(self):
        """The rule the width test guarded is still there underneath: a
        terminal too narrow to split and too short to stack spends what it
        has on the list."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(58, 24)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                self.assertFalse(app.query_one('#right_pane_scroll').display)
                self.assertFalse(app.query_one('#upper').has_class('-stacked'))

    async def test_the_stacked_table_stops_growing_before_it_squeezes_out(self):
        """`height: auto` is what removes the void -- 20 sessions no longer
        sit in a 50-row pane. The cap is what stops the other end: a hundred
        sessions must not push the preview off the bottom of the screen."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td, sessions=[('s%03d' % n, 1, 0) for n in range(120)])
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(66, 75)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
                preview = app.query_one('#right_pane_scroll')
                self.assertTrue(preview.display)
                # Still on the screen, and still worth reading in.
                self.assertGreaterEqual(preview.region.height, 12)

    async def test_the_narrow_view_is_not_a_mode_and_is_not_remembered(self):
        """A small screen is a property of the screen. Persisting it would
        follow the user to a desktop and hide the preview there."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=(58, 30)) as pilot:
                await pilot.pause()
                app.layout_mode = 'split'
                app._apply_layout()
                await pilot.pause()
            self.assertIn(autotmux.config.load_layout(),
                          ('split', autotmux.config.LAYOUT_DEFAULT))

    async def test_the_layout_is_remembered_for_the_next_run(self):
        """It is chosen to fit a terminal, and that is usually the same
        terminal tomorrow."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press('z')
                await pilot.pause()
                self.assertEqual(app.layout_mode, 'wide')
            self.assertEqual(autotmux.config.load_layout(), 'wide')
            self.assertEqual(autotmux.AutotmuxApp().layout_mode, 'wide')

    async def test_a_remembered_layout_is_what_gets_painted_first(self):
        """Reading it after mount would flash the default and rearrange."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            autotmux.config.save_layout('table')
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(app.query_one('#jobs_scroll').display)
                self.assertFalse(app.query_one('#right_pane_scroll').display)

    def test_an_unreadable_preference_never_stops_the_app_starting(self):
        with mock.patch.object(autotmux.config, 'load_layout',
                               side_effect=OSError('nfs is stuck')):
            self.assertEqual(autotmux.AutotmuxApp().layout_mode,
                             autotmux.config.LAYOUT_DEFAULT)

    def test_an_unknown_mode_falls_back_instead_of_hiding_everything(self):
        """A hand-edited file must not be able to produce a blank screen."""
        self.assertEqual(autotmux.layout_spec('nonsense'),
                         autotmux.layout_spec(autotmux.config.LAYOUT_DEFAULT))
        self.assertEqual(autotmux.layout_spec(None)['table'], True)

    def test_every_mode_leaves_something_on_screen(self):
        for mode in autotmux.config.LAYOUT_MODES:
            with self.subTest(mode=mode):
                spec = autotmux.layout_spec(mode)
                self.assertTrue(spec['table'] or spec['preview']
                                or spec['jobs'])
                self.assertTrue(spec['label'])

    async def test_the_key_that_left_the_footer_is_advertised_where_it_acts(self):
        """`j` gave up its footer slot to `z`. Hiding it is only safe because
        the panel it controls prints the key in its own title -- if that stops
        being true, the key becomes undiscoverable outside `?`."""
        by_key = {b.key: b for b in autotmux.AutotmuxApp.BINDINGS}
        self.assertFalse(by_key['j'].show)
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # The bracket went with the three-shapes-of-one-heading
                # cleanup; being advertised where it acts did not.
                self.assertIn('j: switch', str(app.jobs_view.render()))
                await pilot.press('j')
                await pilot.pause()
                # The bracket went with the three-shapes-of-one-heading
                # cleanup; being advertised where it acts did not.
                self.assertIn('j: switch', str(app.jobs_view.render()))

    def test_only_the_jobs_only_mode_expands_the_queue(self):
        """Expanding it while the table is up would push the table out."""
        for mode in autotmux.config.LAYOUT_MODES:
            spec = autotmux.layout_spec(mode)
            if spec['expand_jobs']:
                self.assertFalse(spec['table'], mode)
                self.assertFalse(spec['preview'], mode)


class ConnectionClusterEditingTests(unittest.IsolatedAsyncioTestCase):
    """`g` has to manage every cluster, not just the primary one.

    Editing one cluster at a time is fine; silently dropping the others when
    the dialog saves is not, and that is what it used to do.
    """

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

    def _settings(self, **overrides):
        settings = dict(autotmux.config.CLIENT_DEFAULTS)
        settings.update({
            'mode': 'gateway',
            'gateways': ['k6', 'k7'],
            'agent_command': ['atmux-agent'],
            'clusters': {'zgx': {
                'gateways': ['zgx'],
                'agent_command': ['/opt/venv/bin/atmux-agent'],
                'control_path': '',
            }},
        })
        settings.update(overrides)
        return settings

    @asynccontextmanager
    async def _dialog(self, settings, aliases=('k6', 'k7', 'k8')):
        app = autotmux.AutotmuxApp()
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                dialog = autotmux.ConnectionManager(settings, list(aliases))
                await app.push_screen(dialog)
                await pilot.pause()
                yield dialog, pilot

    async def test_every_configured_cluster_is_offered(self):
        async with self._dialog(self._settings()) as (dialog, _pilot):
            self.assertEqual(list(dialog._clusters),
                             [autotmux.config.PRIMARY_CLUSTER, 'zgx'])
            select = dialog.query_one('#connection_cluster', autotmux.Select)
            self.assertEqual(select.value, autotmux.config.PRIMARY_CLUSTER)

    async def test_saving_after_editing_only_the_primary_keeps_the_rest(self):
        """The bug: the dialog wrote {mode, gateways, agent_command} and every
        other cluster went with it."""
        async with self._dialog(self._settings()) as (dialog, pilot):
            saved = {}
            dialog.dismiss = lambda result: saved.update(result or {})
            dialog.query_one(
                '#connection_aliases', autotmux.SelectionList).select('k8')
            dialog.action_save()
            await pilot.pause()
            self.assertEqual(sorted(saved['gateways']), ['k6', 'k7', 'k8'])
            self.assertIn('zgx', saved['clusters'])
            self.assertEqual(saved['clusters']['zgx']['gateways'], ['zgx'])

    async def test_settings_the_dialog_cannot_edit_survive_a_save(self):
        """control_path is not on screen anywhere. Round-tripping it is the
        only thing stopping a save from deleting it."""
        async with self._dialog(self._settings()) as (dialog, pilot):
            saved = {}
            dialog.dismiss = lambda result: saved.update(result or {})
            dialog.action_save()
            await pilot.pause()
            self.assertEqual(saved['clusters']['zgx']['control_path'], '')
            self.assertEqual(saved['clusters']['zgx']['agent_command'],
                             ['/opt/venv/bin/atmux-agent'])

    async def test_switching_cluster_shows_that_cluster_selection(self):
        async with self._dialog(self._settings()) as (dialog, pilot):
            aliases = dialog.query_one(
                '#connection_aliases', autotmux.SelectionList)
            self.assertEqual(sorted(aliases.selected), ['k6', 'k7'])
            dialog.query_one(
                '#connection_cluster', autotmux.Select).value = 'zgx'
            await pilot.pause()
            self.assertEqual(dialog._current, 'zgx')
            self.assertEqual(list(aliases.selected), ['zgx'])
            self.assertEqual(
                dialog.query_one('#connection_agent', autotmux.Input).value,
                '/opt/venv/bin/atmux-agent')

    async def test_edits_to_one_cluster_survive_switching_away_and_back(self):
        async with self._dialog(self._settings()) as (dialog, pilot):
            aliases = dialog.query_one(
                '#connection_aliases', autotmux.SelectionList)
            aliases.select('k8')
            dialog.query_one(
                '#connection_cluster', autotmux.Select).value = 'zgx'
            await pilot.pause()
            dialog.query_one('#connection_cluster', autotmux.Select).value = (
                autotmux.config.PRIMARY_CLUSTER)
            await pilot.pause()
            self.assertEqual(sorted(aliases.selected), ['k6', 'k7', 'k8'])

    async def test_a_new_cluster_can_be_added_and_filled_in(self):
        async with self._dialog(self._settings()) as (dialog, pilot):
            dialog.query_one(
                '#connection_new_cluster', autotmux.Input).value = 'lab'
            dialog.action_add_cluster()
            await pilot.pause()
            self.assertEqual(dialog._current, 'lab')
            self.assertEqual(
                list(dialog.query_one('#connection_aliases',
                                      autotmux.SelectionList).selected), [])
            dialog.query_one('#connection_extra', autotmux.Input).value = 'ws'
            saved = {}
            dialog.dismiss = lambda result: saved.update(result or {})
            dialog.action_save()
            await pilot.pause()
            self.assertEqual(saved['clusters']['lab']['gateways'], ['ws'])
            # The primary is untouched by adding a cluster.
            self.assertEqual(sorted(saved['gateways']), ['k6', 'k7'])

    async def test_a_bad_cluster_name_is_refused_with_a_reason(self):
        async with self._dialog(self._settings()) as (dialog, pilot):
            for name in ('bad name', '', '-lead', 'x' * 40):
                with self.subTest(name=name):
                    dialog.query_one(
                        '#connection_new_cluster', autotmux.Input).value = name
                    dialog.action_add_cluster()
                    await pilot.pause()
                    self.assertNotIn(name, dialog._clusters)
                    self.assertIn(
                        'cluster name',
                        str(dialog.query_one('#connection_status',
                                             autotmux.Label).render()))

    async def test_a_duplicate_cluster_name_is_refused(self):
        async with self._dialog(self._settings()) as (dialog, pilot):
            dialog.query_one(
                '#connection_new_cluster', autotmux.Input).value = 'zgx'
            dialog.action_add_cluster()
            await pilot.pause()
            self.assertEqual(list(dialog._clusters),
                             [autotmux.config.PRIMARY_CLUSTER, 'zgx'])

    async def test_a_cluster_can_be_removed_but_never_the_primary(self):
        async with self._dialog(self._settings()) as (dialog, pilot):
            remove = dialog.query_one('#connection_remove', autotmux.Button)
            self.assertTrue(remove.disabled, 'primary must not be removable')
            dialog.query_one(
                '#connection_cluster', autotmux.Select).value = 'zgx'
            await pilot.pause()
            self.assertFalse(remove.disabled)
            dialog.action_remove_cluster()
            await pilot.pause()
            self.assertEqual(list(dialog._clusters),
                             [autotmux.config.PRIMARY_CLUSTER])
            self.assertEqual(dialog._current,
                             autotmux.config.PRIMARY_CLUSTER)

    async def test_the_explanation_is_wrapped_rather_than_clipped(self):
        """Label defaults to width:auto, which lays text out on one line and
        lets the container cut the rest. The explanation of what a cluster
        *is* is the one thing in this dialog that must not be half-shown."""
        async with self._dialog(self._settings()) as (dialog, _pilot):
            help_label = dialog.query_one('#connection_help')
            text = str(help_label.render())
            width = help_label.region.width
            self.assertGreater(width, 0)
            # At least as many lines as the text needs to fit that width.
            self.assertGreaterEqual(help_label.region.height,
                                    max(1, len(text) // width))

    async def test_an_empty_primary_is_refused_not_saved(self):
        async with self._dialog(
                self._settings(gateways=[], clusters={})) as (dialog, pilot):
            dismissed = []
            dialog.dismiss = lambda result: dismissed.append(result)
            dialog.action_save()
            await pilot.pause()
            self.assertEqual(dismissed, [])
            self.assertIn('at least one',
                          str(dialog.query_one('#connection_status',
                                               autotmux.Label).render()))


class MousePreferenceTests(unittest.TestCase):
    """Mouse reporting is what makes click-to-attach work and what stops the
    terminal selecting text, so the choice has to survive without a flag."""

    def _want(self, body='[client]\n', *, no_mouse=False, mouse=False,
              remote=False):
        args = SimpleNamespace(no_mouse=no_mouse, mouse=mouse)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'config.toml')
            with open(path, 'w') as handle:
                handle.write(body)
            with mock.patch.object(autotmux.config, 'CONFIG_PATH', path), \
                 mock.patch.object(autotmux, '_is_remote_session',
                                   return_value=remote):
                return autotmux._want_mouse(args)

    def test_default_is_on_locally_and_off_over_ssh(self):
        self.assertTrue(self._want())
        self.assertFalse(self._want(remote=True))

    def test_the_preference_is_honoured(self):
        self.assertFalse(self._want('[client]\nmouse = "off"\n'))
        self.assertTrue(self._want('[client]\nmouse = "on"\n', remote=True))

    def test_a_flag_still_wins_for_one_run(self):
        self.assertTrue(self._want('[client]\nmouse = "off"\n', mouse=True))
        self.assertFalse(self._want('[client]\nmouse = "on"\n', no_mouse=True))

    def test_an_invalid_preference_falls_back_to_auto(self):
        self.assertTrue(self._want('[client]\nmouse = "banana"\n'))
        self.assertTrue(self._want('[client]\nmouse = 1\n'))

    def test_an_unreadable_config_does_not_break_startup(self):
        self.assertTrue(self._want('[client\nbroken'))


class KeyDiscoverabilityTests(unittest.IsolatedAsyncioTestCase):
    """A key the user cannot interpret is a key they will not use."""

    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch

    def test_attach_is_advertised_on_the_table_itself(self):
        """The focused widget's bindings win, so an App-level entry for enter
        would never reach the footer -- it has to live on the table."""
        binding = next(
            b for b in autotmux.ClickToAttachDataTable.BINDINGS
            if b.key == 'enter')
        self.assertEqual(binding.description, 'Attach')
        self.assertTrue(binding.show)

    def test_every_footer_label_names_what_it_acts_on(self):
        """"Shell" and "Local Shell" gave no clue which machine they land on."""
        # Every label, not only the advertised ones: a key demoted out of the
        # footer still appears in `?`, and that is where it has to read well.
        labelled = {b.key: b.description for b in autotmux.AutotmuxApp.BINDINGS}
        self.assertEqual(labelled['s'], 'SSH to node')
        # Shortened from "Attach in new window", which alone pushed the footer
        # past 120 columns and made Textual clip "q Quit" to "q Q".
        self.assertEqual(labelled['o'], 'New window')
        self.assertEqual(labelled['k'], 'Auto-renew job')
        self.assertEqual(labelled['g'], 'Clusters')
        shown = {b.key: b.description
                 for b in autotmux.AutotmuxApp.BINDINGS if b.show}
        # Seven eligible and never seven at once: `esc` is retired by
        # check_action on the list, where there is no back from the list.
        # Six are drawn, and the other seventeen are in `?`, on the phone's
        # keypad, and in `:` -- and all of them still work.
        self.assertLessEqual(len(shown), 7)
        self.assertIn('escape', shown)
        self.assertIn('slash', shown)
        for description in labelled.values():
            self.assertEqual(description, description.strip())
            self.assertTrue(description[:1].isupper() or description[:1] == '↑')

    def test_the_footer_fits_a_realistic_terminal(self):
        """A clipped footer turns "q Quit" into "q Q", which reads as a typo.

        Textual lays each shown binding out as `key description` with a space
        either side and clips whatever overflows, silently. This measures the
        same arithmetic so a future label cannot quietly push a key off the end.
        """
        # The footer prints the key *symbol*, not the binding's name, so
        # `question_mark` costs one column and not thirteen.
        symbols = {'enter': '⏎', 'question_mark': '?'}
        shown = [b for b in autotmux.AutotmuxApp.BINDINGS if b.show]
        width = sum(len(symbols.get(b.key, b.key)) + 1 + len(b.description) + 2
                    for b in shown)
        # `enter` is advertised by the table, not the app, so add it back.
        width += len('⏎') + 1 + len('Attach') + 2
        self.assertLessEqual(width, 110, f'footer needs {width} columns')

    def test_the_command_palette_is_off(self):
        """It sat rightmost in the footer, pushing the app's own keys off, and
        was the one entry `?` could not explain -- this app registers no
        commands, so it only ever offered Textual's built-ins."""
        self.assertFalse(autotmux.AutotmuxApp.ENABLE_COMMAND_PALETTE)

    def test_the_handover_says_what_you_are_looking_at_and_how_to_return(self):
        """A session that finished hours ago paints one static screen. With
        the table gone and nothing naming it, that is indistinguishable from a
        hung dashboard -- which is exactly how it was misread in practice."""
        banner = autotmux._handover_banner('holygpu8a17504', '4gpu')
        self.assertIn('4gpu', banner)
        self.assertIn('holygpu8a17504', banner)
        self.assertIn('d', banner)
        self.assertIn('dashboard', banner)

    def test_the_shell_handover_says_exit_rather_than_detach(self):
        """`s` opens a plain shell: there is nothing to detach from."""
        banner = autotmux._handover_banner(
            'gpu1', autotmux._START_SHELL_SESSION)
        self.assertIn('exit', banner)
        self.assertNotIn('detach', banner)

    def test_the_banner_never_leaks_a_placeholder_name(self):
        for session in (autotmux._START_SHELL_SESSION, autotmux._OFFLINE_SESSION):
            banner = autotmux._handover_banner('gpu1', session)
            self.assertNotIn('\x00', banner)

    def test_rare_keys_are_hidden_but_still_bound(self):
        by_key = {b.key: b for b in autotmux.AutotmuxApp.BINDINGS}
        for key in ('r', 't'):
            with self.subTest(key=key):
                self.assertIn(key, by_key)
                self.assertFalse(by_key[key].show)

    def test_help_documents_every_bound_key(self):
        documented = {row[0] for row in autotmux.AutotmuxApp.HELP_ROWS}
        bound = {b.key for b in autotmux.AutotmuxApp.BINDINGS
                 if b.key != 'question_mark'}
        missing = {k for k in bound if k not in documented}
        self.assertEqual(missing, set(), f'undocumented keys: {missing}')
        self.assertIn('Enter', documented)

    def test_help_rows_say_what_each_key_acts_on(self):
        for key, does, note in autotmux.AutotmuxApp.HELP_ROWS:
            with self.subTest(key=key):
                self.assertTrue(key and does and note)
                self.assertLessEqual(len(does), 50)
                self.assertLessEqual(len(note), 16)

    def test_help_explains_the_model_before_the_keys(self):
        """The keys only make sense once it is clear the sessions are
        elsewhere and outlive the connection."""
        intro = autotmux.AutotmuxApp.HELP_INTRO.lower()
        self.assertIn('compute node', intro)
        self.assertIn('after you disconnect', intro)

    def test_the_four_connecting_keys_are_grouped_and_distinguished(self):
        """Enter/click/o/s/t all "connect"; what differs is where they land
        and whether the thing survives leaving."""
        connect = dict(
            (row[0], row[2])
            for title, rows in autotmux.AutotmuxApp.HELP_SECTIONS
            if title == 'Connect' for row in rows)
        self.assertEqual(set(connect), {'Enter', 'click', 'o', 's', 't'})
        self.assertEqual(connect['s'], 'dies on exit')
        self.assertEqual(connect['Enter'], 'survives')

    def test_the_non_obvious_columns_are_explained(self):
        documented = {name for name, _ in autotmux.AutotmuxApp.HELP_COLUMNS}
        self.assertEqual(documented,
                         {'IDLE', 'LEFT', 'LOAD', '·N', 'STATUS'})

    async def test_question_mark_opens_and_closes_help(self):
        app = autotmux.AutotmuxApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, autotmux.HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, autotmux.HelpScreen)

    async def test_help_cannot_be_stacked_onto_itself(self):
        app = autotmux.AutotmuxApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("question_mark")
                await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, autotmux.HelpScreen)


class WarnedJobPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'warned.json')
        self.patcher = mock.patch.object(
            autotmux, '_WARNED_JOBS_FILE', self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_round_trip(self):
        autotmux._save_warned_jobs({'1', '2'})
        self.assertEqual(autotmux._load_warned_jobs(), {'1', '2'})

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(autotmux._load_warned_jobs(), set())

    def test_corrupt_file_is_ignored(self):
        for junk in ('not json', '{"a": 1}', '[[1]]', ''):
            with open(self.path, 'w') as handle:
                handle.write(junk)
            with self.subTest(junk=junk):
                self.assertIsInstance(autotmux._load_warned_jobs(), set)

    def test_an_unwritable_path_never_raises(self):
        with mock.patch.object(autotmux, '_WARNED_JOBS_FILE',
                               '/nonexistent-dir/warned.json'):
            autotmux._save_warned_jobs({'1'})     # must not raise

    def test_the_record_is_bounded(self):
        autotmux._save_warned_jobs({str(i) for i in range(10_000)})
        self.assertLessEqual(
            len(autotmux._load_warned_jobs()), autotmux._WARNED_JOBS_LIMIT)


class StatusSubtitleTests(unittest.TestCase):
    """`_status_subtitle` picks the message for an empty dashboard.

    A gateway client runs no local daemon, so it must never be told to go
    inspect one.  The gateway marker only appears once a fetch has landed, so
    the mode has to come from how the process was configured.
    """

    @staticmethod
    def _subtitle(state, *, gateway_mode, crash_looping=False,
                  recovery_inflight=False):
        stub = SimpleNamespace(
            _crash_looping=crash_looping,
            _recovery_inflight=recovery_inflight,
            _daemon_age_seconds=lambda *a, **k: 0.0,
        )
        with mock.patch.object(autotmux, '_gateway_mode',
                               return_value=gateway_mode):
            return autotmux.AutotmuxApp._status_subtitle(
                stub, state, [], '?')

    def test_gateway_client_never_points_at_a_local_daemon(self):
        """The regression: before the first RPC lands the state has no gateway
        marker, which used to fall through to the native branch."""
        subtitle = self._subtitle({}, gateway_mode=True)
        self.assertNotIn('daemon', subtitle)
        self.assertNotIn('atd status', subtitle)
        self.assertIn('gateway', subtitle)

    def test_gateway_failure_reports_the_reason(self):
        state = {'nodes': {}, 'gateway': {
            'mode': 'gateway', 'last_error': 'gateway RPC timed out'}}
        subtitle = self._subtitle(state, gateway_mode=True)
        self.assertIn('gateway unavailable', subtitle)
        self.assertIn('gateway RPC timed out', subtitle)

    def test_gateway_failure_falls_back_to_a_generic_reason(self):
        state = {'nodes': {}, 'gateway': {'mode': 'gateway'}}
        self.assertIn('no login gateway is reachable',
                      self._subtitle(state, gateway_mode=True))

    def test_pending_fetch_is_distinct_from_a_failed_one(self):
        """'Not answered yet' and 'answered with an error' must not read the
        same, or a slow first connect looks like an outage."""
        pending = self._subtitle({}, gateway_mode=True)
        failed = self._subtitle(
            {'nodes': {}, 'gateway': {'mode': 'gateway',
                                      'last_error': 'boom'}},
            gateway_mode=True)
        self.assertNotEqual(pending, failed)
        self.assertNotIn('⚠', pending)
        self.assertIn('⚠', failed)

    def test_native_mode_still_waits_on_its_local_daemon(self):
        subtitle = self._subtitle({}, gateway_mode=False)
        self.assertIn('waiting for daemon', subtitle)
        self.assertIn('atd status', subtitle)

    def test_native_mode_reports_an_in_flight_restart(self):
        self.assertEqual(
            self._subtitle({}, gateway_mode=False, recovery_inflight=True),
            'starting daemon…')

    def test_crash_loop_wins_over_every_other_message(self):
        for gateway_mode in (True, False):
            with self.subTest(gateway_mode=gateway_mode):
                self.assertIn('crash-looping', self._subtitle(
                    {}, gateway_mode=gateway_mode, crash_looping=True))

    def test_populated_gateway_state_is_unaffected(self):
        """Nodes present -> the empty-dashboard branch must not fire at all."""
        state = {
            'nodes': SYNTH_STATE['nodes'],
            'gateway': {'mode': 'gateway', 'active': 'login1'},
            'updated': '2026-01-01 00:00:00',
        }
        subtitle = self._subtitle(state, gateway_mode=True)
        self.assertNotIn('waiting for daemon', subtitle)
        self.assertNotIn('connecting to login gateway', subtitle)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class NarrowScreenTests(unittest.TestCase):
    """A phone is 58 columns. Everything sized for a desktop has to give."""

    def test_squeue_loses_the_indent_every_line_shares(self):
        """squeue right-aligns JOBID in a field wide enough for any of them,
        so ~10 leading spaces arrive on every row -- a sixth of a phone
        screen spent on nothing."""
        raw = ('             JOBID PARTITION\n'
               '          37442487 kempner_h\n'
               '          37442463 kempner_h\n')
        out = autotmux._dedent_block(raw).splitlines()
        # The data rows carry the smallest indent, so they end up flush and
        # the header keeps the three columns that align it over them.
        self.assertEqual(out[1], '37442487 kempner_h')
        self.assertEqual(out[0], '   JOBID PARTITION')

    def test_columns_stay_aligned_with_each_other(self):
        """Only whitespace common to every line goes; taking more would shear
        the columns apart."""
        self.assertEqual(autotmux._dedent_block('   a\n     b\n'), 'a\n  b')

    def test_text_with_no_common_indent_is_untouched(self):
        self.assertEqual(autotmux._dedent_block('a\n   b\n'), 'a\n   b\n')

    def test_junk_does_not_raise(self):
        for value in ('', None, 42, '\n\n'):
            with self.subTest(value=value):
                self.assertIsInstance(autotmux._dedent_block(value), str)

    def test_the_jobs_panel_clips_rather_than_wraps(self):
        """squeue prints ~95 columns. Wrapping them onto a phone interleaves
        each row with its own continuation, which is harder to read than
        losing the tail -- the leading columns are the ones worth seeing."""
        self.assertIn('text-wrap: nowrap', autotmux.AutotmuxApp.CSS)
        self.assertIn('overflow-x: auto', autotmux.AutotmuxApp.CSS)


class WebDashboardScreenTests(unittest.TestCase):
    """`w` answers "where is it, and is it up" without anyone keeping four
    commands across two machines in their head. A tool you have to look up is
    a tool you stop reaching for."""

    def test_the_key_is_bound_and_documented(self):
        by_key = {b.key: b for b in autotmux.AutotmuxApp.BINDINGS}
        self.assertIn('w', by_key)
        documented = {row[0] for row in autotmux.AutotmuxApp.HELP_ROWS}
        self.assertIn('w', documented)

    def test_the_serve_url_is_read_from_config_not_from_prose(self):
        """After a tailnet rename the human-readable output kept printing the
        old hostname, and a URL that looks right and 404s is worse than
        none."""
        raw = ('{"TCP":{"443":{"HTTPS":true}},"Web":{'
               '"zgx.shorthair-cat.ts.net:443":{"Handlers":{"/":'
               '{"Proxy":"http://127.0.0.1:7681"}}}}}')
        self.assertEqual(autotmux.webcontrol.parse_serve_url(raw),
                         'https://zgx.shorthair-cat.ts.net/')

    def test_a_non_default_port_survives_into_the_url(self):
        raw = ('{"Web":{"mac.shorthair-cat.ts.net:8080":{"Handlers":{"/":'
               '{"Proxy":"http://127.0.0.1:7681"}}}}}')
        self.assertEqual(autotmux.webcontrol.parse_serve_url(raw),
                         'http://mac.shorthair-cat.ts.net:8080/')

    def test_a_rule_for_some_other_port_is_not_claimed(self):
        raw = ('{"Web":{"h.ts.net:443":{"Handlers":{"/":'
               '{"Proxy":"http://127.0.0.1:9999"}}}}}')
        self.assertEqual(autotmux.webcontrol.parse_serve_url(raw, 7681), '')

    def test_junk_from_either_command_is_survivable(self):
        for raw in ('', 'not json', '{}', 'null', '{"Web":42}', '[]'):
            with self.subTest(raw=raw):
                self.assertEqual(autotmux.webcontrol.parse_serve_url(raw), '')
                self.assertEqual(autotmux.webcontrol.parse_tailnet_host(raw), '')

    def test_the_hostname_loses_its_trailing_dot(self):
        """MagicDNS names are fully qualified; a URL with the dot works but
        looks wrong enough that people retype it."""
        raw = '{"Self":{"DNSName":"zgx.shorthair-cat.ts.net."}}'
        self.assertEqual(autotmux.webcontrol.parse_tailnet_host(raw),
                         'zgx.shorthair-cat.ts.net')

    def test_the_summary_distinguishes_up_from_reachable(self):
        """Running and reachable are different failures with different fixes,
        and the second one is the confusing one."""
        self.assertEqual(
            autotmux.webcontrol.summary({'listening': False}), 'stopped')
        self.assertIn('reachable', autotmux.webcontrol.summary(
            {'listening': True, 'url': 'https://h/'}))
        self.assertIn('local only', autotmux.webcontrol.summary(
            {'listening': True, 'url': ''}))

    def test_an_unknown_verb_is_refused(self):
        ok, message = autotmux.webcontrol.control('destroy')
        self.assertFalse(ok)
        self.assertIn('destroy', message)

    def test_the_shell_commands_are_shown_for_whichever_setup_exists(self):
        with_unit = dict(autotmux.webcontrol.commands({'systemd': True}))
        self.assertIn('start', with_unit)
        self.assertIn('logs', with_unit)
        without = dict(autotmux.webcontrol.commands(
            {'systemd': False, 'port': 7681}))
        self.assertIn('atmux-web', without['start'])


class TouchControlSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """Whoever can draw the controls draws them, and only one of them does.

    Textual's Footer packs every binding onto one line. On a phone that is a
    row of six-pixel targets, and past the fourth binding it is off the edge
    entirely -- so a touch client with no other surface gets buttons in the
    grid instead. A browser draws its own outside the grid and gets neither
    footer nor bar, because two control surfaces is how one action ends up in
    two places with two labels.
    """

    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')
        self._saved_prewarm = autotmux.AutotmuxApp._prewarm_interactive_async

        async def no_prewarm(_app, _nodes, _source_pool):
            return None

        autotmux.AutotmuxApp._prewarm_interactive_async = no_prewarm
        self._saved_env = os.environ.get(autotmux.keypad.TOUCH_ENV)
        # The layout is persisted, so without this the test reads whichever
        # mode the developer last left the real dashboard in -- and in the
        # queue-only mode the table is not on screen, so Attach is not in the
        # live bindings and every assertion here is about a different screen.
        self._layout_dir = tempfile.TemporaryDirectory()
        self._saved_layout_path = autotmux.config.LAYOUT_PATH
        autotmux.config.LAYOUT_PATH = os.path.join(
            self._layout_dir.name, 'layout.json')

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch
        autotmux.AutotmuxApp._prewarm_interactive_async = self._saved_prewarm
        autotmux.config.LAYOUT_PATH = self._saved_layout_path
        self._layout_dir.cleanup()
        if self._saved_env is None:
            os.environ.pop(autotmux.keypad.TOUCH_ENV, None)
        else:
            os.environ[autotmux.keypad.TOUCH_ENV] = self._saved_env

    @asynccontextmanager
    async def _app(self, mode, size=(68, 45)):
        if mode:
            os.environ[autotmux.keypad.TOUCH_ENV] = mode
        else:
            os.environ.pop(autotmux.keypad.TOUCH_ENV, None)
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                # Wait for the condition, not for a duration. Attach is bound
                # on the table, so it only joins the live set once focus has
                # settled there -- and a fixed sleep that is long enough on
                # one machine is a test that fails on another for no reason
                # anybody can act on.
                # Wait for the *bar*, not for the app's bindings. The bar
                # follows on its own poll, so a test that clicks as soon as
                # the binding exists can catch it mid-rebuild -- which is
                # how a press aimed at one button reached another.
                bars = app.query(autotmux.TouchBar)
                if bars:
                    await self._until(pilot, lambda: 'Attach' in [
                        str(b.label) for b in bars.first().query(
                            autotmux.Button)])
                else:
                    await self._until(pilot, lambda: 'Attach' in [
                        k['l'] for k in
                        autotmux.keypad.keys_for(app.active_bindings)])
                yield app, pilot

    async def _until(self, pilot, predicate, timeout=5.0):
        """Pump the app until predicate() holds, or fail saying what did not."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await pilot.pause()
            if predicate():
                await pilot.pause()
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f'condition never held within {timeout}s')

    async def test_a_keyboard_terminal_is_left_exactly_as_it_was(self):
        async with self._app('') as (app, _pilot):
            self.assertTrue(app.query(autotmux.Footer))
            self.assertFalse(app.query(autotmux.TouchBar))

    async def test_a_browser_client_gets_neither_because_it_draws_its_own(self):
        async with self._app('web') as (app, _pilot):
            self.assertFalse(app.query(autotmux.Footer))
            self.assertFalse(app.query(autotmux.TouchBar))

    async def test_a_touch_client_with_no_surface_gets_buttons_in_the_grid(self):
        async with self._app('local') as (app, _pilot):
            self.assertFalse(app.query(autotmux.Footer))
            bar = app.query_one(autotmux.TouchBar)
            labels = [str(b.label) for b in bar.query(autotmux.Button)]
            self.assertIn('Attach', labels)
            self.assertIn('Layout', labels)
            # The keys that are traps or duplicates under a thumb.
            self.assertNotIn('Quit', labels)
            self.assertNotIn('Focus Next', labels)

    async def test_the_buttons_are_hittable(self):
        """44pt is Apple's minimum. One character is about 6x12 pixels at the
        size the layout picks for a phone, so a button has to be several
        cells in both directions to be a target at all."""
        async with self._app('local') as (app, _pilot):
            bar = app.query_one(autotmux.TouchBar)
            for button in bar.query(autotmux.Button):
                with self.subTest(label=str(button.label)):
                    self.assertGreaterEqual(button.outer_size.width, 8)
                    self.assertGreaterEqual(button.outer_size.height, 3)

    async def test_a_button_runs_the_action_its_label_names(self):
        """The one that would silently fail: Attach is bound on the table, so
        running it against the app looks for an action that is not there."""
        async with self._app('local') as (app, pilot):
            bar = app.query_one(autotmux.TouchBar)
            before = app.layout_mode
            await pilot.click(next(b for b in bar.query(autotmux.Button)
                                   if str(b.label) == 'Layout'))
            await pilot.pause()
            self.assertNotEqual(app.layout_mode, before)

    async def test_the_bar_follows_the_screen_it_is_on(self):
        """A modal has its own bindings, and a bar still showing the
        dashboard's is a row of buttons that do nothing."""
        async with self._app('local') as (app, pilot):
            bar = app.query_one(autotmux.TouchBar)
            await pilot.press('question_mark')
            await self._until(pilot, lambda: 'Close' in
                              [str(b.label) for b in bar.query(autotmux.Button)])
            labels = [str(b.label) for b in bar.query(autotmux.Button)]
            self.assertIn('Close', labels)
            self.assertNotIn('Kill session', labels)
            # And exactly one Close, though the help screen binds three keys
            # to it.
            self.assertEqual(labels.count('Close'), 1)

    async def test_publishing_is_silent_where_nobody_asked(self):
        """Bytes on the wire that no client can use are bytes competing with
        the screen on a slow link."""
        async with self._app('') as (app, _pilot):
            written = []
            app._publish_keys()
            self.assertEqual(written, [])
            self.assertIsNone(getattr(app, '_published_keys', None))

    async def test_a_button_never_takes_focus_from_the_table(self):
        """The bug this exists for, which only a real press could find.

        Buttons are focusable by default. Focus leaving the table takes
        Attach -- bound on the table -- out of the live set, so every control
        after it shifts up one position while the finger is on its way down.
        A tap aimed at Layout relabelled that button Clusters mid-press and
        opened the cluster manager instead.
        """
        async with self._app('local') as (app, pilot):
            bar = app.query_one(autotmux.TouchBar)
            focused = app.focused
            self.assertIsNotNone(focused)
            for button in bar.query(autotmux.Button):
                with self.subTest(label=str(button.label)):
                    self.assertFalse(button.can_focus)
            target = next(b for b in bar.query(autotmux.Button)
                          if str(b.label) == 'Refresh now')
            await pilot.click(target, offset=(target.region.width // 2, 1))
            await pilot.pause()
            # Not "focus is unchanged": an action may legitimately move it,
            # as cycling to the queue-only layout does. What must never
            # happen is focus landing on the control that was pressed.
            self.assertNotIn(app.focused, list(bar.query(autotmux.Button)))
            self.assertIs(app.focused, focused)

    async def test_the_control_set_survives_pressing_one_of_it(self):
        """A control bar that renumbers itself when you touch it is a bar
        where the second tap is a lottery."""
        async with self._app('local') as (app, pilot):
            bar = app.query_one(autotmux.TouchBar)
            before = [str(b.label) for b in bar.query(autotmux.Button)]
            target = next(b for b in bar.query(autotmux.Button)
                          if str(b.label) == 'Refresh now')
            await pilot.click(target, offset=(target.region.width // 2, 1))
            # Give the poll several chances to renumber the bar behind the
            # press; the point is that it does not.
            for _ in range(6):
                await pilot.pause()
                await asyncio.sleep(0.1)
            self.assertEqual([str(b.label) for b in bar.query(autotmux.Button)],
                             before)


class PreselectedRowTests(unittest.IsolatedAsyncioTestCase):
    """`--select NODE:SESSION` opens the dashboard standing on a row.

    One flag rather than one per verb. Every action here acts on the
    highlighted row, so landing on the right row is what makes renew, kill,
    note, view-output and ssh reachable from a phone -- none of which has a
    command line of its own, and none of which needs one.
    """

    def setUp(self):
        self._saved_launch = autotmux._launch_daemon
        autotmux._launch_daemon = lambda: (True, '')
        self._saved_prewarm = autotmux.AutotmuxApp._prewarm_interactive_async

        async def no_prewarm(_app, _nodes, _source_pool):
            return None

        autotmux.AutotmuxApp._prewarm_interactive_async = no_prewarm
        self._layout_dir = tempfile.TemporaryDirectory()
        self._saved_layout_path = autotmux.config.LAYOUT_PATH
        autotmux.config.LAYOUT_PATH = os.path.join(
            self._layout_dir.name, 'layout.json')

    def tearDown(self):
        autotmux._launch_daemon = self._saved_launch
        autotmux.AutotmuxApp._prewarm_interactive_async = self._saved_prewarm
        autotmux.config.LAYOUT_PATH = self._saved_layout_path
        self._layout_dir.cleanup()

    @asynccontextmanager
    async def _app(self, select):
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp(select=select)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await asyncio.sleep(0.4)
                await pilot.pause()
                yield app, pilot

    async def test_the_cursor_lands_on_the_row_that_was_asked_for(self):
        async with self._app(('gpu1', 'train')) as (app, _pilot):
            self.assertEqual((app.selected_node, app.selected_session),
                             ('gpu1', 'train'))

    async def test_a_wish_survives_the_empty_first_paint(self):
        """The first frame is painted against an empty state, and the rule
        that drops a selection whose row has vanished would drop this one
        before its row ever arrives."""
        async with self._app(('localhost', 'main')) as (app, _pilot):
            self.assertEqual((app.selected_node, app.selected_session),
                             ('localhost', 'main'))

    async def test_a_row_that_is_not_there_falls_back_rather_than_hanging(self):
        async with self._app(('nowhere', 'nothing')) as (app, _pilot):
            self.assertNotEqual(app.selected_node, 'nowhere')
            self.assertTrue(app.all_sessions)

    async def test_a_layout_with_no_rows_is_overridden_for_this_run_only(self):
        """Being asked to stand on a row in a layout that shows none cannot
        be honoured -- and the saved layout decides, so someone who last left
        the queue on screen would tap a session and arrive somewhere with no
        sessions in it."""
        autotmux.config.save_layout('jobs')
        async with self._app(None) as (app, _pilot):
            self.assertEqual(app.layout_mode, 'jobs')
        async with self._app(('gpu1', 'train')) as (app, _pilot):
            self.assertTrue(autotmux.layout_spec(app.layout_mode)['table'])
            self.assertEqual((app.selected_node, app.selected_session),
                             ('gpu1', 'train'))
        # And their preference is still theirs.
        self.assertEqual(autotmux.config.load_layout(), 'jobs')

    async def test_a_target_that_is_not_one_is_refused_before_it_gets_here(self):
        for value in ('--version', 'nocolon', '', ':', 'n:', ':s', None,
                      'a b:c'):
            with self.subTest(value=value):
                self.assertIsNone(autotmux._valid_select(value))
        self.assertEqual(autotmux._valid_select('gpu1:train'),
                         ('gpu1', 'train'))


class RemoteSessionGatewayTests(unittest.TestCase):
    """SSH is not evidence that this machine can answer for Slurm.

    Native login-node mode is the compatibility contract: an ordinary SSH
    session must not change behaviour, and must not touch a client config
    that may live on a wedged shared filesystem. That guard returned before
    the config was ever read -- so a saved `mode = "gateway"` never got a
    say, and sshing to a workstation that reaches its cluster only through a
    gateway showed zero sessions and an empty queue. The same binary on the
    same host reached every cluster when something other than SSH started it.

    The discriminator is whether the machine has Slurm at all. Where it does,
    nothing changes. Where it does not, native mode could only ever show an
    empty table.
    """

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in (
            'SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT', 'MOSH_CONNECTION',
            'MOSH_IP')}
        for name in self._saved:
            os.environ.pop(name, None)
        # _configure_gateway_mode installs a module-global pool. Left behind,
        # every later test that reads state goes through this one instead of
        # the file -- which is how six unrelated modules started failing only
        # when the whole suite ran.
        self._saved_pool = autotmux._GATEWAY_POOL

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        autotmux._GATEWAY_POOL = self._saved_pool

    def test_ssh_is_detected_from_any_of_the_usual_variables(self):
        for name in ('SSH_CONNECTION', 'SSH_TTY', 'SSH_CLIENT',
                     'MOSH_CONNECTION', 'MOSH_IP'):
            with self.subTest(name=name):
                os.environ[name] = 'x'
                try:
                    self.assertTrue(autotmux._is_remote_session())
                finally:
                    os.environ.pop(name, None)
        self.assertFalse(autotmux._is_remote_session())

    def test_slurm_is_what_makes_a_machine_a_login_node(self):
        with mock.patch.object(autotmux.shutil, 'which',
                               side_effect=lambda name: '/usr/bin/squeue'
                               if name == 'squeue' else None):
            self.assertTrue(autotmux._has_local_slurm())
        with mock.patch.object(autotmux.shutil, 'which', return_value=None):
            self.assertFalse(autotmux._has_local_slurm())

    def _setup(self, *, ssh, slurm, gateways=('k6',), mode='gateway'):
        os.environ['SSH_CONNECTION'] = 'x' if ssh else ''
        if not ssh:
            os.environ.pop('SSH_CONNECTION', None)
        args = SimpleNamespace(
            gateway=None, gateway_mode=False, cluster=None,
            gateway_login=False, gateway_check=False, login_mode=False)
        settings = dict(autotmux.config.CLIENT_DEFAULTS)
        settings['mode'] = mode
        settings['gateways'] = list(gateways)
        with mock.patch.object(autotmux.shutil, 'which',
                               side_effect=lambda n: '/usr/bin/squeue'
                               if (n == 'squeue' and slurm) else None), \
             mock.patch.object(autotmux, '_load_client_config_bounded',
                               return_value=(True, settings)), \
             mock.patch.object(autotmux.gateway_client, 'ClusterPool',
                               return_value=object()) as pool:
            autotmux._GATEWAY_POOL = None
            autotmux._configure_gateway_mode(args)
            return pool.called

    def test_a_login_node_over_ssh_is_left_exactly_as_it_was(self):
        """Where Slurm is, the local daemon is the source of truth."""
        self.assertFalse(self._setup(ssh=True, slurm=True))

    def test_a_gateway_client_over_ssh_now_honours_its_own_config(self):
        """The bug: this returned before the config was read, so a machine
        with no Slurm showed nothing at all."""
        self.assertTrue(self._setup(ssh=True, slurm=False))

    def test_nothing_changes_when_it_is_not_an_ssh_session(self):
        self.assertTrue(self._setup(ssh=False, slurm=False))
        self.assertTrue(self._setup(ssh=False, slurm=True))

    def test_a_host_with_no_gateways_configured_stays_native(self):
        """Falling through the guard must not invent a gateway for someone
        who never configured one."""
        self.assertFalse(self._setup(ssh=True, slurm=False, gateways=()))
