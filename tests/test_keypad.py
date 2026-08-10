"""Tests for the controls the app publishes to a touch client.

The point of this module is that there is one list of what you can do, and
it is the app's own. Everything here guards a way that could stop being
true: a key that types something other than its label, a control that
survives into a screen it does not belong to, an order that buries the
action the app itself considers primary.
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import keypad


class Binding:
    """Enough of textual.binding.Binding to exercise the filter."""

    def __init__(self, key, description='', show=False, action='noop'):
        self.key = key
        self.description = description
        self.show = show
        self.action = action


class Entry:
    """Enough of textual's ActiveBinding: a binding plus who owns it."""

    def __init__(self, binding, enabled=True, node='app'):
        self.binding = binding
        self.enabled = enabled
        self.node = node


def active(*bindings) -> dict:
    return {b.key: Entry(b) for b in bindings}


class KeySequenceTests(unittest.TestCase):
    """A button has to type what its label promises, or not exist."""

    def test_the_keys_atmux_actually_binds(self):
        for name, expected in (('s', 's'), ('z', 'z'), ('question_mark', '?'),
                               ('enter', '\r'), ('escape', '\x1b')):
            with self.subTest(key=name):
                self.assertEqual(keypad.key_sequence(name), expected)

    def test_control_keys_become_control_characters(self):
        self.assertEqual(keypad.key_sequence('ctrl+s'), '\x13')
        self.assertEqual(keypad.key_sequence('ctrl+A'), '\x01')

    def test_arrows_and_pages_are_the_csi_forms(self):
        """Not offered as buttons, but the mapping has to be right: the
        client decides what repeats by looking for the CSI prefix."""
        for name, expected in (('up', '\x1b[A'), ('down', '\x1b[B'),
                               ('right', '\x1b[C'), ('left', '\x1b[D'),
                               ('pageup', '\x1b[5~'), ('pagedown', '\x1b[6~')):
            with self.subTest(key=name):
                self.assertEqual(keypad.key_sequence(name), expected)
                self.assertTrue(expected.startswith('\x1b['))

    def test_an_unknown_key_yields_no_button_rather_than_a_guess(self):
        """The failure this prevents is silent: a button labelled with a real
        action that types something else. No button is strictly better."""
        for name in ('f7', 'ctrl+shift+x', 'super+k', '', None, 'nonsense'):
            with self.subTest(key=name):
                self.assertIsNone(keypad.key_sequence(name))


class VisibleBindingTests(unittest.TestCase):
    def test_what_the_app_shows_comes_first(self):
        """The footer is the app's own statement of what matters, so no
        client has to invent a ranking of its own."""
        chosen = keypad.visible_bindings(active(
            Binding('v', 'View output'),
            Binding('s', 'SSH to node', show=True),
            Binding('n', 'New session'),
            Binding('z', 'Layout', show=True),
        ))
        self.assertEqual([e.binding.description for e in chosen],
                         ['SSH to node', 'Layout', 'View output',
                          'New session'])

    def test_movement_and_focus_and_quit_are_never_buttons(self):
        """Rows are directly tappable -- measured -- so arrows would spend the
        pad's best row on a gesture that already works; you focus by touching
        the thing; and a quit key a thumb-width from the rest closes the
        dashboard by accident."""
        chosen = keypad.visible_bindings(active(
            Binding('up', 'Cursor up'), Binding('down', 'Cursor down'),
            Binding('tab', 'Focus Next'), Binding('q', 'Quit'),
            Binding('s', 'SSH to node'),
        ))
        self.assertEqual([e.binding.key for e in chosen], ['s'])

    def test_two_buttons_that_say_the_same_thing_become_one(self):
        """The help screen closes on escape, on q and on ?, all described
        'Close'. Three identical buttons reads as broken."""
        chosen = keypad.visible_bindings(active(
            Binding('escape', 'Close'), Binding('question_mark', 'Close'),
        ))
        self.assertEqual([e.binding.key for e in chosen], ['escape'])

    def test_a_disabled_binding_is_not_offered(self):
        bindings = active(Binding('s', 'SSH to node'))
        bindings['s'].enabled = False
        self.assertEqual(keypad.visible_bindings(bindings), [])

    def test_a_binding_with_no_description_is_not_offered(self):
        """There would be nothing to write on it."""
        self.assertEqual(keypad.visible_bindings(active(Binding('s', ''))), [])

    def test_the_owning_node_travels_with_the_control(self):
        """Attach is bound on the table, not the app. Running it against the
        app looks for an action that is not there, and the button does
        nothing at all."""
        bindings = active(Binding('enter', 'Attach', action='select_cursor'))
        bindings['enter'].node = 'the-table'
        self.assertEqual(keypad.visible_bindings(bindings)[0].node,
                         'the-table')

    def test_nothing_at_all_is_an_empty_list_not_an_error(self):
        for value in (None, {}, []):
            with self.subTest(value=value):
                self.assertEqual(keypad.visible_bindings(value), [])


