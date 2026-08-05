"""Creating and killing tmux sessions from the dashboard.

These cross four layers -- TUI, gateway pool, agent, daemon -- and one of them
runs a destructive command on a machine the user cannot see. The validation
each layer performs is the subject here.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import agent, cli, config, daemon, gateway


def _ok(returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, '', stderr)


class NameValidationTests(unittest.TestCase):
    """tmux addresses windows and panes with ':' and '.', so a session
    carrying either can never be referred to reliably afterwards."""

    def test_ordinary_names_are_accepted(self):
        for name in ('train', 'tu_debug', 'run-4', 'a@b', 'x+y', 'A1'):
            self.assertTrue(config.NEW_SESSION_RE.fullmatch(name), name)

    def test_target_punctuation_and_hostile_input_are_refused(self):
        for name in ('a:b', 'a.b', '', '-lead', ' spaced', 'a b', 'a/b',
                     'a;rm -rf /', '$(id)', 'a\nb', 'x' * 65):
            self.assertIsNone(config.NEW_SESSION_RE.fullmatch(name), name)


class DaemonSessionRequestTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(daemon._known_nodes_info)
        daemon._known_nodes_info.clear()
        daemon._known_nodes_info['gpu1'] = {
            'sessions': [['train', '1', 100]], 'job_id': '42'}

    def tearDown(self):
        daemon._known_nodes_info.clear()
        daemon._known_nodes_info.update(self._saved)

    def test_an_unknown_verb_is_refused_before_anything_runs(self):
        with mock.patch.object(daemon, '_run_node_tmux') as run:
            reply = daemon._handle_session_request(
                'gpu1', {'verb': 'nuke', 'session': 'train'})
        self.assertFalse(reply['ok'])
        run.assert_not_called()

    def test_killing_an_unknown_session_does_not_reach_the_node(self):
        with mock.patch.object(daemon, '_run_node_tmux') as run:
            reply = daemon._handle_session_request(
                'gpu1', {'verb': 'kill', 'session': 'ghost'})
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['kind'], 'not-found')
        run.assert_not_called()

    def test_a_bad_new_name_never_reaches_the_node(self):
        with mock.patch.object(daemon, '_run_node_tmux') as run:
            reply = daemon._handle_session_request(
                'gpu1', {'verb': 'new', 'session': 'a;rm -rf /'})
        self.assertFalse(reply['ok'])
        run.assert_not_called()

    def test_kill_sends_the_expected_tmux_command(self):
        with mock.patch.object(daemon, '_run_node_tmux',
                               return_value=(True, '')) as run, \
             mock.patch.object(daemon, '_write_status'):
            reply = daemon._handle_session_request(
                'gpu1', {'verb': 'kill', 'session': 'train'})
        self.assertTrue(reply['ok'])
        self.assertEqual(run.call_args.args[1],
                         ['kill-session', '-t', 'train'])

    def test_new_creates_a_detached_session(self):
        with mock.patch.object(daemon, '_run_node_tmux',
                               return_value=(True, '')) as run, \
             mock.patch.object(daemon, '_write_status'):
            reply = daemon._handle_session_request(
                'gpu1', {'verb': 'new', 'session': 'sweep'})
        self.assertTrue(reply['ok'])
        # -d: creating a session must not steal the terminal the user is in.
        self.assertEqual(run.call_args.args[1],
                         ['new-session', '-d', '-s', 'sweep'])

    def test_a_refused_command_reports_why(self):
        with mock.patch.object(daemon, '_run_node_tmux',
                               return_value=(False, 'no server running')):
            reply = daemon._handle_session_request(
                'gpu1', {'verb': 'kill', 'session': 'train'})
        self.assertFalse(reply['ok'])
        self.assertIn('no server', reply['reason'])

    def test_the_published_list_changes_without_waiting_for_a_poll(self):
        """15s of a killed session still on screen makes the key look dead and
        invites a second press."""
        with mock.patch.object(daemon, '_write_status'):
            daemon._apply_session_change('gpu1', 'train', 'kill')
            self.assertEqual(
                daemon._known_nodes_info['gpu1']['sessions'], [])
            daemon._apply_session_change('gpu1', 'sweep', 'new')
            names = [s[0] for s in daemon._known_nodes_info['gpu1']['sessions']]
            self.assertEqual(names, ['sweep'])

    def test_creating_a_session_twice_does_not_duplicate_the_row(self):
        with mock.patch.object(daemon, '_write_status'):
            daemon._apply_session_change('gpu1', 'train', 'new')
            names = [s[0] for s in daemon._known_nodes_info['gpu1']['sessions']]
        self.assertEqual(names, ['train'])


class RunNodeTmuxTests(unittest.TestCase):
    def _lease(self):
        return mock.MagicMock()

    def test_a_tmux_error_is_not_a_transport_failure(self):
        """"session not found" must not trip the node's circuit breaker and
        take previews and attaches down with it."""
        lease = self._lease()
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=lease), \
             mock.patch.object(daemon, '_master_alive', return_value=True), \
             mock.patch.object(daemon, '_hard_run',
                               return_value=_ok(1, 'session not found')):
            ok, why = daemon._run_node_tmux('gpu1', ['kill-session'], 'x')
        self.assertFalse(ok)
        self.assertIn('session not found', why)
        lease.success.assert_called_once()
        lease.failure.assert_not_called()

    def test_ssh_status_255_is_a_transport_failure(self):
        lease = self._lease()
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=lease), \
             mock.patch.object(daemon, '_master_alive', return_value=True), \
             mock.patch.object(daemon, '_hard_run',
                               return_value=_ok(255, 'broken pipe')):
            ok, _why = daemon._run_node_tmux('gpu1', ['kill-session'], 'x')
        self.assertFalse(ok)
        lease.failure.assert_called_once()

    def test_the_session_name_is_quoted_for_the_remote_shell(self):
        lease = self._lease()
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=lease), \
             mock.patch.object(daemon, '_master_alive', return_value=True), \
             mock.patch.object(daemon, '_hard_run',
                               return_value=_ok()) as run:
            daemon._run_node_tmux('gpu1', ['kill-session', '-t', 'a b'], 'x')
        remote = run.call_args.args[0][-1]
        self.assertIn("'a b'", remote)

    def test_localhost_runs_tmux_without_ssh(self):
        lease = self._lease()
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=lease), \
             mock.patch.object(daemon, '_hard_run',
                               return_value=_ok()) as run:
            ok, _ = daemon._run_node_tmux('localhost', ['kill-session'], 'x')
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0][0], 'tmux')

    def test_a_busy_node_is_declined_rather_than_queued(self):
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=None):
            ok, why = daemon._run_node_tmux('gpu1', ['kill-session'], 'x')
        self.assertFalse(ok)
        self.assertIn('busy', why)


class GatewaySessionCommandTests(unittest.TestCase):
    def _pool(self):
        pool = gateway.GatewayPool.__new__(gateway.GatewayPool)
        pool.settings = {'state_timeout': 10.0}
        return pool

    def test_an_invalid_verb_never_leaves_the_client(self):
        with mock.patch.object(gateway.GatewayPool, '_rpc_failover') as rpc:
            reply = self._pool().session_command('gpu1', 'train', 'nuke')
        self.assertFalse(reply['ok'])
        rpc.assert_not_called()

    def test_a_remote_request_is_not_retried_on_failure(self):
        """A preview is a read and costs nothing to repeat. These change state:
        a retry after an ambiguous failure could create a second session or
        kill something the user has since recreated."""
        with mock.patch.object(gateway.GatewayPool, '_rpc_failover',
                               return_value={'ok': True}) as rpc:
            self._pool().session_command('gpu1', 'train', 'kill')
        self.assertIs(rpc.call_args.kwargs['retry_unavailable'], False)

    def test_localhost_validates_the_name_before_running_tmux(self):
        with mock.patch.object(gateway.subprocess, 'run') as run:
            reply = self._pool().session_command('localhost', 'a:b', 'new')
        self.assertFalse(reply['ok'])
        run.assert_not_called()

    def test_localhost_kill_runs_tmux_directly(self):
        with mock.patch.object(gateway.subprocess, 'run',
                               return_value=_ok()) as run:
            reply = self._pool().session_command('localhost', 'train', 'kill')
        self.assertTrue(reply['ok'])
        self.assertEqual(run.call_args.args[0],
                         ['tmux', 'kill-session', '-t', 'train'])


class AgentForwardingTests(unittest.TestCase):
    def test_a_session_request_is_forwarded_with_its_verb(self):
        with mock.patch.object(agent, '_forward_daemon_request',
                               return_value={'ok': True}) as forward:
            agent.handle_rpc({'action': 'session', 'node': 'gpu1',
                              'session': 'train', 'verb': 'kill'})
        sent = forward.call_args.args[0]
        self.assertEqual(sent['verb'], 'kill')
        self.assertEqual(sent['session'], 'train')

    def test_a_bad_verb_is_rejected_at_the_agent(self):
        with mock.patch.object(agent, '_forward_daemon_request') as forward:
            reply = agent.handle_rpc({'action': 'session', 'node': 'gpu1',
                                      'session': 'train', 'verb': 'nuke'})
        self.assertFalse(reply['ok'])
        forward.assert_not_called()

    def test_a_bad_node_is_rejected_at_the_agent(self):
        with mock.patch.object(agent, '_forward_daemon_request') as forward:
            reply = agent.handle_rpc({'action': 'session', 'node': 'a;b',
                                      'session': 'train', 'verb': 'kill'})
        self.assertFalse(reply['ok'])
        forward.assert_not_called()


class ScrollbackPreviewTests(unittest.TestCase):
    """The dashboard preview is one screen; reading why something died means
    looking further back, and attaching to look resizes the session."""

    def test_the_poll_preview_asks_for_no_scrollback(self):
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=mock.MagicMock()), \
             mock.patch.object(daemon, '_master_alive', return_value=True), \
             mock.patch.object(daemon, '_hard_run', return_value=_ok()) as run:
            daemon._capture_pane('gpu1', 'train', source='preview')
        self.assertNotIn('-S', run.call_args.args[0][-1])

    def test_an_expanded_read_asks_for_history(self):
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=mock.MagicMock()), \
             mock.patch.object(daemon, '_master_alive', return_value=True), \
             mock.patch.object(daemon, '_hard_run', return_value=_ok()) as run:
            daemon._capture_pane('gpu1', 'train', source='preview',
                                 history=2000)
        self.assertIn('-S -2000', run.call_args.args[0][-1])

    def test_localhost_history_is_passed_as_argv(self):
        with mock.patch.object(daemon._network_coordinator, 'acquire',
                               return_value=mock.MagicMock()), \
             mock.patch.object(daemon, '_hard_run', return_value=_ok()) as run:
            daemon._capture_pane('localhost', 'train', history=500)
        self.assertEqual(run.call_args.args[0][:6],
                         ['tmux', 'capture-pane', '-p', '-e', '-S', '-500'])

    def test_a_request_cannot_ask_for_unbounded_history(self):
        node_infos = {'sessions': [['train', '1', 1]], 'job_id': '1'}
        daemon._known_nodes_info['gpu1'] = node_infos
        try:
            with mock.patch.object(daemon, '_capture_pane',
                                   return_value='x') as capture, \
                 mock.patch.object(daemon, '_update_snapshot_entry'):
                daemon._handle_preview_request({
                    'action': 'preview', 'node': 'gpu1', 'session': 'train',
                    'history': 10 ** 9})
            self.assertEqual(capture.call_args.kwargs['history'],
                             config.PREVIEW_HISTORY_MAX)
        finally:
            daemon._known_nodes_info.pop('gpu1', None)

    def test_a_junk_history_is_treated_as_none(self):
        daemon._known_nodes_info['gpu1'] = {
            'sessions': [['train', '1', 1]], 'job_id': '1'}
        try:
            for value in ('lots', None, -5, [1]):
                with mock.patch.object(daemon, '_capture_pane',
                                       return_value='x') as capture, \
                     mock.patch.object(daemon, '_update_snapshot_entry'):
                    daemon._handle_preview_request({
                        'action': 'preview', 'node': 'gpu1',
                        'session': 'train', 'history': value})
                self.assertEqual(capture.call_args.kwargs['history'], 0, value)
        finally:
            daemon._known_nodes_info.pop('gpu1', None)

    def test_an_expanded_read_is_not_cached_as_the_row_preview(self):
        """Otherwise the table would start showing scrollback."""
        pool = gateway.GatewayPool.__new__(gateway.GatewayPool)
        pool.settings = {'state_timeout': 10.0}
        with mock.patch.object(gateway.GatewayPool, '_rpc_failover',
                               return_value={'ok': True, 'content': 'x'}), \
             mock.patch.object(gateway.GatewayPool, '_store_preview') as store:
            pool.preview('gpu1', 'train', history=2000)
        store.assert_not_called()

    def test_an_ordinary_preview_is_still_cached(self):
        pool = gateway.GatewayPool.__new__(gateway.GatewayPool)
        pool.settings = {'state_timeout': 10.0}
        with mock.patch.object(gateway.GatewayPool, '_rpc_failover',
                               return_value={'ok': True, 'content': 'x'}), \
             mock.patch.object(gateway.GatewayPool, '_store_preview') as store:
            pool.preview('gpu1', 'train')
        store.assert_called_once()

    def test_the_agent_bounds_history_before_forwarding(self):
        with mock.patch.object(agent, '_forward_daemon_request',
                               return_value={'ok': True}) as forward:
            agent.handle_rpc({'action': 'preview', 'node': 'gpu1',
                              'session': 'train', 'history': 10 ** 9})
        self.assertEqual(forward.call_args.args[0]['history'],
                         config.PREVIEW_HISTORY_MAX)


class ConfirmScreenTests(unittest.TestCase):
    def test_the_destructive_answer_is_never_the_default(self):
        keys = {b.key: b.action for b in cli.ConfirmScreen.BINDINGS}
        self.assertEqual(keys['escape'], 'refuse')
        self.assertEqual(keys['n'], 'refuse')
        self.assertEqual(keys['y'], 'accept')


if __name__ == '__main__':
    unittest.main()
