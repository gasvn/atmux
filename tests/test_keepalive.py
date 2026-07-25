"""Tests for keep-alive auto-renew (pure logic + manager with stubbed sbatch)."""
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import keepalive as ka


CFG = {'enabled': True, 'lead_time': 900, 'cooldown': 600,
       'max_failures': 3, 'submit_timeout': 60}


class TimeLeftParseTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(ka.parse_time_left('15:00'), 900)
        self.assertEqual(ka.parse_time_left('1:15:00'), 4500)
        self.assertEqual(ka.parse_time_left('1-23:27:04'), 86400 + 23*3600 + 27*60 + 4)
        self.assertEqual(ka.parse_time_left('45'), 45)
        self.assertEqual(ka.parse_time_left('2-00:00:00'), 2*86400)

    def test_unlimited_and_unknown(self):
        self.assertEqual(ka.parse_time_left('UNLIMITED'), math.inf)
        self.assertIsNone(ka.parse_time_left(''))
        self.assertIsNone(ka.parse_time_left('NOT_SET'))
        self.assertIsNone(ka.parse_time_left('INVALID'))
        self.assertIsNone(ka.parse_time_left('N/A'))
        self.assertIsNone(ka.parse_time_left('bogus:x'))


class ScontrolParseTests(unittest.TestCase):
    SAMPLE = (
        "JobId=31363559 JobName=h100x1\n"
        "   UserId=shgao(64257) GroupId=x(1)\n"
        "   Requeue=1 Restarts=0 BatchFlag=1 ExitCode=0:0\n"
        "   Command=/n/home12/shgao/h100x1\n"
        "   WorkDir=/n/home12/shgao\n"
    )

    def test_batch_job(self):
        r = ka.parse_scontrol(self.SAMPLE)
        self.assertEqual(r['job_name'], 'h100x1')
        self.assertEqual(r['command'], '/n/home12/shgao/h100x1')
        self.assertEqual(r['workdir'], '/n/home12/shgao')
        self.assertTrue(r['batch'])

    def test_interactive_job_not_batch(self):
        txt = "JobId=1 JobName=bash\n   BatchFlag=0\n   WorkDir=/home/x\n"
        r = ka.parse_scontrol(txt)
        self.assertFalse(r['batch'])
        self.assertIsNone(r['command'])

    def test_command_with_args(self):
        txt = "JobName=j\n   BatchFlag=1\n   Command=/home/x/run.sh --gpu 1\n   WorkDir=/home/x\n"
        r = ka.parse_scontrol(txt)
        self.assertEqual(r['command'], '/home/x/run.sh --gpu 1')


class DecideTests(unittest.TestCase):
    def rt(self, **kw):
        base = {'attempts': 0, 'last_submit': None, 'in_flight': False}
        base.update(kw)
        return base

    def test_fresh_running_job_no_action(self):
        m = [{'state': 'RUNNING', 'time_left': 5000}]
        self.assertEqual(ka.decide(m, self.rt(), 1000, CFG), ('none', 'healthy'))

    def test_pending_replacement_counts_as_fresh(self):
        m = [{'state': 'RUNNING', 'time_left': 60},
             {'state': 'PENDING', 'time_left': None}]
        self.assertEqual(ka.decide(m, self.rt(), 1000, CFG), ('none', 'healthy'))

    def test_all_expiring_triggers_submit(self):
        m = [{'state': 'RUNNING', 'time_left': 300}]
        self.assertEqual(ka.decide(m, self.rt(), 1000, CFG), ('submit', 'renewing'))

    def test_gone_triggers_submit(self):
        self.assertEqual(ka.decide([], self.rt(), 1000, CFG), ('submit', 'renewing'))

    def test_within_cooldown_waits(self):
        rt = self.rt(last_submit=1000)
        self.assertEqual(ka.decide([], rt, 1300, CFG), ('wait', 'renewing'))

    def test_after_cooldown_resubmits(self):
        rt = self.rt(last_submit=1000)
        self.assertEqual(ka.decide([], rt, 2000, CFG), ('submit', 'renewing'))

    def test_in_flight_no_double_submit(self):
        rt = self.rt(in_flight=True)
        self.assertEqual(ka.decide([], rt, 5000, CFG), ('none', 'renewing'))

    def test_failure_cap_pauses(self):
        rt = self.rt(attempts=3)
        self.assertEqual(ka.decide([], rt, 5000, CFG), ('paused', 'paused'))

    def test_unlimited_is_fresh(self):
        m = [{'state': 'RUNNING', 'time_left': math.inf}]
        self.assertEqual(ka.decide(m, self.rt(), 1000, CFG), ('none', 'healthy'))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'sub', 'keepalive.json')

    def test_toggle_round_trip(self):
        self.assertEqual(ka.load_registry(self.path), [])
        on = ka.toggle_entry(self.path, 'j1', '/home/x/j1', '/home/x')
        self.assertTrue(on)
        entries = ka.load_registry(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['job_name'], 'j1')
        self.assertEqual(entries[0]['command'], '/home/x/j1')
        self.assertTrue(entries[0]['enabled'])
        # toggling again removes it
        off = ka.toggle_entry(self.path, 'j1', '/home/x/j1', '/home/x')
        self.assertFalse(off)
        self.assertEqual(ka.load_registry(self.path), [])

    def test_toggle_reenables_disabled_entry(self):
        ka.save_registry(self.path, [{'job_name': 'j1', 'command': '/x',
                                      'workdir': '/w', 'enabled': False}])
        # A disabled entry must re-enable (return True), not be silently deleted.
        on = ka.toggle_entry(self.path, 'j1', '/x2', '/w2')
        self.assertTrue(on)
        entries = ka.load_registry(self.path)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]['enabled'])
        self.assertEqual(entries[0]['command'], '/x2')

    def test_bad_file_reads_empty(self):
        with open(self.path.replace('/sub/', '/'), 'w') as f:
            f.write('not json')
        self.assertEqual(ka.load_registry(self.path.replace('/sub/', '/')), [])