class PayloadTests(unittest.TestCase):
    def test_a_control_carries_its_label_and_its_bytes(self):
        self.assertEqual(
            keypad.keys_for(active(Binding('z', 'Layout', show=True))),
            [{'k': 'z', 'l': 'Layout'}])

    def test_a_key_with_no_sequence_is_dropped_rather_than_sent_empty(self):
        self.assertEqual(keypad.keys_for(active(Binding('f7', 'Something'))),
                         [])

    def test_the_payload_survives_a_round_trip(self):
        keys = keypad.keys_for(active(
            Binding('enter', 'Attach', show=True), Binding('z', 'Layout')))
        decoded = keypad.decode(keypad.encode('app', keys))
        self.assertEqual(decoded, {'mode': 'app', 'keys': keys})

    def test_the_terminator_can_never_appear_inside_the_payload(self):
        """A control character in a label would end the escape sequence early
        and spray the rest onto the screen. json escapes every one of them."""
        raw = keypad.encode('app', [{'k': '\x1b\\', 'l': 'ends\x1b\\it'}])
        self.assertEqual(raw.count('\x1b\\'), 1)
        self.assertEqual(raw.index('\x1b\\'), len(raw) - 2)
        self.assertIsNotNone(keypad.decode(raw))

    def test_the_escape_sequence_is_shaped_the_way_a_terminal_expects(self):
        raw = keypad.encode('app', [])
        self.assertTrue(raw.startswith(f'\x1b]{keypad.OSC};'))
        self.assertTrue(raw.endswith('\x1b\\'))
        json.loads(raw[len(f'\x1b]{keypad.OSC};'):-2])

    def test_garbage_decodes_to_nothing_rather_than_raising(self):
        for raw in ('', 'hello', '\x1b]7710;{oops\x1b\\', '\x1b]99;{}\x1b\\'):
            with self.subTest(raw=raw):
                self.assertIsNone(keypad.decode(raw))

    def test_a_suspended_app_publishes_no_keys_of_its_own(self):
        """Attaching hands the screen to tmux, which has no bindings to
        publish. This used to send seven -- detach, ^C, ^D, prefix, ^Z, PgUp,
        PgDn -- every one of which describes a *terminal* rather than atmux.
        The client owns those now, and has to: it is also what you are left
        with when the pty is running something that publishes nothing."""
        self.assertEqual(list(keypad.EXTERNAL_KEYS), [])

    def test_the_handover_still_carries_the_one_thing_a_client_cannot_know(self):
        """Which is the prefix byte. Twelve tmux buttons are built from it."""
        decoded = keypad.decode(
            keypad.encode('external', keypad.EXTERNAL_KEYS, '\x01'))
        self.assertEqual(decoded, {'mode': 'external', 'keys': [],
                                   'prefix': '\x01'})

    def test_no_prefix_is_absent_rather_than_empty(self):
        """A client that hears nothing keeps its own default. One that heard
        '' would have to decide what that meant, and would decide alone."""
        self.assertNotIn('prefix', keypad.decode(keypad.encode('app', [])))


