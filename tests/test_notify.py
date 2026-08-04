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

    def test_absent_section_leaves_the_feature_off(self):
        cfg = self._load('[client]\ngateways = ["a"]\n')
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['webhook_url'], '')

    def test_a_url_enables_it(self):
        cfg = self._load(
            '[notify]\nwebhook_url = "https://hooks.slack.com/x"\n')
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['webhook_url'], 'https://hooks.slack.com/x')
        self.assertEqual(cfg['lead_time'], 3600)

    def test_enabled_without_a_url_stays_off(self):
        """Nothing to post to, so honouring `enabled` would only log errors."""
        cfg = self._load('[notify]\nenabled = true\n')
        self.assertFalse(cfg['enabled'])

    def test_explicitly_disabled_stays_off_even_with_a_url(self):
        cfg = self._load(
            '[notify]\nenabled = false\nwebhook_url = "https://x.test/h"\n')
        self.assertFalse(cfg['enabled'])

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
                self.assertFalse(cfg['enabled'])

    def test_out_of_range_numbers_fall_back_to_defaults(self):
        cfg = self._load('[notify]\nwebhook_url = "https://x.test/h"\n'
                         'lead_time = -1\ntimeout = 9999\n')
        self.assertEqual(cfg['lead_time'], 3600)
        self.assertEqual(cfg['timeout'], 10)

    def test_a_broken_config_file_never_raises(self):
        cfg = self._load('[notify\nnot toml at all')
        self.assertFalse(cfg['enabled'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
