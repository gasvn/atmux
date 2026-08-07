"""Tests for the controls the app publishes to a touch client.

The point of this module is that there is one list of what you can do, and
it is the app's own. Everything here guards a way that could stop being
true: a key that types something other than its label, a control that
survives into a screen it does not belong to, an order that buries the
action the app itself considers primary.
"""
import json
import os
import sys
import unittest

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

    def test_detach_is_offered_where_no_app_can_be_asked(self):
        """Attaching hands the screen to tmux, which draws no buttons. Ctrl-B
        then d is unreachable without a keyboard, and being stuck inside a
        session is the failure this whole feature would otherwise create."""
        labels = {k['l']: k['k'] for k in keypad.EXTERNAL_KEYS}
        self.assertEqual(labels.get('detach'), '\x02d')
        self.assertEqual(labels.get('^C'), '\x03')


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
