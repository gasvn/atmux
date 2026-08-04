"""Tests for the job-expiry reminder webhook."""
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import config, notify


def _job(job_id='1', time_left='0:45:00', state='RUNNING', **extra):
    return {'job_id': job_id, 'job_name': 'train', 'node': 'gpu1',
            'state': state, 'time': time_left, **extra}


class DueJobTests(unittest.TestCase):
    LEAD = 3600

    def _due(self, jobs, already=()):
        return [j['job_id'] for j in notify.due_jobs(jobs, self.LEAD, set(already))]

    def test_job_inside_the_window_is_due(self):
        self.assertEqual(self._due([_job(time_left='0:45:00')]), ['1'])

    def test_job_outside_the_window_is_not(self):
        self.assertEqual(self._due([_job(time_left='5:00:00')]), [])

    def test_boundary_is_inclusive(self):
        self.assertEqual(self._due([_job(time_left='1:00:00')]), ['1'])
        self.assertEqual(self._due([_job(time_left='1:00:01')]), [])

    def test_already_announced_jobs_stay_quiet(self):
        self.assertEqual(self._due([_job()], already={'1'}), [])

    def test_a_job_spanning_several_nodes_is_announced_once(self):
        jobs = [_job(node='gpu1'), dict(_job(node='gpu2'))]
        self.assertEqual(self._due(jobs), ['1'])

    def test_unlimited_jobs_never_expire_so_never_remind(self):
        for value in ('UNLIMITED', 'INFINITE'):
            self.assertEqual(self._due([_job(time_left=value)]), [])

    def test_unknown_remaining_time_is_not_treated_as_ending(self):
        """An unparseable %L is not evidence of anything; guessing here would
        fire a false alarm on every poll."""
        for value in ('', 'N/A', 'NOT_SET', 'INVALID', 'garbage', None):
            with self.subTest(value=value):
                self.assertEqual(self._due([_job(time_left=value)]), [])

    def test_only_running_jobs_are_considered(self):
        for state in ('PENDING', 'COMPLETING', 'SUSPENDED'):
            with self.subTest(state=state):
                self.assertEqual(self._due([_job(state=state)]), [])
        self.assertEqual(self._due([_job(state='RUNNING')]), ['1'])

    def test_malformed_entries_are_skipped_not_fatal(self):
        self.assertEqual(self._due(['nope', None, 42, {}, _job()]), ['1'])


class MessageTests(unittest.TestCase):
    def test_message_names_the_job_node_and_time(self):
        text = notify.build_message(_job(), 2700)
        self.assertIn('train', text)
        self.assertIn('(1)', text)
        self.assertIn('gpu1', text)
        self.assertIn('45m', text)

    def test_remaining_is_readable(self):
        self.assertEqual(notify.format_remaining(3600), '1h')
        self.assertEqual(notify.format_remaining(3900), '1h 5m')
        self.assertEqual(notify.format_remaining(300), '5m')
        self.assertEqual(notify.format_remaining(-5), '0m')

    def test_message_is_bounded(self):
        long_job = _job(job_name='x' * 5000)
        self.assertLessEqual(len(notify.build_message(long_job, 60)), 2000)

    def test_missing_fields_do_not_raise(self):
        self.assertIn('?', notify.build_message({}, 60))


class _Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


class PostTests(unittest.TestCase):
    URL = 'https://hooks.slack.com/services/xxx'

    def test_success_sends_slack_shaped_json(self):
        seen = {}

        def fake(request, timeout=None):
            seen['url'] = request.full_url
            seen['body'] = request.data
            seen['type'] = request.get_header('Content-type')
            seen['timeout'] = timeout
            return _Response()

        with mock.patch.object(notify.urllib.request, 'urlopen', fake):
            self.assertEqual(notify.post(self.URL, 'hi', 5), (True, ''))
        self.assertEqual(seen['url'], self.URL)
        self.assertEqual(seen['body'], b'{"text": "hi"}')
        self.assertEqual(seen['type'], 'application/json')
        self.assertEqual(seen['timeout'], 5)

    def test_http_error_is_reported_not_raised(self):
        error = urllib.error.HTTPError(self.URL, 404, 'gone', {}, None)
        with mock.patch.object(notify.urllib.request, 'urlopen',
                               side_effect=error):
            ok, message = notify.post(self.URL, 'hi', 5)
        self.assertFalse(ok)
        self.assertIn('404', message)

    def test_non_2xx_is_a_failure(self):
        with mock.patch.object(notify.urllib.request, 'urlopen',
                               return_value=_Response(500)):
            ok, message = notify.post(self.URL, 'hi', 5)
        self.assertFalse(ok)
        self.assertIn('500', message)

    def test_transport_failure_never_escapes(self):
        """A webhook outage must not propagate into the daemon's poll loop."""
        for error in (urllib.error.URLError('unreachable'),
                      TimeoutError('slow'), OSError('boom')):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(notify.urllib.request, 'urlopen',
                                       side_effect=error):
                    ok, message = notify.post(self.URL, 'hi', 1)
                self.assertFalse(ok)
                self.assertTrue(message)


