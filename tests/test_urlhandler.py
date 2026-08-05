"""The atmux:// handler: session tagging, and the applet source we generate.

The quoting in the generated AppleScript is the part that has actually broken in
the field, and it is the part that carries untrusted input, so it is checked
here rather than only by installing and clicking.
"""

import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from autotmux import cli, notify, urlhandler


class FakeTTY(io.StringIO):
    """A stream that claims to be a terminal, so escapes are emitted."""

    def __init__(self, tty=True):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


ITERM_ENV = {'TERM_PROGRAM': 'iTerm.app'}


def decode_user_var(sequence: str) -> str:
    """The value carried by an ``OSC 1337 SetUserVar`` escape."""
    payload = sequence.rstrip('\a').split('=', 2)[2]
    return base64.b64decode(payload).decode('utf-8')


class SetUserVarTests(unittest.TestCase):
    def test_sequence_shape(self):
        seq = urlhandler.set_user_var_sequence('atmuxTarget', 'hello')
        payload = base64.b64encode(b'hello').decode()
        self.assertEqual(seq, f'\033]1337;SetUserVar=atmuxTarget={payload}\a')

    def test_clearing_uses_empty_payload(self):
        self.assertEqual(
            urlhandler.set_user_var_sequence('atmuxTarget', ''),
            '\033]1337;SetUserVar=atmuxTarget=\a')

    def test_non_ascii_value_survives(self):
        seq = urlhandler.set_user_var_sequence('v', 'sé')
        encoded = seq.split('=', 2)[2].rstrip('\a')
        self.assertEqual(base64.b64decode(encoded).decode('utf-8'), 'sé')


class AttachTagTests(unittest.TestCase):
    def test_tag_is_the_link_a_reminder_would_send(self):
        # Both sides have to agree character for character or reuse never
        # matches, so this equality is the contract.
        self.assertEqual(urlhandler.attach_tag('holygpu1', 'train'),
                         notify.attach_url('holygpu1', 'train'))

    def test_pseudo_sessions_are_not_taggable(self):
        # The dashboard's placeholder rows are not real sessions; their names
        # start with NUL and must never reach a URL.
        self.assertEqual(urlhandler.attach_tag('n1', cli._START_SHELL_SESSION), '')
        self.assertEqual(urlhandler.attach_tag('n1', cli._OFFLINE_SESSION), '')

    def test_hostile_names_are_refused(self):
        for node, session in (('n1', 'a;rm -rf /'), ('n1$(id)', 'ok'),
                              ('../etc', 'ok'), ('n1', '')):
            self.assertEqual(urlhandler.attach_tag(node, session), '',
                             f'{node}:{session} should not be taggable')


class MarkAttachedTests(unittest.TestCase):
    def test_marks_on_iterm(self):
        stream = FakeTTY()
        self.assertTrue(
            urlhandler.mark_attached('holygpu1', 'train', stream, ITERM_ENV))
        tag = notify.attach_url('holygpu1', 'train')
        payload = base64.b64encode(tag.encode()).decode()
        self.assertIn(payload, stream.getvalue())

    def test_lc_terminal_also_counts(self):
        stream = FakeTTY()
        self.assertTrue(urlhandler.mark_attached(
            'n1', 'train', stream, {'LC_TERMINAL': 'iTerm2'}))

    def test_silent_on_other_terminals(self):
        stream = FakeTTY()
        self.assertFalse(urlhandler.mark_attached(
            'n1', 'train', stream, {'TERM_PROGRAM': 'Apple_Terminal'}))
        self.assertEqual(stream.getvalue(), '')

    def test_silent_when_not_a_tty(self):
        # Redirected output must not collect escape bytes.
        stream = FakeTTY(tty=False)
        self.assertFalse(
            urlhandler.mark_attached('n1', 'train', stream, ITERM_ENV))
        self.assertEqual(stream.getvalue(), '')

    def test_silent_inside_tmux(self):
        # tmux swallows the escape unless allow-passthrough is on, which it is
        # not by default; emitting anyway would risk stray bytes on screen.
        stream = FakeTTY()
        env = dict(ITERM_ENV, TMUX='/tmp/tmux-501/default,123,0')
        self.assertFalse(urlhandler.mark_attached('n1', 'train', stream, env))
        self.assertEqual(stream.getvalue(), '')

    def test_silent_for_untaggable_session(self):
        stream = FakeTTY()
        self.assertFalse(urlhandler.mark_attached(
            'n1', cli._START_SHELL_SESSION, stream, ITERM_ENV))
        self.assertEqual(stream.getvalue(), '')

    def test_write_failure_is_swallowed(self):
        class Broken(FakeTTY):
            def write(self, _data):
                raise OSError('gone')

        self.assertFalse(
            urlhandler.mark_attached('n1', 'train', Broken(), ITERM_ENV))

    def test_clear_emits_empty_value(self):
        stream = FakeTTY()
        self.assertTrue(urlhandler.clear_attached(stream, ITERM_ENV))
        self.assertEqual(stream.getvalue(),
                         '\033]1337;SetUserVar=atmuxTarget=\a')


