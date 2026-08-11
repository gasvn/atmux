"""Tests for the job-expiry and idle-session reminder webhooks."""
import os
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import config, daemon, notify


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


def _node(job_id='42', sessions=None, **extra):
    return {'job_id': job_id, 'job_name': 'train',
            'sessions': sessions or [], **extra}


class IdleSessionTests(unittest.TestCase):
    """A pane that stops changing is the observable end of a run: the work
    finished, or it wedged. That is the thing worth pushing to the user."""

    def test_a_quiet_session_is_reported(self):
        found = notify.idle_sessions(
            'gpu1', _node(sessions=[['train', '1', 900]]), 300)
        self.assertEqual([(e['session'], e['idle']) for e in found],
                         [('train', 900)])

    def test_a_busy_session_is_not(self):
        self.assertEqual(
            notify.idle_sessions('gpu1', _node(sessions=[['train', '1', 30]]),
                                 300), [])

    def test_the_threshold_is_inclusive(self):
        self.assertEqual(
            len(notify.idle_sessions(
                'gpu1', _node(sessions=[['t', '1', 300]]), 300)), 1)

    def test_zero_disables_the_check(self):
        self.assertEqual(
            notify.idle_sessions('gpu1', _node(sessions=[['t', '1', 9999]]),
                                 0), [])

    def test_login_nodes_and_the_laptop_are_skipped(self):
        """A shell left open on a login node is idle by design; announcing it
        would be pure noise."""
        for job_id in ('-', '', None):
            with self.subTest(job_id=job_id):
                self.assertEqual(
                    notify.idle_sessions(
                        'localhost', _node(job_id=job_id,
                                           sessions=[['t', '1', 9999]]), 300),
                    [])

    def test_entries_without_idle_data_are_skipped(self):
        """An older login-node daemon reports two fields, not three."""
        self.assertEqual(
            notify.idle_sessions('gpu1', _node(sessions=[['t', '1']]), 300),
            [])

    def test_malformed_entries_do_not_raise(self):
        node = _node(sessions=[
            'nope', None, [], ['t'], ['t', '1', 'x'], ['t', '1', float('nan')],
            ['t', '1', True], ['good', '1', 900]])
        self.assertEqual(
            [e['session'] for e in notify.idle_sessions('gpu1', node, 300)],
            ['good'])

    def test_the_message_says_what_stopped_and_for_how_long(self):
        entry = {'node': 'gpu1', 'session': 'train', 'idle': 900,
                 'job_name': 'sweep'}
        text = notify.build_idle_message(entry)
        self.assertIn('train', text)
        self.assertIn('gpu1', text)
        self.assertIn('15m', text)
        self.assertIn('sweep', text)
        self.assertIn('finished or stalled', text)

    def test_the_message_survives_missing_fields(self):
        self.assertTrue(notify.build_idle_message({}))

    def test_the_message_quotes_the_line_it_stopped_on(self):
        entry = {'node': 'gpu1', 'session': 'train', 'idle': 900,
                 'tail': 'CUDA out of memory'}
        self.assertIn('Last line: CUDA out of memory',
                      notify.build_idle_message(entry))

    def test_no_tail_means_the_message_it_always_was(self):
        for tail in ('', '   ', None):
            entry = {'node': 'gpu1', 'session': 'train', 'idle': 900,
                     'tail': tail}
            self.assertNotIn('Last line', notify.build_idle_message(entry))


