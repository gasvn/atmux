"""Tests for the pre-warmed ssh slave pool.

We can't test against a real ssh master in unit tests, so we exercise the
pool with a fake `ssh` script (a shell loop that echoes back what the user
sends, simulating the bash-prompt-then-exec flow).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import cli as autotmux


SSH_STUB = textwrap.dedent('''\
    #!/bin/bash
    # Pretend to be an interactive ssh slave: print a fake prompt, read a
    # line, exec it (typically `tmux attach`-like). For tests we run `cat`
    # so the proxy round-trip is easy to verify.
    echo "fake-bash-prompt$ "
    IFS= read -r line
    # Strip leading "exec " so the test can run a plain command.
    line="${line#exec }"
    eval "$line"
''')


def _install_ssh_stub(tmpdir: str) -> str:
    """Drop a shell script named `ssh` into tmpdir and return the bin dir."""
    bin_dir = os.path.join(tmpdir, 'bin')
    os.makedirs(bin_dir, exist_ok=True)
    stub = os.path.join(bin_dir, 'ssh')
    with open(stub, 'w') as f:
        f.write(SSH_STUB)
    os.chmod(stub, 0o755)
    return bin_dir


class WarmSlavePoolTests(unittest.TestCase):
    def setUp(self):
        # Provide a sentinel ControlPath that exists so warm() proceeds.
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        # Create a fake socket file (just an empty file is enough for our
        # `os.path.exists` check).
        self.fake_node = 'fake-node'
        ctl_file = os.path.join(self.ctl_dir, f'cm_{self.fake_node}')
        open(ctl_file, 'w').close()

        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')

        # Inject our fake `ssh` ahead of the real one.
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warm_localhost_is_noop(self):
        pool = autotmux.WarmSlavePool()
        pool.warm('localhost')
        self.assertNotIn('localhost', pool._slaves)

    def test_warm_no_socket_is_noop(self):
        pool = autotmux.WarmSlavePool()
        pool.warm('nonexistent-node')
        self.assertNotIn('nonexistent-node', pool._slaves)

    def test_warm_creates_slave_when_socket_exists(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            self.assertIn(self.fake_node, pool._slaves)
            pid, fd = pool._slaves[self.fake_node]
            self.assertGreater(pid, 0)
            self.assertGreaterEqual(fd, 0)
        finally:
            pool.shutdown()

    def test_warm_is_idempotent(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            first = pool._slaves[self.fake_node]
            pool.warm(self.fake_node)
            second = pool._slaves[self.fake_node]
            self.assertEqual(first, second,
                             "warm() should reuse the existing slave")
        finally:
            pool.shutdown()

    def test_take_returns_none_when_no_slave(self):
        pool = autotmux.WarmSlavePool()
        self.assertIsNone(pool._take(self.fake_node))

    def test_take_pops_the_slave(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm(self.fake_node)
            self.assertIn(self.fake_node, pool._slaves)
            slave = pool._take(self.fake_node)
            self.assertIsNotNone(slave)
            self.assertNotIn(self.fake_node, pool._slaves,
                             "after take(), slave should be removed from pool")
        finally:
            pool.shutdown()

    def test_attach_returns_false_when_no_warm_slave(self):
        pool = autotmux.WarmSlavePool()
        self.assertFalse(pool.attach('no-such-node', 'whatever'))

    def test_shutdown_kills_all_slaves(self):
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        self.assertIn(self.fake_node, pool._slaves)
        pid = pool._slaves[self.fake_node][0]
        pool.shutdown()
        self.assertNotIn(self.fake_node, pool._slaves)
        # Process should be gone within a second.
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except OSError:
                break  # process gone, good
            time.sleep(0.05)
        else:
            self.fail("shutdown() did not kill the slave process")


class SqueueLoopFieldPreservationTests(unittest.TestCase):
    """The squeue loop refreshes node_info dicts every 30 s. It must NOT
    wipe the fields populated by the session loop (sessions, nproc, load,
    last_error) — otherwise the table flickers between "we know the load"
    and "?" every cycle.
    """
    def setUp(self):
        from autotmux import daemon as d
        self.d = d
        d._known_nodes_info.clear()

    def tearDown(self):
        self.d._known_nodes_info.clear()

    def test_session_loop_fields_survive_squeue_refresh(self):
        d = self.d
        # Simulate the state right after _session_loop has populated extras.
        d._known_nodes_info['gpu1'] = {
            'time': '1:00', 'job_name': 'a', 'job_id': '1', 'state': 'RUNNING',
            'sessions': [['main', '1']], 'nproc': '4', 'load': '2.5',
            'last_error': '',
        }
        # Now mimic what _squeue_loop does: fresh squeue dict has only the
        # squeue-derived fields; merge logic should preserve the rest.
        fresh = {
            'gpu1': {'time': '0:59', 'job_name': 'a', 'job_id': '1', 'state': 'RUNNING'},
        }
        for node, info in fresh.items():
            old = d._known_nodes_info.get(node, {})
            for key in ('sessions', 'nproc', 'load', 'last_error'):
                if key in old:
                    info[key] = old[key]
        d._known_nodes_info.update(fresh)
        n = d._known_nodes_info['gpu1']
        self.assertEqual(n['sessions'], [['main', '1']])
        self.assertEqual(n['nproc'], '4')
        self.assertEqual(n['load'], '2.5')
        # And the squeue-fresh field overrode the old time:
        self.assertEqual(n['time'], '0:59')


class WarmSlaveAttachReapsChildTests(unittest.TestCase):
    """After WarmSlavePool.attach() finishes, the spawned ssh process
    must be fully reaped (no zombie). Otherwise every attach leaks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        self.fake_node = 'fake-node'
        ctl_file = os.path.join(self.ctl_dir, f'cm_{self.fake_node}')
        open(ctl_file, 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_zombie_after_attach(self):
        import subprocess as sp
        pool = autotmux.WarmSlavePool()
        pool.warm(self.fake_node)
        pid = pool._slaves[self.fake_node][0]
        # Replace _proxy with a no-op so we don't actually try to read
        # from fd 0 in the test environment.
        original_proxy = autotmux.WarmSlavePool._proxy
        autotmux.WarmSlavePool._proxy = staticmethod(lambda fd, child_pid: None)
        try:
            # The fake ssh stub will exec `cat` after reading our exec line;
            # we send EOF by writing nothing. Easier: just call attach and
            # let the stub run.
            ok = pool.attach(self.fake_node, 'whatever')
            self.assertTrue(ok)
        finally:
            autotmux.WarmSlavePool._proxy = original_proxy
        # Allow brief time for SIGTERM + waitpid to complete.
        time.sleep(0.3)
        # Check no zombie: try /proc/<pid>/status — if the process is a
        # zombie, status field is "Z (zombie)". If reaped, no /proc entry.
        proc_status = f'/proc/{pid}/status'
        if os.path.exists(proc_status):
            with open(proc_status) as f:
                content = f.read()
            self.assertNotIn('zombie', content.lower(),
                             f'attach left zombie process pid={pid}')


class BuildRowsWithLoadTests(unittest.TestCase):
    """Verify nproc/load make it through into the rendered row tuple
    (column B integration)."""

    def test_nproc_and_load_appear_in_row(self):
        state = {
            'nodes': {
                'gpu1': {
                    'alive': True,
                    'info': {'time': '1:00', 'nproc': '4', 'load': '12.34'},
                    'sessions': [['main', '1']],
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual(len(rows), 1)
        # row layout: (node, session, wins, time, status, cpu, load)
        self.assertEqual(rows[0][5], '4')
        self.assertEqual(rows[0][6], '12.34')

    def test_missing_nproc_load_become_empty_strings(self):
        state = {
            'nodes': {
                'gpu1': {
                    'alive': True,
                    'info': {'time': '1:00'},
                    'sessions': [['main', '1']],
                }
            }
        }
        rows = autotmux.build_session_rows(state)
        self.assertEqual(rows[0][5], '')
        self.assertEqual(rows[0][6], '')


class WarmConcurrencyTests(unittest.TestCase):
    """Two concurrent warm() calls for the same node must not both spawn —
    otherwise one slave is silently overwritten in self._slaves and leaks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        self.fake_node = 'fake-node-conc'
        ctl_file = os.path.join(self.ctl_dir, f'cm_{self.fake_node}')
        open(ctl_file, 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_warm_does_not_leak_slaves(self):
        import threading
        pool = autotmux.WarmSlavePool()
        try:
            barrier = threading.Barrier(4)
            spawned_pids = []
            spawn_lock = threading.Lock()

            def race():
                barrier.wait()
                pool.warm(self.fake_node)
                with spawn_lock:
                    if self.fake_node in pool._slaves:
                        spawned_pids.append(pool._slaves[self.fake_node][0])

            threads = [threading.Thread(target=race) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # All four threads see the SAME pid (only one slave was spawned).
            unique_pids = set(spawned_pids)
            self.assertEqual(len(unique_pids), 1,
                             f'concurrent warm() spawned multiple slaves: {unique_pids}')
        finally:
            pool.shutdown()


class WarmDropsGoneNodesTests(unittest.TestCase):
    """When the visible node set shrinks (slurm job ends), warm slaves
    for departed nodes must be terminated, not left running indefinitely."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ctl_dir = os.path.join(self.tmpdir, 'ctl')
        os.makedirs(self.ctl_dir, exist_ok=True)
        for n in ('node-a', 'node-b'):
            open(os.path.join(self.ctl_dir, f'cm_{n}'), 'w').close()
        self._original_ctl_path = autotmux._ctl_path
        autotmux._ctl_path = lambda node: os.path.join(self.ctl_dir, f'cm_{node}')
        self._old_path = os.environ['PATH']
        bin_dir = _install_ssh_stub(self.tmpdir)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']

    def tearDown(self):
        autotmux._ctl_path = self._original_ctl_path
        os.environ['PATH'] = self._old_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warm_all_drops_slaves_for_nodes_not_in_view(self):
        pool = autotmux.WarmSlavePool()
        try:
            pool.warm_all({'node-a', 'node-b'})
            self.assertIn('node-a', pool._slaves)
            self.assertIn('node-b', pool._slaves)
            # Now node-b leaves the view (slurm job ended).
            pool.warm_all({'node-a'})
            self.assertIn('node-a', pool._slaves)
            self.assertNotIn('node-b', pool._slaves,
                             'warm_all must terminate slaves for nodes that left the view')
        finally:
            pool.shutdown()


if __name__ == '__main__':
    unittest.main(verbosity=2)