class AttachedContextTests(unittest.TestCase):
    def test_marks_then_clears(self):
        stream = FakeTTY()
        with urlhandler.attached('n1', 'train', stream, ITERM_ENV):
            mid = stream.getvalue()
        self.assertEqual(decode_user_var(mid), notify.attach_url('n1', 'train'))
        self.assertTrue(stream.getvalue().endswith(
            '\033]1337;SetUserVar=atmuxTarget=\a'))

    def test_clears_even_when_the_attach_raises(self):
        stream = FakeTTY()
        with self.assertRaises(RuntimeError):
            with urlhandler.attached('n1', 'train', stream, ITERM_ENV):
                raise RuntimeError('ssh died')
        self.assertTrue(stream.getvalue().endswith(
            '\033]1337;SetUserVar=atmuxTarget=\a'))

    def test_no_clear_when_nothing_was_marked(self):
        # Otherwise a plain Terminal.app window would get a stray escape on the
        # way out that it never got on the way in.
        stream = FakeTTY()
        with urlhandler.attached('n1', 'train', stream,
                                 {'TERM_PROGRAM': 'Apple_Terminal'}):
            pass
        self.assertEqual(stream.getvalue(), '')


class AppleScriptTests(unittest.TestCase):
    def test_embeds_the_atmux_path_as_a_literal(self):
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        self.assertIn('quoted form of "/opt/bin/atmux"', source)

    def test_path_with_quotes_cannot_break_out(self):
        # A path is not attacker-controlled, but it is user-controlled, and an
        # unescaped quote here would produce a script that does not compile at
        # best and runs something else at worst.
        source = urlhandler.applescript('/tmp/a"b\\c/atmux', 'iTerm')
        self.assertIn(r'quoted form of "/tmp/a\"b\\c/atmux"', source)

    def test_url_is_never_baked_in(self):
        # The applet receives the URL at runtime; a generator that interpolated
        # one would mean re-installing per link.
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        self.assertNotIn('atmux://', source)
        self.assertIn('on open location this_URL', source)

    def test_url_reaches_the_shell_only_through_quoted_form(self):
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        self.assertIn('quoted form of this_URL', source)
        self.assertIn('& " -l -c " & quoted form of ("exec " & inner)', source)

    def test_runs_under_a_login_shell(self):
        # A window opened by the applet inherits the GUI session's environment,
        # whose PATH has no tmux -- a local attach died with status 127 before
        # drawing anything. The login shell is what makes a clicked link behave
        # like typing the command.
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm',
                                        shell='/bin/zsh')
        self.assertIn('"/bin/zsh" & " -l -c "', source)

    def test_shell_is_escaped_too(self):
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm',
                                        shell='/bin/we"ird')
        self.assertIn(r'"/bin/we\"ird" & " -l -c "', source)

    def test_every_open_is_logged(self):
        # There is no way to attach a debugger to a LaunchServices callback, so
        # the log is the only account of what a click did.
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        for marker in ('my logLine("open " & this_URL)',
                       'focused an existing window', 'opened a new window',
                       'open failed: ', 'reuse lookup failed: '):
            self.assertIn(marker, source)
        self.assertIn(urlhandler.LOG_PATH, source)

    def test_notification_failure_cannot_mask_the_error(self):
        # Notification consent is a separate permission from automation; if it
        # is withheld the log line still has to survive.
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        report = source.split('on reportFailure')[1].split('end reportFailure')[0]
        self.assertIn('try', report)
        self.assertIn('display notification', report)

    def test_iterm_reuses_by_user_variable_and_checks_liveness(self):
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        self.assertIn('variable named "user.atmuxTarget"', source)
        self.assertIn('my hasProcess(tty of s)', source)
        self.assertIn('ps -t ', source)

    def test_terminal_reuses_by_custom_title(self):
        source = urlhandler.applescript('/opt/bin/atmux', 'Terminal')
        self.assertIn('custom title of t', source)
        self.assertIn('processes of t) is not {}', source)
        self.assertNotIn('variable named', source)

    def test_both_terminals_refocus_after_the_link_owner_settles(self):
        for terminal in urlhandler.TERMINALS:
            source = urlhandler.applescript('/opt/bin/atmux', terminal)
            self.assertIn(f'delay {urlhandler.FOCUS_SETTLE_SECONDS}', source)
            self.assertIn(f'tell application "{terminal}" to activate', source)

    def test_reuse_failure_falls_back_to_opening(self):
        # Every reuse step sits inside a try, so a future iTerm that renames a
        # property degrades to the old behaviour instead of breaking the link.
        source = urlhandler.applescript('/opt/bin/atmux', 'iTerm')
        dispatch = source.split('on dispatch')[1].split('end dispatch')[0]
        self.assertIn('try', dispatch)
        self.assertIn('my openNew(', dispatch)

    def test_unknown_terminal_is_rejected(self):
        with self.assertRaises(ValueError):
            urlhandler.applescript('/opt/bin/atmux', 'Ghostty')

    def test_default_terminal_prefers_iterm_when_installed(self):
        self.assertEqual(urlhandler.default_terminal(lambda _p: True), 'iTerm')
        self.assertEqual(urlhandler.default_terminal(lambda _p: False),
                         'Terminal')


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('osacompile'),
                     'osacompile is macOS-only')
