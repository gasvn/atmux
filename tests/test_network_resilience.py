"""Regression tests for weak-network coordination and warm-child ownership."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from autotmux import cli, daemon, ipc, lifecycle, network, warm_registry


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NodeNetworkCoordinatorTests(unittest.TestCase):
    def test_one_background_operation_per_node_but_other_nodes_continue(self):
        clock = FakeClock()
        coordinator = network.NodeNetworkCoordinator((2, 4), clock=clock)

        first = coordinator.acquire('gpu1', 'sessions')
        self.assertIsNotNone(first)
        self.assertIsNone(coordinator.acquire('gpu1', 'preview'))

        other = coordinator.acquire('gpu2', 'preview')
        self.assertIsNotNone(other)
        other.success()
        first.success()

        self.assertEqual(coordinator.snapshot('gpu1')['state'], 'healthy')
        self.assertEqual(coordinator.snapshot('gpu2')['state'], 'healthy')

    def test_failure_backoff_allows_one_half_open_probe_then_resets(self):
        clock = FakeClock()
        coordinator = network.NodeNetworkCoordinator((2, 4), clock=clock)
        lease = coordinator.acquire('gpu1', 'sessions')
        self.assertIsNotNone(lease)
        lease.failure('connection timed out')

        failed = coordinator.snapshot('gpu1')
        self.assertEqual(failed['state'], 'suspect')
        self.assertEqual(failed['failures'], 1)
        self.assertGreater(failed['retry_in'], 0)
        self.assertIsNone(coordinator.acquire('gpu1', 'preview'))

        clock.advance(10)
        probe = coordinator.acquire('gpu1', 'health')
        self.assertIsNotNone(probe)
        self.assertEqual(coordinator.snapshot('gpu1')['state'], 'half-open')
        self.assertIsNone(coordinator.acquire('gpu1', 'snapshot'))
        probe.success()

        recovered = coordinator.snapshot('gpu1')
        self.assertEqual(recovered['state'], 'healthy')
        self.assertEqual(recovered['failures'], 0)
        self.assertEqual(recovered['retry_in'], 0)

    def test_interactive_reports_share_and_extend_the_same_circuit(self):
        clock = FakeClock()
        coordinator = network.NodeNetworkCoordinator((1, 5), clock=clock)
        coordinator.report_failure('gpu1', 'frontend:attach', 'mux failed')
        first = coordinator.snapshot('gpu1')
        coordinator.report_failure('gpu1', 'frontend:direct', 'network down')
        second = coordinator.snapshot('gpu1')

        self.assertEqual(first['failures'], 1)
        self.assertEqual(second['failures'], 2)
        self.assertEqual(second['state'], 'offline')
        self.assertGreater(second['retry_in'], first['retry_in'])
        self.assertEqual(second['source'], 'frontend:direct')

        coordinator.report_success('gpu1', 'frontend:direct')
        self.assertEqual(coordinator.snapshot('gpu1')['state'], 'healthy')


class BoundedIpcTests(unittest.TestCase):
    class MemorySocket:
        def __init__(self, chunks=()):
            self.chunks = list(chunks)
            self.sent = b''

        def sendall(self, value):
            self.sent += value

        def recv(self, _size):
            return self.chunks.pop(0) if self.chunks else b''

    def test_round_trip_uses_one_bounded_json_frame(self):
        sender = self.MemorySocket()
        ipc.send_json(sender, {'action': 'status'}, 1024)
        receiver = self.MemorySocket([sender.sent])
        self.assertEqual(ipc.recv_json(receiver, 1024), {'action': 'status'})

    def test_trailing_data_and_oversized_messages_are_rejected(self):
        receiver = self.MemorySocket([b'{"ok":true}\ntrailing'])
        with self.assertRaisesRegex(ValueError, 'malformed'):
            ipc.recv_json(receiver, 1024)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            ipc._encoded({'data': 'x' * 100}, 32)

    def test_reply_survives_a_peer_that_closes_first(self):
        """A daemon that answers and hangs up must still deliver its answer.

        ``shutdown(SHUT_WR)`` fails with ENOTCONN once the peer is gone, but the
        reply is already buffered and readable, so aborting there would discard
        a perfectly good response.  Ordinarily this depends on scheduling and
        shows up only as a rare failure under load; here the client is held
        until the server has definitely closed, which makes it certain.
        """
        original_send = ipc.send_json
        server_closed = threading.Event()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'preview.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                try:
                    server.bind(path)
                    server.listen(1)
                except PermissionError as error:
                    self.skipTest(f'Unix sockets denied by sandbox: {error}')
                server.settimeout(5)

                def serve():
                    connection, _ = server.accept()
                    with connection:
                        request = ipc.recv_json(
                            connection, ipc.MAX_REQUEST_BYTES)
                        original_send(connection, {'ok': True, 'echo': request},
                                      ipc.MAX_RESPONSE_BYTES)
                    server_closed.set()

                def client_send(sock, value, limit):
                    # Only the client reaches the patched name; the server calls
                    # the original directly.
                    original_send(sock, value, limit)
                    self.assertTrue(server_closed.wait(5), 'server never closed')

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                try:
                    with mock.patch.object(ipc, 'send_json', client_send):
                        response = ipc.request(
                            path, {'action': 'status'}, timeout=5)
                except PermissionError as error:
                    self.skipTest(f'Unix sockets denied by sandbox: {error}')
                finally:
                    thread.join(timeout=5)
            finally:
                server.close()
        self.assertEqual(response,
                         {'ok': True, 'echo': {'action': 'status'}})

    def test_real_private_unix_socket_round_trip(self):
        """Exercise the exact client protocol where Unix sockets are allowed."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'preview.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                try:
                    server.bind(path)
                    server.listen(1)
                except PermissionError as error:
                    self.skipTest(f'Unix sockets denied by sandbox: {error}')
                server.settimeout(2)
                errors = []

                def serve():
                    try:
                        connection, _ = server.accept()
                        with connection:
                            request = ipc.recv_json(
                                connection, ipc.MAX_REQUEST_BYTES)
                            ipc.send_json(connection, {
                                'ok': True, 'echo': request,
                            }, ipc.MAX_RESPONSE_BYTES)
                    except Exception as error:  # surfaced below in test thread
                        errors.append(error)

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                try:
                    response = ipc.request(
                        path, {'action': 'status', 'node': 'gpu1'}, timeout=2)
                except PermissionError as error:
                    self.skipTest(f'Unix sockets denied by sandbox: {error}')
                finally:
                    thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(response, {
                    'ok': True,
                    'echo': {'action': 'status', 'node': 'gpu1'},
                })
            finally:
                server.close()


