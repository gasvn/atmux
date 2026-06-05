"""Headless tests of the AutotmuxApp via Textual's Pilot framework.

These don't need a running daemon — they point STATE_FILE at a temp
file with synthetic content and exercise the UI logic directly.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotmux

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


class FrontendPilotTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_offline_row_skipped_for_attach(self):
        """Pressing Enter on an <offline> row should not attempt to spawn ssh."""
        with tempfile.TemporaryDirectory() as td:
            _setup_state(td)
            app = autotmux.AutotmuxApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                # Force selection onto the offline gpu2 row.
                app.selected_node = 'gpu2'
                app.selected_session = '<offline>'
                # Should be a no-op rather than raising.
                await app.action_attach_session()

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
