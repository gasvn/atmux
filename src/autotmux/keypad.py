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

# Not published, for three separate reasons. Note what this list is *not*:
# it is not "keys a phone does not need". Movement is left out because it
# belongs to the client, which owns terminal primitives and must offer them
# whether or not anything is publishing -- see NAV_KEYS in app.js.
#
# It was once left out on the theory that a tap on a row selects it, which
# is false. Measured against the cursor colour rather than against "did the
# screen change" (the table re-sorts by idle time every few seconds, which
# looks the same): a mouse click does not move the selection, a finger tap
# does not move the selection, an arrow key does. xterm.js has no touch
# support, and this table attaches on a single click, so routing taps into
# it would turn a mis-tap into an attach.
#
#   movement  the client's, not the app's -- and the only way to move
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


# Nothing, and that is the answer rather than an omission. Attaching suspends
# the dashboard and hands the screen to tmux, which has no bindings to publish
# and cannot be asked about the ones it has.
#
# This used to hold seven: detach, ^C, ^D, prefix, ^Z, PgUp, PgDn. Every one of
# them is a property of *a terminal*, not of atmux -- and a copy of the
# terminal's vocabulary kept on this side is exactly the thing the docstring
# above warns about. The client has to own that vocabulary anyway, because it
# is also what you are left with when the pty is running something that
# publishes nothing at all. What a client genuinely cannot know is the prefix
# byte, so that is what travels instead.
EXTERNAL_KEYS: tuple = ()


# ── the tmux prefix ─────────────────────────────────────────────────────────
#
# Every tmux control is a chord: the prefix, then one key. A touch client can
# offer each chord as a single button -- which is the only way tmux is usable
# by thumb, since holding one key while pressing another is not a gesture a
# thumb has -- but it cannot work out the first byte for itself. ``set -g
# prefix C-a`` is a common rebinding and nothing on the wire announces it.
#
# So it is stated rather than guessed, and stated once. C-b is tmux's own
# default and is right unless the user has said otherwise, which they can here.
TMUX_PREFIX_ENV = 'AUTOTMUX_TMUX_PREFIX'
PREFIX_DEFAULT = '\x02'                                   # C-b

# The control characters that are not a letter. C-@ and C-Space are both NUL,
# which is what tmux means by either spelling -- and ``show-options -gv prefix``
# prints the second one, so 'Space' is a name this has to know.
_CTRL_EXTRA = {'@': '\x00', 'Space': '\x00', '[': '\x1b', '\\': '\x1c',
               ']': '\x1d', '^': '\x1e', '_': '\x1f', '?': '\x7f'}


def prefix_sequence(name) -> str | None:
    """The byte tmux's ``prefix`` option names, or None if it names nothing.

    Accepts the spelling tmux uses (``C-b``, ``M-x``) and the caret form
    people write in notes (``^B``). None rather than a fallback, for the same
    reason key_sequence returns None: a wrong first byte does not fail, it
    quietly makes every tmux button mean something else.
    """
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text:
        return None
    if len(text) == 2 and text[0] == '^':
        text = 'C-' + text[1]
    head, rest = text[:2].upper(), text[2:]
    if head == 'C-':
        if len(rest) == 1 and 'a' <= rest.lower() <= 'z':
            return chr(ord(rest.lower()) - 96)
        return _CTRL_EXTRA.get(rest)
    if head == 'M-':
        if rest == 'Space':
            return '\x1b '
        if len(rest) == 1 and rest.isprintable():
            return '\x1b' + rest
        return None
    if len(text) == 1 and text.isprintable():
        return text
    return None


def tmux_prefix(env: dict | None = None) -> str:
    """The prefix byte to build chords from. C-b unless told otherwise."""
    raw = (env if env is not None else os.environ).get(TMUX_PREFIX_ENV, '')
    return prefix_sequence(raw) or PREFIX_DEFAULT


def encode(mode: str, keys, prefix: str = '') -> str:
    """The escape sequence carrying one set of buttons.

    json.dumps escapes every control character, so the payload can never
    contain the terminator that ends the sequence.
    """
    body: dict = {'mode': mode, 'keys': list(keys)}
    # Omitted rather than sent empty: a client that hears nothing keeps its own
    # default, and a client that hears '' would have to decide what that meant.
    if prefix:
        body['prefix'] = prefix
    return (f'\x1b]{OSC};'
            f'{json.dumps(body, separators=(",", ":"), ensure_ascii=True)}{_ST}')


def decode(sequence: str) -> dict | None:
    """Inverse of encode, for tests and for anyone reading a capture."""
    prefix = f'\x1b]{OSC};'
    if not sequence.startswith(prefix) or not sequence.endswith(_ST):
        return None
    try:
        return json.loads(sequence[len(prefix):-len(_ST)])
    except ValueError:
        return None
