"""What you can do right now, published so a touch client can draw it.

A TUI encodes its interaction model as keystrokes on a character grid. A
phone has neither a keyboard nor a wide grid, so every action has to be
translated back into a keystroke -- which is why the browser client grew a
keyboard emulator, and why that emulator grew pages, and why the layout key
ended up somewhere nobody would look for it.

The way out is that the app already knows its own bindings, and knows them
per screen: ``App.active_bindings`` changes when a modal opens or focus
moves. Publishing that means the buttons on a phone are the app's real
bindings rather than a copy of them written by hand in javascript -- a copy
being exactly the thing that drifts.

Everything here is pure. The escape sequence is built from data and parsed
by a test; nothing in this module needs a terminal, a browser or Textual.
"""

from __future__ import annotations

import json
import os

# Which client is on the other end, and therefore who draws the controls.
# Exactly one surface should: two is how the same action ends up in two
# places disagreeing about its label.
#
#   'web'   the browser client draws buttons outside the grid, so the app
#           publishes its bindings and draws nothing extra itself.
#   'local' a touch client with no way to draw its own controls (a phone
#           ssh app), so the app draws them inside the grid.
#   ''      an ordinary terminal with a keyboard. Neither.
#
# Declared, not detected: nothing on the far side of an ssh connection says
# whether a human is using a mouse or a thumb, and a narrow terminal is not
# evidence -- a narrow window on a desktop is still a desktop.
TOUCH_ENV = 'AUTOTMUX_TOUCH'
TOUCH_MODES = ('web', 'local')


def touch_mode(env: dict | None = None) -> str:
    """Which touch client is attached, or '' for a keyboard."""
    raw = (env if env is not None else os.environ).get(TOUCH_ENV, '')
    value = str(raw).strip().lower()
    if value in TOUCH_MODES:
        return value
    # A bare truthy value predates the distinction and means "a touch client
    # that cannot draw for itself", which is the conservative reading.
    return 'local' if value in ('1', 'true', 'yes', 'on') else ''


# No registry exists for OSC codes; terminals simply ignore ones they do not
# implement, so the cost of a collision is a client that draws nothing rather
# than a screen full of garbage. This one is not used by any terminal we could
# find (iTerm2 is 1337, VS Code 633, urxvt 777, shell integration 133).
OSC = 7710
_ST = '\x1b\\'


# Textual names a key; a terminal wants bytes. Anything missing here yields no
# button at all, which is the safe direction: a key nobody offered cannot be
# the wrong key sent silently.
_NAMED = {
    'enter': '\r',
    'escape': '\x1b',
    'tab': '\t',
    'backspace': '\x7f',
    'delete': '\x1b[3~',
    'space': ' ',
    'up': '\x1b[A',
    'down': '\x1b[B',
    'right': '\x1b[C',
    'left': '\x1b[D',
    'home': '\x1b[H',
    'end': '\x1b[F',
    'pageup': '\x1b[5~',
    'pagedown': '\x1b[6~',
    'question_mark': '?',
    'exclamation_mark': '!',
    'full_stop': '.',
    'comma': ',',
    'minus': '-',
    'plus': '+',
    'equals_sign': '=',
    'underscore': '_',
    'slash': '/',
    'colon': ':',
    'semicolon': ';',
    'at': '@',
    'number_sign': '#',
    'percent_sign': '%',
    'asterisk': '*',
}

# Never worth a button, for three separate reasons:
#
#   movement  the list is directly tappable and scrollable -- measured, a tap
#             on a row moves the selection -- so arrows would spend the most
#             valuable row on the pad duplicating a gesture that already works
#   focus     you focus by touching the thing, so "Focus Next" is a button
#             whose label means nothing to the person holding the phone
#   quit      a key that closes the dashboard, one thumb-width from the rest
_SKIP = frozenset({
    'up', 'down', 'left', 'right', 'home', 'end', 'pageup', 'pagedown',
    'tab', 'shift+tab',
    'q', 'ctrl+c', 'ctrl+q',
})


