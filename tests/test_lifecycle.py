"""Process identity, PID-reuse, and stable-lock regression tests."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from autotmux import lifecycle


class ProcessIdentityTests(unittest.TestCase):
    def test_argument_substring_does_not_impersonate_daemon(self):
        proc = subprocess.Popen([
            sys.executable, '-c', 'import time; time.sleep(30)',
            'autotmux.daemon', 'atd',
        ])
        try:
            self.assertTrue(lifecycle.pid_running(proc.pid))
            self.assertFalse(lifecycle.is_autotmux_daemon(proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_atd_as_argument_to_non_python_program_is_not_daemon(self):
        with mock.patch.object(lifecycle, '_cmdline',
                               return_value=[b'/bin/echo', b'atd']):
            self.assertFalse(lifecycle.is_autotmux_daemon(123))

    def test_python_flags_before_module_are_recognised(self):
        with mock.patch.object(
                lifecycle, '_cmdline',
                return_value=[b'/usr/bin/python3', b'-W', b'ignore', b'-m',
                              b'autotmux.daemon', b'run']):
            self.assertTrue(lifecycle.is_autotmux_daemon(123))

    def test_short_lived_daemon_control_commands_are_not_daemons(self):
        for action in (b'status', b'stop', b'logs', b'--version'):
            with self.subTest(action=action), mock.patch.object(
                    lifecycle, '_cmdline',
                    return_value=[b'/usr/bin/python3', b'-m',
                                  b'autotmux.daemon', action]):
                self.assertFalse(lifecycle.is_autotmux_daemon(123))

    def test_only_long_lived_console_actions_are_recognised(self):
        for action in (b'start', b'restart', b'run'):
            with self.subTest(action=action), mock.patch.object(
                    lifecycle, '_cmdline',
                    return_value=[b'/env/bin/python3', b'/env/bin/atd',
                                  action]):
                self.assertTrue(lifecycle.is_autotmux_daemon(123))

    def test_console_script_basenames_are_recognised(self):
        for script in (b'atd', b'atmux-daemon'):
            with self.subTest(script=script), mock.patch.object(
                    lifecycle, '_cmdline',
                    return_value=[b'/env/bin/python3', b'/env/bin/' + script,
                                  b'start']):
                self.assertTrue(lifecycle.is_autotmux_daemon(123))

    def test_unrelated_native_atd_basename_is_not_daemon(self):
        with mock.patch.object(lifecycle, '_cmdline',
                               return_value=[b'/usr/sbin/atd']):
            self.assertFalse(lifecycle.is_autotmux_daemon(123))

    def test_unrelated_python_script_named_atd_is_not_daemon(self):
        with tempfile.TemporaryDirectory() as td:
            script = os.path.join(td, 'atd')
            with open(script, 'w') as f:
                f.write('import time\ntime.sleep(30)\n')
            with mock.patch.object(
                    lifecycle, '_cmdline',
                    return_value=[os.fsencode(sys.executable),
                                  os.fsencode(script)]):
                self.assertFalse(lifecycle.is_autotmux_daemon(123))

    def test_mismatched_start_token_prevents_signal(self):
        proc = subprocess.Popen(['sleep', '30'])
        try:
            self.assertFalse(lifecycle.signal_same_process(
                proc.pid, 'definitely-not-its-start-time', signal.SIGTERM))
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate()
            proc.wait()


class OwnedRuntimeFileTests(unittest.TestCase):
    def test_reader_rejects_symlink_fifo_and_oversized_file_without_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, 'target')
            link = os.path.join(td, 'link')
            fifo = os.path.join(td, 'fifo')
            large = os.path.join(td, 'large')
            with open(target, 'wb') as f:
                f.write(b'ok')
            os.symlink(target, link)
            os.mkfifo(fifo)
            with open(large, 'wb') as f:
                f.write(b'x' * 32)
            with self.assertRaises(OSError):
                lifecycle.read_owned_regular_file(link, 32)
            with self.assertRaises(OSError):
                lifecycle.read_owned_regular_file(fifo, 32)
            with self.assertRaises(OSError):
                lifecycle.read_owned_regular_file(large, 16)
            self.assertEqual(
                lifecycle.read_owned_regular_file(target, 32), b'ok')

    def test_reader_rejects_invalid_size_bound(self):
        with self.assertRaises(ValueError):
            lifecycle.read_owned_regular_file('/does/not/matter', -1)

    def test_deferred_reaper_uses_one_polling_thread_and_releases_handles(self):
        class DelayedProc:
            def __init__(self):
                self.done = False

            def poll(self):
                return 0 if self.done else None

        first = DelayedProc()
        second = DelayedProc()
        lifecycle.defer_popen_reap(first)
        thread = lifecycle._deferred_reaper_thread
        lifecycle.defer_popen_reap(second)
        self.assertIs(lifecycle._deferred_reaper_thread, thread)
        self.assertEqual(len(lifecycle._deferred_reaps), 2)
        first.done = second.done = True
        deadline = time.time() + 1
        while lifecycle._deferred_reaps and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(lifecycle._deferred_reaps, {})


class LockTests(unittest.TestCase):
    def test_lock_owner_pid_and_liveness(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'guard')
            fd = lifecycle.open_lock_file(path, create=True)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertTrue(lifecycle.lock_is_held(path))
                self.assertEqual(lifecycle.lock_owner_pid(path), os.getpid())
            finally:
                os.close(fd)
            self.assertFalse(lifecycle.lock_is_held(path))

    def test_lock_open_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, 'target')
            link = os.path.join(td, 'link')
            with open(target, 'w'):
                pass
            os.symlink(target, link)
            with self.assertRaises(OSError):
                lifecycle.open_lock_file(link)

    def test_live_guard_advertises_its_secure_runtime_base(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, 'runtime')
            os.mkdir(base, 0o700)
            guard = os.path.join(td, 'guard')
            fd = lifecycle.open_lock_file(guard, create=True)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                payload = json.dumps({'pid': os.getpid(), 'base': base})
                os.write(fd, payload.encode())
                self.assertEqual(lifecycle.active_runtime_base(guard), base)
            finally:
                os.close(fd)
            self.assertIsNone(lifecycle.active_runtime_base(guard))

    def test_live_guard_rejects_runtime_base_with_loose_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, 'runtime')
            os.mkdir(base, 0o700)
            guard = os.path.join(td, 'guard')
            fd = lifecycle.open_lock_file(guard, create=True)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.write(fd, json.dumps({
                    'pid': os.getpid(), 'base': base,
                }).encode())
                os.chmod(base, 0o755)
                self.assertIsNone(lifecycle.active_runtime_base(guard))
            finally:
                os.close(fd)

    def test_starting_guard_is_not_advertised_as_ready(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, 'runtime')
            os.mkdir(base, 0o700)
            guard = os.path.join(td, 'guard')
            fd = lifecycle.open_lock_file(guard, create=True)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.write(fd, json.dumps({
                    'pid': os.getpid(), 'base': base, 'ready': False,
                }).encode())
                self.assertIsNone(lifecycle.active_runtime_base(guard))
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps({
                    'pid': os.getpid(), 'base': base, 'ready': True,
                }).encode())
                self.assertEqual(lifecycle.active_runtime_base(guard), base)
            finally:
                os.close(fd)


if __name__ == '__main__':
    unittest.main()
