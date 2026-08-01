"""Tests for keep-alive auto-renew (pure logic + manager with stubbed sbatch)."""
import math
import os
import sys
import tempfile
import time
import unittest
import threading
from unittest import mock

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
        self.assertIsNone(ka.parse_time_left('-5'))
        self.assertIsNone(ka.parse_time_left(None))


class JobIdentityTests(unittest.TestCase):
    def test_array_and_heterogeneous_rows_share_their_job_family(self):
        self.assertEqual(ka.job_family_id('123'), '123')
        self.assertEqual(ka.job_family_id('123_4'), '123')
        self.assertEqual(ka.job_family_id('123_[1-8%2]'), '123')
        self.assertEqual(ka.job_family_id('123+1'), '123')

    def test_option_like_or_malformed_job_ids_are_rejected(self):
        for value in ('-Q', '123;cancel', '', None, True):
            self.assertIsNone(ka.job_family_id(value))


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

    def test_cross_daemon_defer_window_waits_without_counting_failure(self):
        rt = self.rt(last_submit=None, defer_until=1100)
        self.assertEqual(ka.decide([], rt, 1000, CFG), ('wait', 'renewing'))
        self.assertEqual(ka.decide([], rt, 1100, CFG), ('submit', 'renewing'))

    def test_after_cooldown_resubmits(self):
        rt = self.rt(last_submit=1000)
        self.assertEqual(ka.decide([], rt, 2000, CFG), ('submit', 'renewing'))

    def test_in_flight_no_double_submit(self):
        rt = self.rt(in_flight=True)
        self.assertEqual(ka.decide([], rt, 5000, CFG), ('none', 'renewing'))

    def test_failure_cap_pauses(self):
        rt = self.rt(attempts=3)
        self.assertEqual(ka.decide([], rt, 5000, CFG), ('paused', 'paused'))

    def test_healthy_job_recovers_a_previously_paused_entry(self):
        rt = self.rt(attempts=CFG['max_failures'])
        matching = [{'state': 'RUNNING', 'time_left': 5000}]
        self.assertEqual(
            ka.decide(matching, rt, 5000, CFG), ('none', 'healthy'))

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

    def test_explicit_enable_is_idempotent_instead_of_toggling_off(self):
        self.assertTrue(ka.set_entry_enabled(
            self.path, 'j1', True, '/x/one', '/w'))
        self.assertTrue(ka.set_entry_enabled(
            self.path, 'j1', True, '/x/two', '/w2'))
        entries = ka.load_registry(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['command'], '/x/two')

    def test_explicit_disable_is_idempotent(self):
        ka.set_entry_enabled(self.path, 'j1', True, '/x/one', '/w')
        self.assertFalse(ka.set_entry_enabled(self.path, 'j1', False))
        self.assertFalse(ka.set_entry_enabled(self.path, 'j1', False))
        self.assertEqual(ka.load_registry(self.path), [])

    def test_explicit_enable_rejects_empty_command(self):
        with self.assertRaises(ValueError):
            ka.set_entry_enabled(self.path, 'j1', True, '', '/w')
        self.assertEqual(ka.load_registry(self.path), [])

    def test_same_name_jobs_are_independent_when_job_ids_are_present(self):
        ka.set_entry_enabled(
            self.path, 'h100x2', True, '/x/a', '/w',
            job_id='101', entry_id='entry-a')
        ka.set_entry_enabled(
            self.path, 'h100x2', True, '/x/b', '/w',
            job_id='202', entry_id='entry-b')
        entries = ka.load_registry(self.path)
        self.assertEqual({e['job_id'] for e in entries}, {'101', '202'})

        ka.set_entry_enabled(
            self.path, 'h100x2', False,
            job_id='101', entry_id='entry-a')
        entries = ka.load_registry(self.path)
        self.assertEqual([(e['entry_id'], e['job_id']) for e in entries],
                         [('entry-b', '202')])

    def test_submit_completion_advances_only_the_same_uuid(self):
        ka.save_registry(self.path, [
            {'entry_id': 'keep', 'job_id': '101', 'job_name': 'same',
             'command': '/x/a', 'workdir': '/w', 'enabled': True},
            {'entry_id': 'other', 'job_id': '202', 'job_name': 'same',
             'command': '/x/b', 'workdir': '/w', 'enabled': True},
        ])
        self.assertTrue(ka.update_entry_after_submit(
            self.path, 'keep', '303', submitted_at=123.0))
        entries = {e['entry_id']: e for e in ka.load_registry(self.path)}
        self.assertEqual(entries['keep']['job_id'], '303')
        self.assertEqual(entries['keep']['last_submit_at'], 123.0)
        self.assertEqual(entries['other']['job_id'], '202')

    def test_submit_claim_serializes_hosts_and_preserves_ambiguous_cooldown(self):
        ka.save_registry(self.path, [{
            'entry_id': 'tracked', 'job_id': '101', 'job_name': 'same',
            'command': '/x/a', 'workdir': '/w', 'enabled': True,
        }])
        first = ka.claim_entry_for_submit(
            self.path, 'tracked', '101', owner_id='host-a',
            cooldown=60, lease_seconds=90, now=100)
        self.assertTrue(first['token'])
        second = ka.claim_entry_for_submit(
            self.path, 'tracked', '101', owner_id='host-b',
            cooldown=60, lease_seconds=90, now=101)
        self.assertIsNone(second['token'])
        self.assertIn('claimed', second['reason'])

        # A timeout/failure can be ambiguous: clear the active lease but retain
        # a shared cooldown so another host cannot duplicate an accepted job.
        self.assertTrue(ka.finish_submit_claim(
            self.path, first['token'], success=False, owner_id='host-a',
            submitted_at=102, submitted_monotonic=50,
            clock_id='clock-a', record_cooldown=True))
        denied = ka.claim_entry_for_submit(
            self.path, 'tracked', '101', owner_id='host-b',
            cooldown=60, lease_seconds=90, now=103)
        self.assertIsNone(denied['token'])
        self.assertIn('cooldown', denied['reason'])
        allowed = ka.claim_entry_for_submit(
            self.path, 'tracked', '101', owner_id='host-b',
            cooldown=60, lease_seconds=90, now=162)
        self.assertTrue(allowed['token'])

    def test_successful_claim_advances_id_and_requires_matching_owner(self):
        ka.save_registry(self.path, [{
            'entry_id': 'tracked', 'job_id': '101', 'job_name': 'same',
            'command': '/x/a', 'workdir': '/w', 'enabled': True,
        }])
        claim = ka.claim_entry_for_submit(
            self.path, 'tracked', '101', owner_id='owner',
            cooldown=60, lease_seconds=90, now=100)
        self.assertFalse(ka.finish_submit_claim(
            self.path, claim['token'], success=True, owner_id='wrong',
            job_id='303', submitted_at=101))
        self.assertIn('submit_claim', ka.load_registry(self.path)[0])
        self.assertTrue(ka.finish_submit_claim(
            self.path, claim['token'], success=True, owner_id='owner',
            job_id='303', submitted_at=101, submitted_monotonic=77,
            clock_id='clock'))
        entry = ka.load_registry(self.path)[0]
        self.assertNotIn('submit_claim', entry)
        self.assertEqual(entry['job_id'], '303')
        self.assertEqual(entry['last_submit_at'], 101.0)
        self.assertEqual(entry['last_submit_monotonic'], 77.0)
        self.assertEqual(entry['last_submit_clock_id'], 'clock')

    def test_oversized_registry_is_not_overwritten_as_if_empty(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w') as f:
            f.write('{"entries": [' + (' ' * 128) + ']}')
        with mock.patch.object(ka, '_REGISTRY_FILE_LIMIT', 32):
            self.assertEqual(ka.load_registry(self.path), [])
            with self.assertRaises(OSError):
                ka.set_entry_enabled(self.path, 'new', True, '/x', '/w')

    def test_bad_file_reads_empty(self):
        with open(self.path.replace('/sub/', '/'), 'w') as f:
            f.write('not json')
        self.assertEqual(ka.load_registry(self.path.replace('/sub/', '/')), [])

    def test_concurrent_distinct_toggles_do_not_lose_entries(self):
        count = 12
        barrier = threading.Barrier(count)

        def add(i):
            barrier.wait()
            ka.toggle_entry(self.path, f'j{i}', f'/x/{i}', '/x')

        threads = [threading.Thread(target=add, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual({entry['job_name'] for entry in ka.load_registry(self.path)},
                         {f'j{i}' for i in range(count)})

    def test_save_supports_relative_path_and_leaves_no_temp(self):
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            ka.save_registry('keepalive.json', [])
            self.assertEqual(ka.load_registry('keepalive.json'), [])
            self.assertEqual(os.stat('keepalive.json').st_mode & 0o777, 0o600)
            self.assertEqual([name for name in os.listdir('.') if '.tmp.' in name], [])
        finally:
            os.chdir(old_cwd)

    def test_toggle_refuses_to_overwrite_corrupt_registry(self):
        ka.save_registry(self.path, [{'job_name': 'important', 'enabled': True}])
        with open(self.path, 'w') as f:
            f.write('{broken json')
        with open(self.path, 'rb') as f:
            before = f.read()
        with self.assertRaises(OSError):
            ka.toggle_entry(self.path, 'new', '/x', '/w')
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), before)


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

    def test_paused_entry_auto_recovers_when_job_becomes_healthy(self):
        self._register()
        self.mgr.poll_needed()
        self.mgr._runtime['j1'] = {
            'attempts': CFG['max_failures'], 'last_submit': 1000,
            'in_flight': False, 'state': 'paused', 'last_error': 'old failure',
            'submitted_id': None,
        }
        self.mgr.tick([
            {'name': 'j1', 'state': 'RUNNING', 'time_left': '5:00:00'}
        ], now=2000)
        status = self.mgr.status()['j1']
        self.assertEqual(status['state'], 'healthy')
        self.assertEqual(status['attempts'], 0)
        self.assertEqual(status['last_error'], '')

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
        cfg = dict(CFG)
        cfg['enabled'] = False
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

    def test_unregistered_same_name_job_does_not_mask_tracked_expiry(self):
        calls = []

        def submit(command, workdir):
            calls.append((command, workdir))
            return (True, '', '303')

        ka.save_registry(self.path, [{
            'entry_id': 'tracked', 'job_id': '101', 'job_name': 'h100x2',
            'command': '/x/tracked', 'workdir': '/w', 'enabled': True,
        }])
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        mgr.tick([
            {'id': '101', 'name': 'h100x2', 'state': 'RUNNING',
             'time_left': '5:00'},
            {'id': '202', 'name': 'h100x2', 'state': 'RUNNING',
             'time_left': '5:00:00'},
        ], now=1000)
        end = time.time() + 1
        while not calls and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(calls, [('/x/tracked', '/w')])

    def test_submitted_job_id_and_cooldown_survive_daemon_restart(self):
        calls = []

        def submit(command, workdir):
            calls.append((command, workdir))
            return (True, '', '303')

        ka.save_registry(self.path, [{
            'entry_id': 'tracked', 'job_id': '101', 'job_name': 'original',
            'command': '/x/tracked', 'workdir': '/w', 'enabled': True,
        }])
        first = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        first.tick([], now=1000)
        end = time.time() + 1
        while time.time() < end:
            entries = ka.load_registry(self.path)
            if entries and entries[0].get('job_id') == '303':
                break
            time.sleep(0.01)
        self.assertEqual(ka.load_registry(self.path)[0]['job_id'], '303')

        # A fresh manager represents a daemon restart. Before squeue exposes
        # the replacement, persisted cooldown must prevent a duplicate sbatch.
        second = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        second.tick([], now=5000)
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)
        # Once visible, the exact replacement ID is healthy even if its name
        # differs from the original script's JobName.
        second.tick([{'id': '303', 'name': 'renamed', 'state': 'PENDING',
                      'time_left': '2-00:00:00'}], now=5010)
        self.assertEqual(second.status()['tracked']['state'], 'healthy')

    def test_two_managers_cannot_submit_the_same_entry_concurrently(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        def submit(command, workdir):
            with calls_lock:
                calls.append((command, workdir))
            started.set()
            release.wait(2)
            return (True, '', '303')

        ka.save_registry(self.path, [{
            'entry_id': 'tracked', 'job_id': '101', 'job_name': 'same',
            'command': '/x/a', 'workdir': '/w', 'enabled': True,
        }])
        first = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        second = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=submit)
        first.tick([], now=1000)
        self.assertTrue(started.wait(1))
        second.tick([], now=1000)
        time.sleep(0.2)
        try:
            self.assertEqual(calls, [('/x/a', '/w')])
            self.assertFalse(second._runtime['tracked']['in_flight'])
            self.assertEqual(second._runtime['tracked']['attempts'], 0)
            self.assertIn('claimed', second._runtime['tracked']['last_error'])
        finally:
            release.set()

    def test_foreign_monotonic_clock_uses_persisted_wall_time(self):
        entry = {
            'entry_id': 'tracked', 'job_id': '101', 'job_name': 'same',
            'command': '/x/a', 'workdir': '/w', 'enabled': True,
            'last_submit_monotonic': 1999.0,
            'last_submit_clock_id': 'another-host:another-boot',
            'last_submit_at': 950.0,
        }
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=lambda *_: None)
        with mock.patch.object(ka.time, 'time', return_value=1000.0):
            rt = mgr._rt('tracked', entry, now=2000.0)
        self.assertEqual(rt['last_submit'], 1950.0)

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
        cfg = dict(CFG)
        cfg['enabled'] = False
        mgr = ka.KeepAliveManager(self.path, cfg, submit_fn=lambda c, w: self.submits.append((c, w)) or (True, ''))
        self._register()
        mgr.tick([], now=1000)
        self._wait_submits(1, 0.3)
        self.assertEqual(self.submits, [])

    def test_submit_threads_are_hard_capped(self):
        release = threading.Event()
        started = []
        started_lock = threading.Lock()

        def block(command, workdir):
            with started_lock:
                started.append(command)
            release.wait(2)
            return (True, '')

        entries = [
            {'job_name': f'j{i}', 'command': f'/x/{i}',
             'workdir': '/x', 'enabled': True}
            for i in range(20)
        ]
        ka.save_registry(self.path, entries)
        mgr = ka.KeepAliveManager(self.path, dict(CFG), submit_fn=block)
        mgr.tick([], now=1000)
        deadline = time.time() + 1
        while len(started) < 4 and time.time() < deadline:
            time.sleep(0.01)
        try:
            self.assertEqual(len(started), 4)
            self.assertEqual(sum(bool(rt['in_flight']) for rt in mgr._runtime.values()), 4)
        finally:
            release.set()

    def test_transient_registry_read_failure_retains_last_good_entries(self):
        self._register()
        self.assertTrue(self.mgr.poll_needed())
        self.mgr._mtime = ('force-reload',)
        with mock.patch.object(ka, '_load_registry_checked', return_value=(False, [])):
            self.assertTrue(self.mgr.poll_needed())
        self.assertIn('j1', self.mgr._entries)

    def test_status_never_waits_forever_for_registry_lock(self):
        self.mgr._lock.acquire()
        try:
            started = time.monotonic()
            self.assertEqual(self.mgr.status(), {})
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            self.mgr._lock.release()

    def test_sbatch_hard_deadline_clears_stuck_timeout_cleanup(self):
        cfg = dict(CFG)
        cfg['submit_timeout'] = 0.1
        mgr = ka.KeepAliveManager(self.path, cfg)
        mgr._command_cleanup_grace = 0.01
        release = threading.Event()

        def stuck_run(*_args, **_kwargs):
            release.wait(2)
            return mock.Mock(returncode=0, stdout='Submitted batch job 1', stderr='')

        started = time.monotonic()
        try:
            with mock.patch.object(ka.subprocess, 'run', side_effect=stuck_run):
                ok, error, _job_id = mgr._sbatch('/tmp/job.sh', None)
            self.assertFalse(ok)
            self.assertIn('cleanup is stuck', error)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            time.sleep(0.05)

    def test_sbatch_uses_machine_readable_job_id_output(self):
        mgr = ka.KeepAliveManager(self.path, dict(CFG))
        result = mock.Mock(returncode=0, stdout='303;primary\n', stderr='')
        with mock.patch.object(ka.subprocess, 'run', return_value=result) as run:
            self.assertEqual(mgr._sbatch('/tmp/job.sh --arg x', '/tmp'),
                             (True, '', '303'))
        self.assertEqual(
            run.call_args.args[0],
            ['sbatch', '--parsable', '--', '/tmp/job.sh', '--arg', 'x'])

    def test_local_command_capacity_does_not_pause_an_unattempted_entry(self):
        self._register()
        mgr = ka.KeepAliveManager(self.path, dict(CFG))
        mgr._command_slots = threading.Semaphore(0)
        now = 1000
        for _ in range(CFG['max_failures'] + 2):
            mgr.tick([], now=now)
            end = time.time() + 0.5
            while mgr._runtime.get('j1', {}).get('in_flight') and time.time() < end:
                time.sleep(0.01)
            self.assertIsNone(mgr._runtime['j1']['last_submit'])
            now += 1
        status = mgr.status()['j1']
        self.assertEqual(status['attempts'], 0)
        self.assertEqual(status['state'], 'renewing')
        self.assertIn('capacity exhausted', status['last_error'])


if __name__ == '__main__':
    unittest.main()