class JobStartTests(unittest.TestCase):
    """A job holds no node until it starts, so appearing in the allocated set
    is the transition worth announcing."""

    def test_a_newly_running_job_is_reported(self):
        jobs = [{'job_id': '7', 'job_name': 'train', 'state': 'RUNNING',
                 'node': 'gpu1'}]
        fresh = notify.started_jobs(jobs, set())
        self.assertEqual([j['job_id'] for j in fresh], ['7'])

    def test_a_job_already_seen_is_not_reported_again(self):
        jobs = [{'job_id': '7', 'state': 'RUNNING'}]
        self.assertEqual(notify.started_jobs(jobs, {'7'}), [])

    def test_a_job_that_is_not_running_is_ignored(self):
        for state in ('PENDING', 'COMPLETING', 'SUSPENDED'):
            jobs = [{'job_id': '7', 'state': state}]
            self.assertEqual(notify.started_jobs(jobs, set()), [], state)

    def test_malformed_entries_do_not_raise(self):
        jobs = ['nope', None, {}, {'job_id': ''}, {'job_id': '7'}]
        self.assertEqual([j['job_id'] for j in notify.started_jobs(jobs, set())],
                         ['7'])

    def test_the_message_names_the_job_and_where_it_landed(self):
        text = notify.build_start_message(
            {'job_id': '7', 'job_name': 'train', 'node': 'gpu1'})
        self.assertIn('train', text)
        self.assertIn('7', text)
        self.assertIn('gpu1', text)
        self.assertIn('now running', text)

    def test_the_message_survives_missing_fields(self):
        self.assertTrue(notify.build_start_message({}))


