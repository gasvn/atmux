"""The macOS ``atmux://`` link handler: session tagging and applet source.

Clicking *Attach* in a chat message should land in the session -- and if a
window is already sitting in that session, in **that** window rather than a
second copy of it.  Two tmux clients on one session both work, but they share a
size, so opening a duplicate silently shrinks whichever window was larger.

Finding the right window needs the terminal to know which target each one
holds.  iTerm2 takes that from the running program: an ``OSC 1337 SetUserVar``
escape sets a per-session variable the handler can read back later.  Tagging
from inside atmux -- rather than from the applet at window-creation time --
means *every* attach is tagged, including ones started from the dashboard, so a
link can find a window the link never opened.  Terminal.app has no equivalent
escape, so there the applet tags what it opens itself and reuse is limited to
link-opened windows.

The AppleScript is generated here, rather than written into the installer,
because its quoting is the part that has actually broken: the URL is untrusted
(anyone who can post to the channel can craft one) and passes through an
AppleScript literal, a shell word, and argv on its way to the attach.  Building
it in Python puts every one of those layers under test.
"""

from __future__ import annotations

import base64
import os
import sys

from .notify import attach_url

# iTerm2 namespaces program-set variables under `user.`; the escape sets the
# bare name and readers ask for the prefixed one.
USER_VAR = 'atmuxTarget'
USER_VAR_REF = f'user.{USER_VAR}'

TERMINALS = ('iTerm', 'Terminal')

# Where a failed link open leaves its breadcrumb. The failure is invisible from
# the user's side -- the terminal comes forward and nothing happens -- so there
# has to be somewhere to look.
LOG_PATH = '/tmp/atmux-url-handler.log'

# How long to let the linking app finish re-activating itself before taking the
# foreground back. Slack and browsers commonly raise their own window a beat
# after handing the URL over; one repeat activation after that settles wins
# without starting a focus fight.
FOCUS_SETTLE_SECONDS = 0.45