class AppleScriptCompilesTests(unittest.TestCase):
    """The generator is only useful if what it emits is valid AppleScript."""

    def test_every_terminal_compiles(self):
        for terminal in urlhandler.TERMINALS:
            with self.subTest(terminal=terminal):
                source = urlhandler.applescript('/opt/bin/atmux', terminal)
                with tempfile.TemporaryDirectory() as tmp:
                    script = os.path.join(tmp, 'h.applescript')
                    with open(script, 'w', encoding='utf-8') as handle:
                        handle.write(source)
                    result = subprocess.run(
                        ['osacompile', '-o', os.path.join(tmp, 'h.scpt'),
                         script],
                        capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stderr)


class LoginShellTests(unittest.TestCase):
    def test_uses_the_users_shell(self):
        self.assertEqual(
            urlhandler.login_shell({'SHELL': '/bin/zsh'}, lambda _p: True),
            '/bin/zsh')

    def test_falls_back_when_missing_relative_or_not_executable(self):
        for env, executable in (({}, lambda _p: True),
                                ({'SHELL': ''}, lambda _p: True),
                                ({'SHELL': 'zsh'}, lambda _p: True),
                                ({'SHELL': '/bin/zsh'}, lambda _p: False)):
            self.assertEqual(urlhandler.login_shell(env, executable),
                             urlhandler.FALLBACK_SHELL, f'{env} should fall back')

    def test_real_environment_yields_something_usable(self):
        shell = urlhandler.login_shell()
        self.assertTrue(shell.startswith('/'))
        self.assertTrue(os.access(shell, os.X_OK))


class HandlerPathTests(unittest.TestCase):
    def test_env_override_wins(self):
        self.assertEqual(
            cli._handler_atmux_path({'ATMUX_BIN': '/opt/bin/atmux'}, '/x/y'),
            '/opt/bin/atmux')

    def test_override_is_expanded(self):
        got = cli._handler_atmux_path({'ATMUX_BIN': '~/bin/atmux'}, '/x/y')
        self.assertEqual(got, os.path.expanduser('~/bin/atmux'))
        self.assertTrue(os.path.isabs(got))

    def test_blank_override_falls_back(self):
        got = cli._handler_atmux_path({'ATMUX_BIN': '   '}, __file__)
        self.assertEqual(got, os.path.realpath(__file__))


class PrintUrlHandlerCliTests(unittest.TestCase):
    def _run(self, argv):
        parser = cli._build_argparser()
        return parser.parse_args(argv)

    def test_flag_defaults_to_autodetect(self):
        self.assertEqual(self._run(['--print-url-handler']).print_url_handler,
                         '')

    def test_flag_accepts_a_terminal(self):
        self.assertEqual(
            self._run(['--print-url-handler', 'Terminal']).print_url_handler,
            'Terminal')

    def test_absent_flag_is_none(self):
        self.assertIsNone(self._run([]).print_url_handler)

    def test_unknown_terminal_is_rejected(self):
        with self.assertRaises(SystemExit):
            with mock.patch('sys.stderr', new=io.StringIO()):
                self._run(['--print-url-handler', 'Ghostty'])

    def test_main_prints_and_exits_before_touching_the_runtime(self):
        out = io.StringIO()
        with mock.patch.object(sys, 'argv',
                               ['atmux', '--print-url-handler', 'iTerm']), \
             mock.patch.dict(os.environ, {'ATMUX_BIN': '/opt/bin/atmux'}), \
             mock.patch.object(cli, '_configure_gateway_mode') as configure, \
             mock.patch('sys.stdout', new=out):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('on open location this_URL', out.getvalue())
        self.assertIn('/opt/bin/atmux', out.getvalue())
        configure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