class LastOutputLineTests(unittest.TestCase):
    """Whether a run finished or died is in the line it stopped on, and that
    line is raw terminal bytes on its way into a chat message."""

    def test_takes_the_last_line_with_words_in_it(self):
        pane = 'Epoch 39/40\nEpoch 40/40 done\n\n   \n'
        self.assertEqual(notify.last_output_line(pane), 'Epoch 40/40 done')

    def test_strips_colour_and_cursor_sequences(self):
        pane = '\x1b[1;32mtrain\x1b[0m: \x1b[Kloss 0.31\x1b[?25h'
        self.assertEqual(notify.last_output_line(pane), 'train: loss 0.31')

    def test_strips_title_sequences_tmux_emits(self):
        for terminator in ('\x07', '\x1b\\'):
            pane = f'\x1b]0;bash{terminator}real output'
            self.assertEqual(notify.last_output_line(pane), 'real output')

    def test_control_characters_never_reach_the_message(self):
        # A chat client would render these as mojibake.
        self.assertEqual(notify.last_output_line('a\x00b\x08c\x7fd'), 'abcd')

    def test_a_progress_bar_reports_its_final_state(self):
        # A bar redraws by returning to column 0, so the useful number is the
        # last thing written rather than the first. splitlines() treats the
        # carriage return as a break, which lands on exactly that.
        pane = 'training\n 10%|## \r 50%|##### \r100%|##########| 40/40'
        self.assertEqual(notify.last_output_line(pane),
                         '100%|##########| 40/40')

    def test_whitespace_is_collapsed(self):
        self.assertEqual(notify.last_output_line('a\t\t b     c'), 'a b c')

    def test_long_lines_are_capped(self):
        got = notify.last_output_line('x' * 500)
        self.assertEqual(len(got), notify.TAIL_LIMIT)
        self.assertTrue(got.endswith('…'))

    def test_a_silly_limit_cannot_produce_a_negative_slice(self):
        for limit in (0, -5, 1):
            self.assertLessEqual(len(notify.last_output_line('y' * 50, limit)),
                                 8)

    def test_borders_spinners_and_bare_prompts_are_stepped_over(self):
        # These are the last thing on screen often enough to matter, and none
        # of them say anything about the run.
        pane = 'Saved checkpoint 40\n────────────\n❯\n  ⠋  \n│  │\n'
        self.assertEqual(notify.last_output_line(pane), 'Saved checkpoint 40')

    def test_a_bordered_line_with_words_still_counts(self):
        self.assertEqual(notify.last_output_line('── extend ──'), '── extend ──')

    # The bottom of a real Claude Code pane, which is what every one of this
    # user's sessions runs. Before the chrome rule, all of them reported
    # "auto mode on" -- the one thing that is true whatever the session did.
    TUI_PANE = '\n'.join([
        'the search space is covered; nothing else fits.',
        '',
        '✻ Crunched for 3m 16s · 1 monitor still running',
        '',
        '─────────────────────────────────────────── extend ──',
        '❯',
        '──────────────────────────────────────────────────────',
        '[Opus 5 | Max] │ harness git:(main*) │ extend',
        'Context ████████░░ 83% │ Usage ████████░░ 78%',
        '10 hooks',
        '✓ Bash ×17 | ✓ Monitor ×3',
        '⏵⏵ auto mode on · 1 monitor · ← for agents',
    ])

    def test_a_status_bar_block_is_not_the_answer(self):
        self.assertEqual(notify.last_output_line(self.TUI_PANE),
                         '✻ Crunched for 3m 16s · 1 monitor still running')

    def test_a_rule_with_a_word_centred_in_it_is_still_a_rule(self):
        self.assertTrue(notify._looks_like_rule(
            '─────────────────────────────────────────── extend ──'))
        self.assertFalse(notify._looks_like_rule('Epoch 40/40 done'))

    def test_output_below_a_rule_is_kept_when_it_reads_like_output(self):
        """A table's rows sit below its header rule and are the real answer;
        discarding them would be worse than quoting a status bar."""
        pane = 'NAME VALUE\n──────────────────\nalpha 41\nbeta 42'
        self.assertEqual(notify.last_output_line(pane), 'beta 42')

    def test_a_single_line_below_a_rule_is_never_treated_as_chrome(self):
        pane = 'building\n──────────────────\nbuild succeeded'
        self.assertEqual(notify.last_output_line(pane), 'build succeeded')

    def test_a_pane_with_no_rule_is_unaffected(self):
        pane = 'Epoch 39/40\nEpoch 40/40 done'
        self.assertEqual(notify.last_output_line(pane), 'Epoch 40/40 done')

    def test_a_pane_that_is_only_chrome_yields_nothing(self):
        pane = '──────────\n│ x │\n⏵⏵ auto mode on\n✓ Bash ×2'
        self.assertEqual(notify.last_output_line(pane), '')

    def test_a_pane_with_nothing_in_it_yields_nothing(self):
        for pane in ('', '   \n\n\t\n', '\x1b[0m\x1b[0m', None, 42, b'bytes'):
            self.assertEqual(notify.last_output_line(pane), '')

    def test_only_the_tail_of_a_huge_pane_is_scanned(self):
        # A capture is a screenful, but nothing stops it being far bigger, and
        # walking all of it on the delivery thread is wasted work. 200 lines is
        # several screens past where the answer can be.
        pane = '\n'.join(['old'] * 5000 + ['   '] * 201)
        self.assertEqual(notify.last_output_line(pane), '')
        pane = '\n'.join(['old'] * 5000 + ['   '] * 150)
        self.assertEqual(notify.last_output_line(pane), 'old')


