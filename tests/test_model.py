"""Tests for the dashboard's model — the part both views read.

The table and the browser list are two renderings of one derivation. What
these guard is that it stays one: a second walk over the state would be a
second opinion about which session is stale, which machine this is, and
whether a job is being renewed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import model


def state(**nodes) -> dict:
    return {'nodes': nodes, 'updated': '2026-08-07 12:00:00'}


def node(sessions=(), *, time='2:00:00', nproc='96', load='4.0',
         job_id=None, job_name=None, error='') -> dict:
    info = {'time': time, 'nproc': nproc, 'load': load,
            'sessions': list(sessions)}
    if job_id is not None:
        info['job_id'] = job_id
    if job_name is not None:
        info['job_name'] = job_name
    return {'alive': True, 'info': info, 'sessions': list(sessions),
            'last_error': error}


class NodeLabelTests(unittest.TestCase):
    """The daemon encodes a login host; nobody outside the model shows that."""

    def test_a_login_host_reads_the_way_the_table_reads_it(self):
        self.assertEqual(model.node_label('login--zgx'), 'login:zgx')
        self.assertEqual(
            model.node_label('login--holylogin08.rc.fas.harvard.edu'),
            'login:holylogin08')

    def test_a_compute_node_keeps_its_short_name(self):
        self.assertEqual(model.node_label('holygpu8a11104.rc.fas.harvard.edu'),
                         'holygpu8a11104')
        self.assertEqual(model.node_label('localhost'), 'localhost')

    def test_the_record_carries_both_the_routing_name_and_the_label(self):
        """Showing the routing name is how a phone calls a machine
        login--zgx while the table beside it calls it login:zgx."""
        rows = model.sessions(state(**{'login--zgx': node([('work', 1, 0)])}))
        self.assertEqual(rows[0]['node'], 'login--zgx')
        self.assertEqual(rows[0]['node_label'], 'login:zgx')


class SessionRecordTests(unittest.TestCase):
    def test_a_session_carries_the_fields_a_list_needs(self):
        rows = model.sessions(state(n1=node([('train', 2, 0)])))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['session'], 'train')
        self.assertEqual(row['label'], 'train')
        self.assertEqual(row['kind'], 'session')
        self.assertEqual(row['windows'], '2')
        self.assertEqual(row['left'], '2:00:00')
        self.assertEqual(row['load'], '4.0')
        self.assertEqual(row['cpu'], '96')

    def test_a_node_with_no_sessions_is_marked_as_such(self):
        """The row is a placeholder, not a session called \\x00autotmux:...,
        and a client must not have to know that sentinel to render it."""
        rows = model.sessions(state(n1=node([])))
        self.assertEqual(rows[0]['kind'], 'empty')
        self.assertEqual(rows[0]['session'], '')
        self.assertNotIn('\x00', rows[0]['label'])

    def test_the_idle_tier_comes_out_as_data_not_as_a_glyph(self):
        """A client colours by the tier. Parsing it back out of '● 5m' would
        be a third opinion about the same number."""
        model.IDLE_HINT_SECONDS, model.IDLE_STALE_SECONDS = 300, 3600
        rows = model.sessions(state(
            busy=node([('a', 1, 10)]),
            idle=node([('b', 1, 900)]),
            old=node([('c', 1, 99999)])))
        tiers = {r['label']: r['tier'] for r in rows}
        self.assertEqual(tiers['a'], '')
        self.assertEqual(tiers['b'], 'idle')
        self.assertEqual(tiers['c'], 'stale')
        seconds = {r['label']: r['idle_seconds'] for r in rows}
        self.assertEqual(seconds['b'], 900)

    def test_the_status_is_separated_from_the_idle_marker(self):
        """They are glued together in the table cell because a cell is one
        string. A list has two places to put them -- and the dot itself is a
        terminal's way of showing a tier it cannot show with shape or
        position, so it does not travel with the data at all."""
        rows = model.sessions(state(n1=node([('a', 1, 900)])))
        self.assertNotIn('●', rows[0]['status'])
        self.assertNotIn('●', rows[0]['idle_label'])
        self.assertEqual(rows[0]['idle_label'], '15m')
        self.assertEqual(rows[0]['tier'], 'idle')

    def test_the_order_is_the_tables_order(self):
        """Two views that sort differently are two views, and the one you are
        not looking at is the one you will act on next."""
        st = state(n1=node([('quiet', 1, 99999)]), n2=node([('busy', 1, 1)]))
        self.assertEqual(
            [r['label'] for r in model.sessions(st)],
            [model._session_label(r[1]) for r in model.build_session_rows(st)])

    def test_garbage_state_yields_no_rows_rather_than_an_exception(self):
        for value in (None, {}, [], {'nodes': 'nope'}, {'nodes': {'n': 5}}):
            with self.subTest(value=value):
                self.assertEqual(model.sessions(value), [])


class KeepaliveTests(unittest.TestCase):
    """Matched by job family, not by name.

    A renewed batch job comes back with a new id under the same name, so a
    name match would claim every job that ever shared it -- including ones
    nobody armed.
    """

    def _state(self, job_id, job_name, ka=None):
        st = state(n1=node([('run', 1, 0)], job_id=job_id, job_name=job_name))
        st['keepalive'] = ka or {}
        return st

    def test_an_unarmed_session_says_so(self):
        rows = model.sessions(self._state('123', 'train'), keepalive_entries=[])
        self.assertEqual(rows[0]['keepalive'], '')

    def test_an_armed_job_is_matched_by_its_id(self):
        entries = [{'job_id': '123', 'job_name': 'train', 'entry_id': 'e1'}]
        rows = model.sessions(self._state('123', 'train'),
                              keepalive_entries=entries)
        self.assertEqual(rows[0]['keepalive'], 'healthy')

    def test_a_different_job_with_the_same_name_is_not_claimed(self):
        entries = [{'job_id': '123', 'job_name': 'train', 'entry_id': 'e1'}]
        rows = model.sessions(self._state('999', 'train'),
                              keepalive_entries=entries)
        self.assertEqual(rows[0]['keepalive'], '')

    def test_the_live_renewal_state_reaches_the_client(self):
        entries = [{'job_id': '123', 'job_name': 'train', 'entry_id': 'e1'}]
        for reported, expected in (('renewing', 'renewing'),
                                   ('paused', 'paused'),
                                   ('healthy', 'healthy')):
            with self.subTest(state=reported):
                st = self._state('123', 'train',
                                 ka={'e1': {'state': reported}})
                rows = model.sessions(st, keepalive_entries=entries)
                self.assertEqual(rows[0]['keepalive'], expected)

    def test_the_table_and_the_list_agree(self):
        """The one that matters: the table folds a suffix into its STATUS
        cell and the list shows a tag, from the same function."""
        entries = [{'job_id': '123', 'job_name': 'train', 'entry_id': 'e1'}]
        st = self._state('123', 'train', ka={'e1': {'state': 'renewing'}})
        rows = model.decorate_keepalive(
            model.build_session_rows(st), st, entries)
        self.assertIn('renewing', rows[0][4])
        self.assertEqual(
            model.sessions(st, keepalive_entries=entries)[0]['keepalive'],
            'renewing')


class QueueTests(unittest.TestCase):
    def test_the_queue_is_passed_through_as_text(self):
        """`squeue -l` columns differ between sites, so parsing it here to
        re-render it would be inventing a schema Slurm did not promise."""
        st = {'squeue_long': 'JOBID NAME\n1 x', 'squeue_pending': '',
              'squeue_updated': 'now'}
        self.assertEqual(model.queue(st),
                         {'long': 'JOBID NAME\n1 x', 'pending': '',
                          'updated': 'now'})

    def test_a_missing_queue_is_empty_strings_not_none(self):
        for value in (None, {}, 'nope'):
            with self.subTest(value=value):
                self.assertEqual(set(model.queue(value)),
                                 {'long', 'pending', 'updated'})