class TmuxPrefixTests(unittest.TestCase):
    """Every tmux control is a chord, and the first byte is not guessable.

    ``set -g prefix C-a`` is a common rebinding and nothing on the wire
    announces it. Getting this wrong does not fail loudly -- it makes twelve
    buttons quietly mean something else.
    """

    def test_the_spellings_tmux_itself_uses(self):
        for name, expected in (('C-b', '\x02'), ('C-a', '\x01'),
                               ('C-A', '\x01'), ('c-b', '\x02'),
                               (' C-b ', '\x02')):
            with self.subTest(name=name):
                self.assertEqual(keypad.prefix_sequence(name), expected)

    def test_the_caret_form_people_write_in_notes(self):
        self.assertEqual(keypad.prefix_sequence('^B'), '\x02')
        self.assertEqual(keypad.prefix_sequence('^a'), '\x01')

    def test_the_control_codes_that_are_not_letters(self):
        """C-@ and C-Space are both NUL, which is what tmux means by either --
        and `show-options -gv prefix` prints the second spelling."""
        for name, expected in (('C-@', '\x00'), ('C-Space', '\x00'),
                               ('C-[', '\x1b'), ('C-\\', '\x1c'),
                               ('C-]', '\x1d'), ('C-^', '\x1e'),
                               ('C-_', '\x1f'), ('C-?', '\x7f')):
            with self.subTest(name=name):
                self.assertEqual(keypad.prefix_sequence(name), expected)

    def test_a_meta_prefix_is_escape_then_the_key(self):
        self.assertEqual(keypad.prefix_sequence('M-a'), '\x1ba')
        self.assertEqual(keypad.prefix_sequence('M-Space'), '\x1b ')

    def test_a_bare_key_is_taken_literally(self):
        """`set -g prefix \\`` and `set -g prefix ^` are both real."""
        self.assertEqual(keypad.prefix_sequence('`'), '`')
        self.assertEqual(keypad.prefix_sequence('^'), '^')

    def test_a_name_that_names_nothing_yields_none_rather_than_a_guess(self):
        for name in ('', '   ', 'C-', 'C-bb', 'M-', 'M-Enter', 'nonsense',
                     'F1', None, 5, 'C-\x01', '\x02'):
            with self.subTest(name=name):
                self.assertIsNone(keypad.prefix_sequence(name))

    def test_the_default_is_tmuxs_own(self):
        self.assertEqual(keypad.tmux_prefix({}), '\x02')
        self.assertEqual(keypad.PREFIX_DEFAULT, '\x02')

    def test_a_rebound_prefix_is_read_from_the_environment(self):
        self.assertEqual(
            keypad.tmux_prefix({keypad.TMUX_PREFIX_ENV: 'C-a'}), '\x01')

    def test_an_unreadable_setting_falls_back_rather_than_breaking_every_button(
            self):
        """None from prefix_sequence means "I cannot tell". Sending nothing
        would leave the client without a prefix at all; sending C-b leaves it
        with the one that is right for almost everyone."""
        for value in ('', 'garbage', 'C-'):
            with self.subTest(value=value):
                self.assertEqual(
                    keypad.tmux_prefix({keypad.TMUX_PREFIX_ENV: value}),
                    '\x02')


class TouchModeTests(unittest.TestCase):
    """Who draws the controls. Exactly one surface should."""

    def test_a_plain_terminal_is_neither(self):
        for value in ('', 'no', '0', 'off', 'maybe'):
            with self.subTest(value=value):
                self.assertEqual(keypad.touch_mode({keypad.TOUCH_ENV: value}),
                                 '')
        self.assertEqual(keypad.touch_mode({}), '')

    def test_the_two_kinds_of_touch_client_are_told_apart(self):
        self.assertEqual(keypad.touch_mode({keypad.TOUCH_ENV: 'web'}), 'web')
        self.assertEqual(keypad.touch_mode({keypad.TOUCH_ENV: 'local'}),
                         'local')

    def test_a_bare_yes_means_the_client_cannot_draw_for_itself(self):
        """The conservative reading: draw the controls in the grid, because
        something that only says 'touch' has given no reason to believe it
        can draw them anywhere else."""
        for value in ('1', 'true', 'YES', 'on'):
            with self.subTest(value=value):
                self.assertEqual(keypad.touch_mode({keypad.TOUCH_ENV: value}),
                                 'local')