def applescript_literal(value: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal."""
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def _literal(value: str) -> str:
    return f'"{applescript_literal(value)}"'


def is_iterm(env=None) -> bool:
    """Whether the terminal on the other end of this process is iTerm2.

    ``LC_TERMINAL`` is checked as well because iTerm forwards it over SSH,
    where ``TERM_PROGRAM`` is whatever the remote login shell sets.
    """
    env = os.environ if env is None else env
    return (env.get('TERM_PROGRAM') == 'iTerm.app'
            or env.get('LC_TERMINAL') == 'iTerm2')


def set_user_var_sequence(name: str, value: str) -> str:
    """The iTerm2 escape that sets one user variable on the current session.

    An empty value clears the variable, which is how a window stops advertising
    a session it is no longer attached to.
    """
    encoded = base64.b64encode(str(value).encode('utf-8')).decode('ascii')
    return f'\033]1337;SetUserVar={name}={encoded}\a'


def attach_tag(node: str, session: str) -> str:
    """The canonical tag for a target: the same URL a reminder would link to.

    Sharing one spelling with :func:`notify.attach_url` is what makes the match
    work -- the applet compares the URL it was handed against this tag, so both
    sides have to agree character for character.  A hand-typed link that
    percent-encodes differently simply misses and opens a new window, which is
    the old behaviour rather than a wrong one.
    """
    return attach_url(node, session)


def _write_sequence(sequence: str, stream, env) -> bool:
    """Emit a terminal escape, if there is a terminal that understands it."""
    stream = sys.stdout if stream is None else stream
    if not sequence or not is_iterm(env):
        return False
    # Inside tmux the escape would be swallowed rather than forwarded: the
    # passthrough wrapper needs `allow-passthrough`, which is off by default, so
    # emitting it would more often leave stray bytes than set the variable.
    env = os.environ if env is None else env
    if env.get('TMUX'):
        return False
    try:
        if not stream.isatty():
            return False
        stream.write(sequence)
        stream.flush()
    except (OSError, ValueError, AttributeError):
        return False
    return True


def mark_attached(node: str, session: str, stream=None, env=None) -> bool:
    """Advertise, to the terminal, which session this window is now showing."""
    tag = attach_tag(node, session)
    if not tag:
        return False
    return _write_sequence(set_user_var_sequence(USER_VAR, tag), stream, env)


def clear_attached(stream=None, env=None) -> bool:
    """Stop advertising a session once the attach has returned.

    Without this a window that outlives its attach keeps claiming the session,
    and a later link would raise a terminal sitting at a dead prompt.
    """
    return _write_sequence(set_user_var_sequence(USER_VAR, ''), stream, env)


class attached:
    """Tag the terminal for the duration of an attach, whatever the outcome."""

    def __init__(self, node: str, session: str, stream=None, env=None):
        self._args = (node, session, stream, env)
        self._marked = False

    def __enter__(self):
        node, session, stream, env = self._args
        self._marked = mark_attached(node, session, stream, env)
        return self

    def __exit__(self, *_exc):
        if self._marked:
            clear_attached(self._args[2], self._args[3])
        return False


# --- applet source -------------------------------------------------------

_PREAMBLE = '''\
on open location this_URL
    my dispatch(this_URL)
end open location

on dispatch(this_URL)
    -- Every open leaves a line behind. A link that fails is invisible from the
    -- user's side -- the terminal comes forward and nothing happens -- and
    -- there is no way to attach a debugger to a LaunchServices callback, so the
    -- log is the only way anyone finds out what went wrong.
    my logLine("open " & this_URL)
    -- Reuse is an optimisation, never a failure mode: anything unexpected in
    -- the lookup falls through to opening a window, which is what this handler
    -- did before reuse existed.
    try
        if my focusExisting(this_URL) then
            my logLine("focused an existing window")
            my holdFocus()
            return
        end if
    on error errMsg
        my logLine("reuse lookup failed: " & (errMsg as text))
    end try
    try
        my openNew(my commandFor(this_URL), this_URL)
        my logLine("opened a new window")
        my holdFocus()
    on error errMsg
        my logLine("open failed: " & (errMsg as text))
        my reportFailure(errMsg)
    end try
end dispatch

on logLine(msg)
    try
        do shell script "printf '%s | %s\\n' " & ¬
            quoted form of ((current date) as text) & " " & ¬
            quoted form of (msg as text) & " >> " & quoted form of {log}
    end try
end logLine

on commandFor(this_URL)
    -- Two things are going on here.
    --
    -- iTerm2 and Terminal exec the command as argv rather than handing it to a
    -- shell, so a bare `quoted form of` path arrives with its quotes intact and
    -- the binary is not found -- the window opens and dies immediately. Hence
    -- the explicit shell; the inner quoting is what keeps the URL, which is
    -- untrusted, from being reinterpreted.
    --
    -- And it is a *login* shell because a window opened this way inherits the
    -- GUI session's environment, not the user's. That PATH is /usr/bin:/bin
    -- and friends, so `tmux` is not on it and a local attach dies with status
    -- 127 before it ever draws. Running the login shell makes a clicked link
    -- behave like typing the command.
    set inner to quoted form of {atmux} & " --open-url " & quoted form of this_URL
    return {shell} & " -l -c " & quoted form of ("exec " & inner)
end commandFor

on holdFocus()
    -- Whatever owned the link commonly re-activates itself a moment after
    -- handing the URL over, leaving the new window behind it.
    delay {settle}
    try
        tell application {app} to activate
    end try
end holdFocus

on reportFailure(errMsg)
    -- The log already has the detail; this is only so the user learns the click
    -- did nothing without having to know the log exists. Notification consent
    -- is its own permission, so a failure to show it must not mask the error.
    try
        display notification (errMsg as text) with title "AutoTmux link failed"
    end try
end reportFailure
'''

_ITERM_BODY = '''
on focusExisting(this_URL)
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    set tag to ""
                    try
                        tell s to set tag to (variable named {var})
                    end try
                    if tag is this_URL and my hasProcess(tty of s) then
                        activate
                        tell w to select
                        tell t to select
                        tell s to select
                        return true
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return false
end focusExisting

on hasProcess(ttyPath)
    -- A window whose atmux was killed outright keeps the tag but has nothing
    -- running on its tty. Raising that would show a dead terminal, so the tag
    -- alone is not enough to count as a match.
    if ttyPath is missing value then return false
    set devName to ttyPath as text
    if devName is "" then return false
    try
        return (do shell script "ps -t " & quoted form of devName & " -o pid=") is not ""
    on error
        return false
    end try
end hasProcess

on openNew(theCommand, this_URL)
    -- No tagging here: atmux sets the user variable itself once it attaches,
    -- which also covers sessions this handler never opened.
    tell application "iTerm"
        activate
        create window with default profile command theCommand
    end tell
end openNew
'''

_TERMINAL_BODY = '''
on tagFor(this_URL)
    return "atmux " & this_URL
end tagFor

on focusExisting(this_URL)
    set wanted to my tagFor(this_URL)
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                set tag to ""
                try
                    set tag to custom title of t
                end try
                if tag is wanted and (processes of t) is not {{}} then
                    activate
                    set frontmost of w to true
                    set selected of t to true
                    return true
                end if
            end repeat
        end repeat
    end tell
    return false
end focusExisting

on openNew(theCommand, this_URL)
    -- Terminal.app has no way for the running program to tag its own window, so
    -- the tag goes on here and reuse only ever finds windows this handler
    -- opened. The custom title is visible, which is the cost of the mechanism.
    tell application "Terminal"
        activate
        set newTab to do script theCommand
        try
            set custom title of newTab to my tagFor(this_URL)
        end try
    end tell
end openNew
'''


FALLBACK_SHELL = '/bin/sh'


def login_shell(env=None, is_executable=None) -> str:
    """The user's shell, for running a clicked link the way they would type it.

    Taken from the environment at install time, which is an interactive shell
    and therefore knows.  Anything that is not an absolute path to something
    executable falls back to ``/bin/sh``: a wrong shell here would break every
    link, and ``/bin/sh -l`` at least reads a profile.
    """
    env = os.environ if env is None else env
    if is_executable is None:
        def is_executable(path):
            return os.path.isfile(path) and os.access(path, os.X_OK)
    candidate = (env.get('SHELL') or '').strip()
    if candidate.startswith('/') and is_executable(candidate):
        return candidate
    return FALLBACK_SHELL


def applescript(atmux_path: str, terminal: str = 'iTerm',
                shell: str | None = None) -> str:
    """The full applet source for one terminal application."""
    if terminal not in TERMINALS:
        raise ValueError(f'unsupported terminal {terminal!r}')
    body = _ITERM_BODY if terminal == 'iTerm' else _TERMINAL_BODY
    preamble = _PREAMBLE.format(
        atmux=_literal(atmux_path),
        app=_literal(terminal),
        log=_literal(LOG_PATH),
        settle=FOCUS_SETTLE_SECONDS,
        shell=_literal(login_shell() if shell is None else shell),
    )
    return preamble + body.format(var=_literal(USER_VAR_REF))


def default_terminal(exists=os.path.isdir) -> str:
    """iTerm2 when it is installed, else Terminal.app."""
    return 'iTerm' if exists('/Applications/iTerm.app') else 'Terminal'