class NotifyConfigTests(unittest.TestCase):
    def _load(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'config.toml')
            with open(path, 'w') as handle:
                handle.write(body)
            with mock.patch.object(config, 'CONFIG_PATH', path):
                return config.load_notify()

    def test_desktop_works_without_any_webhook(self):
        """The desktop route needs no endpoint, so an unset URL must not
        silence it -- only the webhook route depends on webhook_url."""
        cfg = self._load('[client]\ngateways = ["a"]\n')
        self.assertTrue(cfg['enabled'])
        self.assertTrue(cfg['desktop'])
        self.assertEqual(cfg['webhook_url'], '')

    def test_a_url_configures_the_webhook_route(self):
        cfg = self._load(
            '[notify]\nwebhook_url = "https://hooks.slack.com/x"\n')
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['webhook_url'], 'https://hooks.slack.com/x')
        self.assertEqual(cfg['lead_time'], 3600)

    def test_master_switch_silences_every_route(self):
        cfg = self._load(
            '[notify]\nenabled = false\nwebhook_url = "https://x.test/h"\n')
        self.assertFalse(cfg['enabled'])

    def test_desktop_can_be_turned_off_on_its_own(self):
        cfg = self._load('[notify]\ndesktop = false\n')
        self.assertTrue(cfg['enabled'])
        self.assertFalse(cfg['desktop'])

    def test_non_boolean_flags_are_ignored(self):
        cfg = self._load('[notify]\nenabled = "yes"\ndesktop = 1\n')
        self.assertTrue(cfg['enabled'])
        self.assertTrue(cfg['desktop'])

    def test_non_http_urls_are_rejected(self):
        """The daemon POSTs this unexamined; a file:/ftp: typo must not turn
        into an unexpected local read."""
        # TOML basic strings, so "\n" below is a real newline once parsed.
        for literal in ('"file:///etc/passwd"', '"ftp://x.test/h"',
                        '"x.test/h"', '"javascript:alert(1)"',
                        '"https://x.test/\\nInjected"',
                        '"https://x.test/\\u001b[2J"'):
            with self.subTest(literal=literal):
                cfg = self._load(f'[notify]\nwebhook_url = {literal}\n')
                self.assertEqual(cfg['webhook_url'], '')

    def test_out_of_range_numbers_fall_back_to_defaults(self):
        cfg = self._load('[notify]\nwebhook_url = "https://x.test/h"\n'
                         'lead_time = -1\ntimeout = 9999\n')
        self.assertEqual(cfg['lead_time'], 3600)
        self.assertEqual(cfg['timeout'], 10)

    def test_a_broken_config_file_never_raises(self):
        cfg = self._load('[notify\nnot toml at all')
        self.assertEqual(cfg['webhook_url'], '')
        self.assertEqual(cfg['lead_time'], 3600)


class LocalNotifyTests(unittest.TestCase):
    """Desktop popup on whichever machine runs the TUI."""

    def _argv(self, platform, title='AutoTmux', text='ends in 45m'):
        with mock.patch.object(notify.sys, 'platform', platform):
            return notify.local_notify_argv(title, text)

    def test_macos_uses_osascript(self):
        argv = self._argv('darwin')
        self.assertEqual(argv[:2], ['osascript', '-e'])
        self.assertIn('display notification "ends in 45m"', argv[2])
        self.assertIn('with title "AutoTmux"', argv[2])

    def test_linux_uses_notify_send(self):
        self.assertEqual(self._argv('linux'),
                         ['notify-send', 'AutoTmux', 'ends in 45m'])

    def test_unsupported_platform_yields_nothing(self):
        self.assertIsNone(self._argv('win32'))

    def test_applescript_quoting_cannot_break_out_of_the_string(self):
        """A job name is untrusted text; it must stay data, not become code."""
        argv = self._argv('darwin', text='a" & do shell script "touch /tmp/x')
        script = argv[2]
        self.assertNotIn('" & do shell script "', script)
        self.assertIn('\\"', script)
        # Exactly one unescaped quote pair opens and closes each literal.
        self.assertEqual(script.count('"') - script.count('\\"'), 4)

    def test_newlines_are_folded_out(self):
        argv = self._argv('linux', text='line one\nline two')
        self.assertEqual(argv[2], 'line one line two')

    def test_empty_text_is_not_announced(self):
        self.assertIsNone(self._argv('darwin', text='   '))

    def test_a_missing_backend_is_not_fatal(self):
        with mock.patch.object(notify.subprocess, 'run',
                               side_effect=FileNotFoundError()):
            self.assertFalse(notify.local_notify('t', 'x'))

    def test_a_hung_backend_is_bounded(self):
        with mock.patch.object(
                notify.subprocess, 'run',
                side_effect=notify.subprocess.TimeoutExpired('osascript', 5)):
            self.assertFalse(notify.local_notify('t', 'x'))


class JobsFromStateTests(unittest.TestCase):
    """The gateway client already receives what a reminder needs, so it can
    warn locally without running squeue itself."""

    def test_jobs_are_collected_once_per_id(self):
        state = {'nodes': {
            'gpu1': {'info': {'job_id': '7', 'job_name': 'train',
                              'state': 'RUNNING', 'time': '0:30:00'}},
            'gpu2': {'info': {'job_id': '7', 'job_name': 'train',
                              'state': 'RUNNING', 'time': '0:30:00'}},
            'gpu3': {'info': {'job_id': '8', 'job_name': 'other',
                              'state': 'RUNNING', 'time': '9:00:00'}},
        }}
        jobs = notify.jobs_from_state(state)
        self.assertEqual(sorted(j['job_id'] for j in jobs), ['7', '8'])
        self.assertEqual(next(j for j in jobs if j['job_id'] == '7')['node'],
                         'gpu1')

    def test_placeholder_and_local_rows_are_skipped(self):
        state = {'nodes': {
            'localhost': {'info': {}},
            'login--x': {'info': {'job_id': '-'}},
            'broken': {'info': 'not a dict'},
            'gpu1': {'info': {'job_id': '9', 'time': '0:10:00',
                              'state': 'RUNNING'}},
        }}
        self.assertEqual([j['job_id'] for j in notify.jobs_from_state(state)],
                         ['9'])

    def test_malformed_state_yields_nothing(self):
        for state in ({}, {'nodes': None}, {'nodes': []}, 'nope', None):
            with self.subTest(state=state):
                self.assertEqual(notify.jobs_from_state(state), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