class WebAttachUrlTests(unittest.TestCase):
    """The link a phone can actually follow.

    Nothing on a phone resolves atmux://, so a notice read where notices are
    actually read -- away from the machine with the handler installed -- had a
    dead link on it and nothing else.
    """

    BASE = 'https://host.tailnet.ts.net/term/'

    def test_it_opens_the_session_in_the_browser_client(self):
        self.assertEqual(
            notify.web_attach_url(self.BASE, 'gpu1', 'train'),
            'https://host.tailnet.ts.net/term/console/?attach=gpu1%3Atrain')

    def test_a_missing_trailing_slash_is_not_the_users_problem(self):
        self.assertEqual(
            notify.web_attach_url('https://h/term', 'gpu1', 'train'),
            notify.web_attach_url('https://h/term/', 'gpu1', 'train'))

    def test_the_target_is_a_query_value_not_a_path(self):
        """A session named ../.. must not be able to climb out of /console/.
        It is refused outright, and the colon is encoded either way."""
        self.assertEqual(notify.web_attach_url(self.BASE, 'gpu1', '../../etc'), '')
        self.assertIn('%3A', notify.web_attach_url(self.BASE, 'gpu1', 'train'))
        self.assertNotIn('..', notify.web_attach_url(self.BASE, 'gpu1', 'train'))

    def test_only_http_bases_are_accepted(self):
        """Same rule as the webhook: a file:/ftp: base would turn a config
        typo into a link that does something else entirely."""
        for base in ('ftp://h/', 'file:///etc/', 'javascript:x', '', 'h/term'):
            with self.subTest(base=base):
                self.assertEqual(
                    notify.web_attach_url(base, 'gpu1', 'train'), '')

    def test_a_name_it_cannot_express_gets_no_link(self):
        for node, session in (('bad host', 's'), ('gpu1', 'a/b'),
                              ('gpu1', ''), ('', 's')):
            with self.subTest(node=node, session=session):
                self.assertEqual(
                    notify.web_attach_url(self.BASE, node, session), '')

    def test_the_notice_offers_both_and_neither_by_default(self):
        """Which device is reading cannot be told from here -- nor reliably
        from a User-Agent, because iPadOS reports itself as MacIntel -- so
        both are shown and the reader, who does know, picks."""
        entry = {'session': 'train', 'node': 'gpu1', 'idle': 900}
        plain = notify.build_idle_message(entry)
        self.assertNotIn('|Attach>', plain)
        self.assertNotIn('|Browser>', plain)
        both = notify.build_idle_message(entry, link=True, web=self.BASE)
        self.assertIn('|Attach>', both)
        self.assertIn('|Browser>', both)
        # The browser link stands on its own: someone who never installed the
        # handler still gets the link that works.
        web_only = notify.build_idle_message(entry, web=self.BASE)
        self.assertNotIn('|Attach>', web_only)
        self.assertIn('|Browser>', web_only)


class AttachUrlTests(unittest.TestCase):
    """The link arrives from a chat message, so anyone who can post to the
    channel can craft one. It is validated here and dispatched as argv."""

    def test_round_trip(self):
        url = notify.attach_url('holygpu8a11104', 'newclaw')
        self.assertEqual(url, 'atmux://attach/holygpu8a11104/newclaw')
        self.assertEqual(notify.parse_attach_url(url),
                         ('holygpu8a11104', 'newclaw'))

    def test_a_space_in_a_session_name_survives(self):
        url = notify.attach_url('gpu1', 'my session')
        self.assertEqual(notify.parse_attach_url(url), ('gpu1', 'my session'))

    def test_names_that_cannot_be_expressed_safely_yield_no_link(self):
        for node, session in (('gpu1', 'a/b'), ('gpu1', ';rm -rf /'),
                              ('gpu1', '$(id)'), ('-oProxy=x', 's'),
                              ('gpu1', '`id`'), ('gpu1', 'a\nb'),
                              ('gpu1', ''), ('', 's')):
            with self.subTest(node=node, session=session):
                self.assertEqual(notify.attach_url(node, session), '')

    def test_hostile_urls_are_refused(self):
        for url in (
                'atmux://attach/gpu1/%3Brm%20-rf%20%2F',
                'atmux://attach/gpu1/%24%28id%29',
                'atmux://attach/../../etc/passwd',
                'atmux://attach/-oProxyCommand%3Dx/s',
                'atmux://attach/gpu1/a/b',
                'atmux://attach/gpu1',
                'atmux://attach/',
                'atmux://other/gpu1/s',
                'http://evil.test/attach/gpu1/s',
                'file:///etc/passwd',
                '', None, 42,
                'atmux://attach/gpu1/%0Aid'):
            with self.subTest(url=url):
                self.assertIsNone(notify.parse_attach_url(url))

    def test_a_query_string_is_ignored_not_smuggled(self):
        self.assertEqual(
            notify.parse_attach_url('atmux://attach/gpu1/sess?x=1'),
            ('gpu1', 'sess'))

    def test_the_link_is_opt_in(self):
        entry = {'node': 'gpu1', 'session': 'train', 'idle': 900}
        self.assertNotIn('atmux://', notify.build_idle_message(entry))
        self.assertIn('<atmux://attach/gpu1/train|Attach>',
                      notify.build_idle_message(entry, link=True))

    def test_an_unlinkable_session_degrades_to_plain_text(self):
        entry = {'node': 'gpu1', 'session': 'a/b', 'idle': 900}
        text = notify.build_idle_message(entry, link=True)
        self.assertNotIn('atmux://', text)
        self.assertIn('a/b', text)


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