class ConfigClampTests(unittest.TestCase):
    def setUp(self):
        from autotmux import config as cfgmod
        self.cfgmod = cfgmod
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'config.toml')
        self._prev = os.environ.get('AUTOTMUX_CONFIG')
        os.environ['AUTOTMUX_CONFIG'] = self.path

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('AUTOTMUX_CONFIG', None)
        else:
            os.environ['AUTOTMUX_CONFIG'] = self._prev

    def _write(self, body):
        with open(self.path, 'w') as f:
            f.write(body)

    def test_clamps_nonsensical_values(self):
        self._write('[keepalive]\nmax_failures = 0\nsubmit_timeout = 0\n'
                    'cooldown = -5\nlead_time = -10\n')
        cfg = self.cfgmod.load_keepalive()
        self.assertGreaterEqual(cfg['max_failures'], 1)
        self.assertGreaterEqual(cfg['submit_timeout'], 1)
        self.assertGreaterEqual(cfg['cooldown'], 0)
        self.assertGreaterEqual(cfg['lead_time'], 0)

    def test_defaults_when_no_file(self):
        cfg = self.cfgmod.load_keepalive()
        self.assertEqual(cfg['max_failures'], 3)
        self.assertTrue(cfg['enabled'])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'keepalive.json')
        self.submits = []

        def fake_submit(command, workdir):
            self.submits.append((command, workdir))
            return (self._submit_ok, '' if self._submit_ok else 'boom')
        self._submit_ok = True
        self.mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=fake_submit)

    def _register(self, name='j1', command='/home/x/j1', workdir='/home/x'):
        ka.save_registry(self.path, [{'job_name': name, 'command': command,
                                      'workdir': workdir, 'enabled': True}])

    def _wait_submits(self, n, timeout=2.0):
        end = time.time() + timeout
        while len(self.submits) < n and time.time() < end:
            time.sleep(0.02)

    def test_healthy_job_no_submit(self):
        self._register()
        self.mgr.tick([{'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00:00'}])
        self._wait_submits(1, 0.3)
        self.assertEqual(self.submits, [])
        self.assertEqual(self.mgr.status()['j1']['state'], 'healthy')

    def test_expiring_job_submits_once(self):
        self._register()
        rows = [{'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00'}]
        self.mgr.tick(rows, now=1000)
        self._wait_submits(1)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.submits[0], ('/home/x/j1', '/home/x'))
        # A second tick inside cooldown must not submit again.
        self.mgr.tick(rows, now=1030)
        self._wait_submits(2, 0.3)
        self.assertEqual(len(self.submits), 1)

    def test_gone_job_submits(self):
        self._register()
        self.mgr.tick([], now=1000)
        self._wait_submits(1)
        self.assertEqual(len(self.submits), 1)

    def test_failure_cap_pauses(self):
        self._submit_ok = False
        self._register()
        now = 1000
        for _ in range(5):
            self.mgr.tick([], now=now)
            self._wait_submits(len(self.submits) + 1, 0.3)
            now += CFG['cooldown'] + 1
        self.assertEqual(self.mgr.status()['j1']['state'], 'paused')
        self.assertLessEqual(len(self.submits), CFG['max_failures'])

    def test_unregister_prunes_status(self):
        self._register()
        self.mgr.tick([{'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00:00'}])
        self.assertIn('j1', self.mgr.status())
        ka.save_registry(self.path, [])
        # mtime must change for the reload to pick it up
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        self.mgr.tick([])
        self.assertNotIn('j1', self.mgr.status())

    def test_pending_replacement_no_submit(self):
        # A queued (PENDING) replacement of the same name must read as fresh —
        # the daemon now feeds pending rows precisely so this works.
        self._register()
        rows = [{'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00'},
                {'name': 'j1', 'state': 'PENDING', 'time_left': '2-00:00:00'}]
        self.mgr.tick(rows, now=1000)
        self._wait_submits(1, 0.3)
        self.assertEqual(self.submits, [])
        self.assertEqual(self.mgr.status()['j1']['state'], 'healthy')

    def test_cooldown_survives_healthy_flap(self):
        self._register()
        self.mgr.tick([{'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00'}], now=1000)
        self._wait_submits(1)
        self.assertEqual(len(self.submits), 1)
        # replacement appears (healthy), then momentarily vanishes within cooldown
        self.mgr.tick([{'name': 'j1', 'state': 'PENDING', 'time_left': '2-00:00:00'}], now=1030)
        self.mgr.tick([], now=1060)
        self._wait_submits(2, 0.3)
        self.assertEqual(len(self.submits), 1,
                         'cooldown must survive a fresh→gone flap')

    def test_poll_needed(self):
        self.assertFalse(self.mgr.poll_needed())   # no registry file yet
        self._register()
        self.assertTrue(self.mgr.poll_needed())
        cfg = dict(CFG); cfg['enabled'] = False
        self.assertFalse(ka.KeepAliveManager(self.path, cfg).poll_needed())

    def test_toggle_off_midflight_no_resurrect(self):
        import threading
        started, release = threading.Event(), threading.Event()

        def blocking_submit(cmd, wd):
            started.set()
            release.wait(2)
            return (True, '')
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=blocking_submit)
        self._register()
        mgr.tick([], now=1000)               # fires submit; thread blocks
        self.assertTrue(started.wait(1))
        ka.save_registry(self.path, [])       # user toggles OFF
        mgr.tick([], now=1001)                # _reload drops j1 runtime
        self.assertNotIn('j1', mgr.status())
        release.set()                         # submit thread finishes
        time.sleep(0.2)
        self.assertNotIn('j1', mgr.status(),  # must not be resurrected
                         'a completing submit must not recreate de-registered runtime')

    def test_tracks_submitted_id_across_name_mismatch(self):
        # The replacement comes up under a DIFFERENT name (e.g. --job-name set
        # on the sbatch CLI). Tracking the submitted job id must still recognise
        # it as ours — no re-submit, no false pause.
        def submit(cmd, wd):
            return (True, '', '55555')
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        self._register(name='j1')
        mgr.tick([{'id': '100', 'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00'}],
                 now=1000)
        end = time.time() + 1.0
        while mgr._runtime.get('j1', {}).get('submitted_id') is None and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(mgr._runtime['j1']['submitted_id'], '55555')
        # Replacement appears PENDING under a different name, same id.
        mgr.tick([{'id': '55555', 'name': 'OTHER', 'state': 'PENDING',
                   'time_left': '2-00:00:00'}], now=1100)
        self.assertEqual(mgr.status()['j1']['state'], 'healthy')
        self.assertEqual(mgr.status()['j1']['attempts'], 0)

    def test_success_never_pauses(self):
        # Even if the replacement is never observed, successful submits must not
        # trip the failure cap (attempts counts FAILURES only).
        calls = []
        def submit(cmd, wd):
            calls.append(1)
            return (True, '', None)
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        self._register()
        now = 1000
        for _ in range(6):
            mgr.tick([], now=now)
            end = time.time() + 0.3
            while mgr._runtime.get('j1', {}).get('in_flight') and time.time() < end:
                time.sleep(0.02)
            now += CFG['cooldown'] + 1
        self.assertNotEqual(mgr.status()['j1']['state'], 'paused')

    def test_submit_exception_does_not_latch_inflight(self):
        # If the submit function raises, in_flight must be cleared so the entry
        # can renew again — otherwise decide() returns 'renewing' forever.
        def boom(cmd, wd):
            raise ValueError('bad quoting in command')
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=boom)
        self._register()
        mgr.tick([], now=1000)
        # let the submit thread run and clear in_flight
        end = time.time() + 1.0
        while mgr._runtime.get('j1', {}).get('in_flight', True) and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(mgr._runtime['j1']['in_flight'],
                         'a raising submit must not latch in_flight True')

    def test_disabled_feature_no_submit(self):
        cfg = dict(CFG); cfg['enabled'] = False
        mgr = ka.KeepAliveManager(self.path, cfg, submit_fn=lambda c, w: self.submits.append((c, w)) or (True, ''))
        self._register()
        mgr.tick([], now=1000)
        self._wait_submits(1, 0.3)
        self.assertEqual(self.submits, [])


if __name__ == '__main__':
    unittest.main()