def key_sequence(key: str) -> str | None:
    """The bytes a terminal sends for a Textual key name, or None.

    None means "do not offer this": better no button than a button that
    types something other than what its label promises.
    """
    if not isinstance(key, str) or not key:
        return None
    name = key.strip()
    if name in _NAMED:
        return _NAMED[name]
    if len(name) == 1 and name.isprintable():
        return name
    if name.startswith('ctrl+'):
        rest = name[5:]
        if len(rest) == 1 and 'a' <= rest.lower() <= 'z':
            return chr(ord(rest.lower()) - 96)
    return None


def _label(binding) -> str:
    text = str(getattr(binding, 'description', '') or '').strip()
    return text


def visible_bindings(bindings) -> list:
    """The controls worth offering, in the app's own order.

    Takes a mapping shaped like ``App.active_bindings`` and returns its
    entries. The app's own footer is its statement of what matters, so the
    keys it shows come first and no consumer has to rank anything itself.

    Entries, not bindings: an entry carries the node that owns the binding,
    and ``Attach`` lives on the table rather than on the app -- running it
    against the app would look for an action that is not there.

    Both clients filter through here. A browser needs the bytes to send and a
    widget needs the action to run, but which controls exist is one question
    and deserves one answer.
    """
    shown, hidden, seen, labelled = [], [], set(), set()
    for entry in (bindings or {}).values():
        binding = getattr(entry, 'binding', None)
        if binding is None or not getattr(entry, 'enabled', True):
            continue
        key = str(getattr(binding, 'key', '') or '')
        if key in _SKIP or key in seen:
            continue
        label = _label(binding)
        sequence = key_sequence(key)
        if not label or sequence is None:
            continue
        # Two buttons that say the same thing are one button. The help screen
        # closes on escape, on `q` and on `?`, all described as "Close", and
        # three identical keys is a row that looks broken. First wins, and the
        # first is the one the screen declared first.
        if label.casefold() in labelled:
            continue
        labelled.add(label.casefold())
        seen.add(key)
        (shown if getattr(binding, 'show', False) else hidden).append(entry)
    return shown + hidden


def keys_for(bindings) -> list[dict]:
    """The same controls, as label plus the bytes a terminal would send."""
    out = []
    for entry in visible_bindings(bindings):
        binding = entry.binding
        sequence = key_sequence(str(getattr(binding, 'key', '') or ''))
        if sequence is not None:
            out.append({'k': sequence, 'l': _label(binding)})
    return out


# What a raw terminal needs, for the stretch where there is no app to ask.
# Attaching suspends the dashboard and hands the screen to tmux, which draws
# no buttons and answers no questions -- so this set is static because the
# situation genuinely is. Detach is first because it is the one key nobody
# can guess, and the whole reason the handover banner exists.
EXTERNAL_KEYS = (
    {'k': '\x02d', 'l': 'detach'},
    {'k': '\x03', 'l': '^C'},
    {'k': '\x04', 'l': '^D'},
    {'k': '\x02', 'l': 'prefix'},
    {'k': '\x1a', 'l': '^Z'},
    {'k': '\x1b[5~', 'l': 'PgUp'},
    {'k': '\x1b[6~', 'l': 'PgDn'},
)


def encode(mode: str, keys) -> str:
    """The escape sequence carrying one set of buttons.

    json.dumps escapes every control character, so the payload can never
    contain the terminator that ends the sequence.
    """
    body = json.dumps({'mode': mode, 'keys': list(keys)},
                      separators=(',', ':'), ensure_ascii=True)
    return f'\x1b]{OSC};{body}{_ST}'


def decode(sequence: str) -> dict | None:
    """Inverse of encode, for tests and for anyone reading a capture."""
    prefix = f'\x1b]{OSC};'
    if not sequence.startswith(prefix) or not sequence.endswith(_ST):
        return None
    try:
        return json.loads(sequence[len(prefix):-len(_ST)])
    except ValueError:
        return None