class DirectAttachAnnouncementTests(unittest.TestCase):
    """`atmux -a` hands the screen to tmux without ever building a dashboard.

    Every other handover says so -- App.suspend publishes 'external' before
    it yields -- but this one has no app to publish from, and it is the path
    a phone takes when it taps a session row. A client that is never told
    what is on the other end draws the wrong keys and has to refuse the
    swipe, which is what it did until this existed.
    """

    def setUp(self):
        from autotmux import cli
        self.cli = cli
        self.saved = os.environ.get(keypad.TOUCH_ENV)
        if self.saved is not None:
            del os.environ[keypad.TOUCH_ENV]

    def tearDown(self):
        os.environ.pop(keypad.TOUCH_ENV, None)
        if self.saved is not None:
            os.environ[keypad.TOUCH_ENV] = self.saved

    def _announced(self):
        stream = io.StringIO()
        with mock.patch.object(self.cli.sys, 'stdout', stream):
            self.cli._announce_external()
        return stream.getvalue()

    def test_a_web_client_is_told_the_screen_became_tmux(self):
        os.environ[keypad.TOUCH_ENV] = 'web'
        payload = keypad.decode(self._announced())
        self.assertIsNotNone(payload)
        self.assertEqual(payload['mode'], 'external')

    def test_the_prefix_travels_with_it(self):
        """The client builds its tmux chords from this. Sending the mode
        without the prefix would leave every chord typing the default."""
        os.environ[keypad.TOUCH_ENV] = 'web'
        os.environ[keypad.TMUX_PREFIX_ENV] = 'C-a'
        try:
            payload = keypad.decode(self._announced())
        finally:
            del os.environ[keypad.TMUX_PREFIX_ENV]
        self.assertEqual(payload['prefix'], '\x01')

    def test_a_terminal_hears_nothing(self):
        """This is bytes on the same pty as the screen. A client that did not
        ask for it would be a terminal, and a terminal draws no buttons."""
        for value in ('', 'local', '1'):
            with self.subTest(value=value):
                if value:
                    os.environ[keypad.TOUCH_ENV] = value
                else:
                    os.environ.pop(keypad.TOUCH_ENV, None)
                self.assertEqual(self._announced(), '')

    def test_a_closed_pty_does_not_take_the_attach_down_with_it(self):
        os.environ[keypad.TOUCH_ENV] = 'web'
        broken = mock.Mock()
        broken.write.side_effect = OSError('closed')
        with mock.patch.object(self.cli.sys, 'stdout', broken):
            self.cli._announce_external()          # must not raise

    def test_the_attach_announces_before_handing_over(self):
        """Order matters: tmux owns the terminal from the moment it starts,
        and a client told afterwards spends the gap holding the app's keys."""
        order = []
        with mock.patch.object(self.cli, '_request_daemon_start'), \
             mock.patch.object(self.cli, '_will_nest_tmux', return_value=False), \
             mock.patch.object(self.cli, '_announce_external',
                               side_effect=lambda: order.append('announce')), \
             mock.patch.object(self.cli, '_run_user_command',
                               side_effect=lambda *a: (order.append('attach'),
                                                       (0, ''))[1]):
            self.assertEqual(self.cli._direct_attach('localhost:main'), 0)
        self.assertEqual(order, ['announce', 'attach'])

    def test_a_rejected_target_announces_nothing(self):
        """It never reaches tmux, so saying it did would leave the client
        holding tmux's keys in front of an error message."""
        with mock.patch.object(self.cli, '_announce_external') as announce:
            for target in ('nocolon', ':session', 'node:', 'bad node:s'):
                with self.subTest(target=target):
                    self.assertEqual(self.cli._direct_attach(target), 2)
        announce.assert_not_called()