class IdleAnnouncementTests(unittest.TestCase):
    """Announced once per quiet spell, re-armed when the session moves again."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sent = []
        self._saved_cfg = daemon._notify_cfg
        self._saved_seen = daemon._idle_announced
        daemon._idle_announced = {}
        daemon._notify_cfg = dict(config.NOTIFY_DEFAULTS)
        daemon._notify_cfg.update(
            enabled=True, webhook_url='https://x.test/h', idle_notify=300)
        self.patchers = [
            mock.patch.object(config, 'NOTIFY_CLAIM_DIR',
                              os.path.join(self.temp.name, 'claims')),
            mock.patch.object(
                daemon.notify, 'post',
                side_effect=lambda u, t, to: (self.sent.append(t), (True, ''))[1]),
            # Deliver inline so the assertions do not race a worker thread.
            mock.patch.object(
                daemon.threading, 'Thread',
                side_effect=lambda target, args, **kw: SimpleNamespace(
                    start=lambda: target(*args))),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        daemon._notify_cfg = self._saved_cfg
        daemon._idle_announced = self._saved_seen
        self.temp.cleanup()

    def _poll(self, idle, session='train'):
        daemon._notify_idle_sessions('gpu1', {
            'job_id': '42', 'job_name': 'sweep',
            'sessions': [[session, '1', idle]]})

    def test_one_message_per_quiet_spell(self):
        self._poll(900)
        self._poll(960)
        self._poll(1200)
        self.assertEqual(len(self.sent), 1)

    def test_activity_re_arms_the_session(self):
        self._poll(900)
        self._poll(5)              # produced output again
        self.assertEqual(daemon._idle_announced.get('gpu1'), set())

    def test_a_second_spell_is_held_off_by_the_cooldown(self):
        """Across four login-node daemons the shared claim is what stops the
        same session being announced four times."""
        self._poll(900)
        self._poll(5)
        self._poll(900)
        self.assertEqual(len(self.sent), 1)

    def test_the_notice_quotes_the_line_the_session_stopped_on(self):
        with mock.patch.object(daemon, '_capture_pane',
                               return_value='setup\n\x1b[31mCUDA OOM\x1b[0m\n'):
            self._poll(900)
        self.assertIn('Last line: CUDA OOM', self.sent[0])

    def test_idle_tail_can_be_turned_off(self):
        """One line of terminal output leaving the cluster is its own decision,
        separate from wanting the notice at all."""
        daemon._notify_cfg['idle_tail'] = False
        with mock.patch.object(daemon, '_capture_pane') as capture:
            self._poll(900)
        capture.assert_not_called()
        self.assertIn('finished or stalled', self.sent[0])
        self.assertNotIn('Last line', self.sent[0])

    def test_an_unavailable_capture_still_sends_the_notice(self):
        """The quiet session is the news; the quoted line is a bonus, and a
        node too busy to answer a capture is exactly when it matters most."""
        for index, outcome in enumerate(({'return_value': None},
                                         {'side_effect': OSError('no route')})):
            with self.subTest(outcome=outcome):
                self.sent.clear()
                daemon._idle_announced.clear()
                # A fresh claim file per case: the shared claim is what stops a
                # second announcement, and it would mask the second case.
                with mock.patch.object(config, 'NOTIFY_CLAIM_DIR',
                                       os.path.join(self.temp.name,
                                                    f'claims{index}')):
                    with mock.patch.object(daemon, '_capture_pane', **outcome):
                        self._poll(900)
                self.assertEqual(len(self.sent), 1)
                self.assertNotIn('Last line', self.sent[0])

    def test_the_first_complete_poll_is_seeded_not_announced(self):
        """A restart would otherwise announce every job already running."""
        daemon._started_jobs.clear()
        daemon._started_seeded = False
        running = {'gpu1': {'job_id': '7', 'job_name': 'train',
                            'state': 'RUNNING'}}
        daemon._notify_started_jobs(running)
        self.assertEqual(self.sent, [])
        self.assertIn('7', daemon._started_jobs)

    def test_a_job_that_starts_later_is_announced(self):
        daemon._started_jobs.clear()
        daemon._started_seeded = False
        daemon._notify_started_jobs({'gpu1': {'job_id': '7', 'state': 'RUNNING'}})
        daemon._notify_started_jobs({
            'gpu1': {'job_id': '7', 'state': 'RUNNING'},
            'gpu2': {'job_id': '8', 'job_name': 'sweep', 'state': 'RUNNING'}})
        self.assertEqual(len(self.sent), 1)
        self.assertIn('sweep', self.sent[0])
        self.assertIn('now running', self.sent[0])

    def test_a_job_is_announced_once(self):
        daemon._started_jobs.clear()
        daemon._started_seeded = False
        daemon._notify_started_jobs({})
        for _ in range(3):
            daemon._notify_started_jobs(
                {'gpu1': {'job_id': '9', 'state': 'RUNNING'}})
        self.assertEqual(len(self.sent), 1)

    def test_job_start_can_be_turned_off(self):
        daemon._notify_cfg['job_start'] = False
        daemon._started_jobs.clear()
        daemon._started_seeded = False
        daemon._notify_started_jobs({})
        daemon._notify_started_jobs({'gpu1': {'job_id': '7', 'state': 'RUNNING'}})
        self.assertEqual(self.sent, [])

    def test_a_job_leaving_the_queue_does_not_grow_the_set(self):
        daemon._started_jobs.clear()
        daemon._started_seeded = False
        daemon._notify_started_jobs({'gpu1': {'job_id': '7', 'state': 'RUNNING'}})
        daemon._notify_started_jobs({})
        self.assertEqual(daemon._started_jobs, set())

    def test_nothing_is_sent_without_a_webhook(self):
        daemon._notify_cfg['webhook_url'] = ''
        self._poll(900)
        self.assertEqual(self.sent, [])

    def test_zero_threshold_disables_it(self):
        daemon._notify_cfg['idle_notify'] = 0
        self._poll(9999)
        self.assertEqual(self.sent, [])

    def test_a_failed_post_is_not_recorded_as_delivered(self):
        with mock.patch.object(daemon.notify, 'post',
                               return_value=(False, 'boom')):
            self._poll(900)
        self.assertEqual(self.sent, [])

    def test_a_failed_post_is_retried_rather_than_lost(self):
        """The claim has to be taken before posting, but keeping it after a
        failure would silence the notice for a whole cooldown."""
        with mock.patch.object(daemon.notify, 'post',
                               return_value=(False, 'webhook down')):
            self._poll(900)
        self.assertEqual(self.sent, [])
        self._poll(960)                       # webhook back up
        self.assertEqual(len(self.sent), 1)

    def test_distinct_sessions_are_announced_separately(self):
        daemon._notify_idle_sessions('gpu1', {
            'job_id': '42', 'job_name': 'sweep',
            'sessions': [['a', '1', 900], ['b', '1', 900]]})
        self.assertEqual(len(self.sent), 2)


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