class WarmRegistryTests(unittest.TestCase):
    def test_sweep_kills_only_exact_registered_child_with_dead_parent(self):
        with tempfile.TemporaryDirectory() as td:
            warm_dir = os.path.join(td, 'warm')
            ctl_dir = os.path.join(td, 'ctl')
            os.mkdir(warm_dir, 0o700)
            os.mkdir(ctl_dir, 0o700)
            child_pid = 43210
            control_path = os.path.join(ctl_dir, 'cm_gpu1')
            path = warm_registry.registry_path(warm_dir, child_pid)
            record = {
                'version': 1,
                'kind': 'warm-ssh',
                'pid': child_pid,
                'token': 'child-token',
                'parent_pid': 43100,
                'parent_token': 'parent-token',
                'node': 'gpu1',
                'control_path': control_path,
                'argv': ['ssh', '-o', f'ControlPath={control_path}',
                         '-o', 'BatchMode=yes', '-o', 'ConnectionAttempts=1',
                         '-tt', 'gpu1'],
                'created': time.time(),
            }
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(record, handle)
            os.chmod(path, 0o600)

            def same_process(pid, _token):
                return pid == child_pid

            with mock.patch.object(warm_registry.lifecycle, 'same_process',
                                   side_effect=same_process), \
                 mock.patch.object(warm_registry, '_warm_ssh_matches',
                                   return_value=True), \
                 mock.patch.object(warm_registry, '_terminate',
                                   return_value=True) as terminate:
                result = warm_registry.sweep(
                    warm_dir, ctl_dir, include_legacy=False)

            self.assertEqual(result['registered_killed'], 1)
            self.assertFalse(os.path.exists(path))
            terminate.assert_called_once_with(child_pid, 'child-token')

    @unittest.skipUnless(sys.platform.startswith('linux'),
                         'PR_SET_PDEATHSIG is Linux-specific')
    def test_warm_ssh_dies_when_owning_frontend_is_killed(self):
        """Exercise the real helper race barrier and kernel parent-death signal."""
        with tempfile.TemporaryDirectory() as td:
            warm_dir = os.path.join(td, 'warm')
            ctl_dir = os.path.join(td, 'ctl')
            bin_dir = os.path.join(td, 'bin')
            os.mkdir(warm_dir, 0o700)
            os.mkdir(ctl_dir, 0o700)
            os.mkdir(bin_dir, 0o700)
            fake_ssh = os.path.join(bin_dir, 'ssh')
            with open(fake_ssh, 'w', encoding='utf-8') as handle:
                handle.write('#!/bin/sh\nexec /bin/sleep 30\n')
            os.chmod(fake_ssh, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            control_path = os.path.join(ctl_dir, 'cm_gpu1')

            supervisor_code = r'''
import os, subprocess, sys
from autotmux import lifecycle
parent = os.getpid()
token = lifecycle.process_token(parent)
argv = [
    sys.executable, '-m', 'autotmux.ssh_child',
    '--parent-pid', str(parent), '--parent-token', token,
    '--registry-dir', sys.argv[1], '--node', 'gpu1',
    '--control-path', sys.argv[2], '--', 'ssh',
    '-o', 'BatchMode=yes', '-o', 'ConnectionAttempts=1',
    '-o', 'ControlPath=' + sys.argv[2], '-tt', 'gpu1']
child = subprocess.Popen(argv)
print(child.pid, flush=True)
child.wait()
'''
            env = dict(os.environ)
            env['PATH'] = bin_dir + os.pathsep + env.get('PATH', '')
            source_dir = os.path.abspath(os.path.join(
                os.path.dirname(__file__), '..', 'src'))
            env['PYTHONPATH'] = source_dir + os.pathsep + env.get(
                'PYTHONPATH', '')
            supervisor = subprocess.Popen(
                [sys.executable, '-c', supervisor_code, warm_dir, control_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=env,
            )
            child_pid = None
            child_token = None
            try:
                line = supervisor.stdout.readline().strip()
                self.assertTrue(line.isdigit(),
                                f'unexpected supervisor output: {line!r}')
                child_pid = int(line)
                child_token = lifecycle.process_token(child_pid)
                record_path = warm_registry.registry_path(warm_dir, child_pid)
                deadline = time.monotonic() + 3
                while not os.path.exists(record_path) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(record_path),
                                'warm helper did not publish its ownership record')

                os.kill(supervisor.pid, signal.SIGKILL)
                supervisor.wait(timeout=3)
                deadline = time.monotonic() + 3
                while (lifecycle.same_process(child_pid, child_token)
                       and time.monotonic() < deadline):
                    time.sleep(0.02)
                self.assertFalse(lifecycle.same_process(child_pid, child_token),
                                 'warm SSH survived the owning frontend')
            finally:
                if supervisor.poll() is None:
                    os.kill(supervisor.pid, signal.SIGKILL)
                    supervisor.wait(timeout=3)
                if child_pid and lifecycle.same_process(child_pid, child_token):
                    lifecycle.signal_same_process(
                        child_pid, child_token, signal.SIGKILL)


class InteractiveFallbackTests(unittest.TestCase):
    def test_mux_transport_failure_retries_once_with_explicit_direct_ssh(self):
        with mock.patch.object(cli, '_run_user_command',
                               side_effect=[(255, 'mux failed'), (0, '')]) as run, \
             mock.patch.object(cli, '_report_network_event') as report:
            returncode, error, direct = cli._run_remote_user_command(
                'gpu1', ['tmux', 'attach', '-t', 'train'])

        self.assertEqual((returncode, error, direct), (0, '', True))
        self.assertEqual(run.call_count, 2)
        first = run.call_args_list[0].args[0]
        second = run.call_args_list[1].args[0]
        self.assertNotIn('ControlPath=none', first)
        self.assertIn('ControlMaster=auto', first)
        self.assertIn('ControlPersist=300', first)
        self.assertIn('IPQoS=none', first)
        self.assertIn('Compression=no', first)
        self.assertIn('attach-session', first)
        self.assertIn('-d', first)
        self.assertIn('ControlPath=none', second)
        self.assertIn('ControlMaster=no', second)
        report.assert_called_once()

    def test_frontend_uses_daemon_published_ssh_timeouts(self):
        with cli._SSH_SETTINGS_LOCK:
            previous = dict(cli._SSH_SETTINGS)
        try:
            cli._apply_daemon_ssh_settings({'ssh_config': {
                'connect_timeout': 17,
                'server_alive_int': 23,
                'server_alive_max': 4,
            }})
            args = cli._get_ssh_args('gpu1', direct=True)
            self.assertIn('ConnectTimeout=17', args)
            self.assertIn('ServerAliveInterval=23', args)
            self.assertIn('ServerAliveCountMax=4', args)
        finally:
            with cli._SSH_SETTINGS_LOCK:
                cli._SSH_SETTINGS.clear()
                cli._SSH_SETTINGS.update(previous)

    def test_interactive_master_is_isolated_from_daemon_master(self):
        args = cli._get_ssh_args('gpu1', interactive=True)
        control = next(
            item for item in args if item.startswith('ControlPath='))
        self.assertNotEqual(control, f'ControlPath={cli._ctl_path("gpu1")}')
        self.assertIn('interactive-ctl', control)

    def test_direct_attach_helper_uses_published_direct_preference(self):
        with mock.patch.object(cli, '_request_daemon_start'), \
             mock.patch.object(cli, '_will_nest_tmux', return_value=False), \
             mock.patch.object(cli, '_published_direct_preference',
                               return_value=True), \
             mock.patch.object(cli, '_run_remote_user_command',
                               return_value=(0, '', True)) as run, \
             mock.patch.object(cli, '_publish_remote_command_result') as publish:
            self.assertEqual(cli._direct_attach('gpu1:train'), 0)

        run.assert_called_once_with(
            'gpu1', ['tmux', 'attach', '-t', 'train'], direct=True)
        publish.assert_called_once_with(
            'gpu1', 0, '', 'direct-attach')

    def test_direct_shell_helper_has_the_same_direct_fallback(self):
        with mock.patch.object(cli, '_request_daemon_start'), \
             mock.patch.object(cli, '_published_direct_preference',
                               return_value=False), \
             mock.patch.object(cli, '_run_remote_user_command',
                               return_value=(0, '', True)) as run, \
             mock.patch.object(cli, '_publish_remote_command_result') as publish:
            self.assertEqual(cli._direct_shell('gpu1'), 0)

        run.assert_called_once_with('gpu1', None, direct=False)
        publish.assert_called_once_with('gpu1', 0, '', 'direct-shell')


class InteractivePrewarmTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_native_handshake_is_not_duplicated_by_refresh(self):
        app = object.__new__(cli.AutotmuxApp)
        app._interactive_prewarm_retry = {}
        app._interactive_prewarming = set()
        app._interactive_prewarm_lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()

        def slow_run(*_args, **_kwargs):
            started.set()
            release.wait(3)
            return mock.Mock(returncode=0)

        first = None
        with mock.patch.object(cli, '_GATEWAY_POOL', None), \
             mock.patch.object(cli.os.path, 'exists', return_value=False), \
             mock.patch.object(cli.subprocess, 'run', side_effect=slow_run) as run:
            first = asyncio.create_task(
                app._prewarm_interactive_async(('gpu1',), None))
            try:
                deadline = time.monotonic() + 2
                while not started.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set(),
                                'first SSH prewarm did not start')
                await app._prewarm_interactive_async(('gpu1',), None)
                self.assertEqual(run.call_count, 1)
            finally:
                release.set()
                await first


class PreviewCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.old_nodes = daemon._known_nodes_info
        self.old_coordinator = daemon._network_coordinator
        daemon._known_nodes_info = {
            'gpu1': {'sessions': [['train', '1']], 'last_error': ''},
        }
        daemon._network_coordinator = network.NodeNetworkCoordinator((1, 2))

    def tearDown(self):
        daemon._known_nodes_info = self.old_nodes
        daemon._network_coordinator = self.old_coordinator

    def test_preview_is_captured_and_cached_only_by_daemon(self):
        with mock.patch.object(daemon, '_capture_pane',
                               return_value='pane output') as capture, \
             mock.patch.object(daemon, '_update_snapshot_entry') as update:
            response = daemon._handle_preview_request({
                'action': 'preview', 'node': 'gpu1', 'session': 'train'})

        self.assertTrue(response['ok'])
        self.assertEqual(response['content'], 'pane output')
        # history=0: the poll preview is one screen. Scrollback is only
        # ever asked for by an explicit expanded read.
        capture.assert_called_once_with(
            'gpu1', 'train', source='preview', history=0)
        update.assert_called_once_with('gpu1', 'train', 'pane output')

    def test_unknown_session_is_rejected_before_any_network_call(self):
        with mock.patch.object(daemon, '_capture_pane') as capture:
            response = daemon._handle_preview_request({
                'action': 'preview', 'node': 'gpu1', 'session': 'vanished'})
        self.assertFalse(response['ok'])
        self.assertEqual(response['kind'], 'not-found')
        capture.assert_not_called()

    def test_interactive_failure_report_opens_daemon_shared_circuit(self):
        with mock.patch.object(daemon, '_write_status'):
            response = daemon._handle_preview_request({
                'action': 'report', 'node': 'gpu1', 'outcome': 'failure',
                'source': 'attach', 'reason': 'network unreachable'})
        self.assertTrue(response['ok'])
        self.assertEqual(response['network']['failures'], 1)
        self.assertEqual(response['network']['state'], 'suspect')


class LastKnownStateRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_state_file = daemon.STAT_FILE
        self.old_nodes = daemon._known_nodes_info
        self.old_squeue = daemon._squeue_text
        self.old_gone = daemon._gone_node_streak
        daemon.STAT_FILE = os.path.join(self.tempdir.name, 'daemon.json')
        daemon._known_nodes_info = {}
        daemon._squeue_text = {
            'long': '', 'pending': '', 'updated': '',
            'updated_monotonic': None,
        }
        daemon._gone_node_streak = {}

    def tearDown(self):
        daemon.STAT_FILE = self.old_state_file
        daemon._known_nodes_info = self.old_nodes
        daemon._squeue_text = self.old_squeue
        daemon._gone_node_streak = self.old_gone
        self.tempdir.cleanup()

    def test_restart_preserves_sessions_until_fresh_discovery_arrives(self):
        previous = {
            'nodes': {
                'gpu1': {
                    'info': {
                        'time': '1:00:00', 'job_name': 'train',
                        'job_id': '123', 'state': 'RUNNING',
                        'sessions': [['main', '2']], 'nproc': '8',
                        'load': '1.5', 'escape_time': '10',
                    },
                    'sessions': [['main', '2']],
                },
                '-option-looking-host': {'info': {'job_id': 'bad'}},
            },
            'squeue_long': 'last good jobs',
            'squeue_pending': 'last good pending jobs',
            'squeue_updated': '2026-08-01 12:00:00',
            'squeue_updated_monotonic': 100.0,
        }
        with open(daemon.STAT_FILE, 'w', encoding='utf-8') as handle:
            json.dump(previous, handle)

        self.assertEqual(daemon._restore_last_known_state(), 1)
        cached = daemon._known_nodes_info['gpu1']
        self.assertEqual(cached['sessions'], [['main', '2']])
        self.assertTrue(cached['restored_from_cache'])
        self.assertIn('last-known', cached['last_error'])
        self.assertNotIn('-option-looking-host', daemon._known_nodes_info)
        self.assertEqual(daemon._squeue_text['long'], 'last good jobs')

        fresh = {
            'gpu1': {
                'time': '0:59:30', 'job_name': 'train',
                'job_id': '123', 'state': 'RUNNING',
            },
        }
        gone, nodes = daemon._merge_discovery(fresh, complete=False)
        self.assertEqual(gone, [])
        self.assertEqual(nodes, ['gpu1'])
        current = daemon._known_nodes_info['gpu1']
        self.assertEqual(current['sessions'], [['main', '2']])
        self.assertNotIn('restored_from_cache', current)
        self.assertNotIn('last_error', current)
        self.assertNotIn('errors', current)

    def test_missing_or_malformed_status_is_ignored(self):
        self.assertEqual(daemon._restore_last_known_state(), 0)
        with open(daemon.STAT_FILE, 'w', encoding='utf-8') as handle:
            handle.write('[]')
        self.assertEqual(daemon._restore_last_known_state(), 0)
        self.assertEqual(daemon._known_nodes_info, {})


if __name__ == '__main__':
    unittest.main()
