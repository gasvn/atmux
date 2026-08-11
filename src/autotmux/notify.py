"""Outbound reminders for jobs approaching their time limit.

The daemon runs on the login node, so it keeps watching after the dashboard is
closed -- which is the only time a "your allocation ends soon" reminder is
worth anything.  Delivery is a single POST of ``{"text": ...}``, the shape
Slack incoming webhooks accept and that Discord, Teams, ntfy and the usual
relay services all take too, so no service-specific client is needed.

Everything here is best-effort: a webhook that is slow, wrong, or unreachable
must never delay or break the poll loop that drives the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import keepalive

_NODE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

log = logging.getLogger('autotmux_daemon')

# A reminder is only useful once, and only while there is still time to act on
# it.  Re-announcing the same job every poll would train the user to ignore it.
_MAX_TEXT = 2000


def format_remaining(seconds: float) -> str:
    """Human-readable remaining time, e.g. ``1h 5m``."""
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f'{hours}h {minutes}m'
    if hours:
        return f'{hours}h'
    return f'{minutes}m'


def build_message(job: dict, remaining: float) -> str:
    """A one-line reminder naming the job, its node, and the time left."""
    job_id = str(job.get('job_id') or '?')
    name = str(job.get('job_name') or '?')
    node = str(job.get('node') or '')
    where = f' on {node}' if node else ''
    return (f'AutoTmux: Slurm job {name} ({job_id}){where} ends in '
            f'{format_remaining(remaining)}.')[:_MAX_TEXT]


def build_start_message(job: dict) -> str:
    """A job you were waiting on is now running somewhere."""
    job_id = str(job.get('job_id') or '?')
    name = str(job.get('job_name') or '?')
    node = str(job.get('node') or '')
    where = f' on {node}' if node else ''
    return (f'AutoTmux: Slurm job {name} ({job_id}) is now '
            f'running{where}.')[:_MAX_TEXT]


def started_jobs(jobs, seen) -> list[dict]:
    """Jobs that are running now and were not running the last time we looked.

    A job holds no node until it starts, so "appeared in the allocated set" is
    the transition worth announcing.  ``seen`` is what the caller already knew
    about; on a daemon's first complete poll it is seeded rather than compared,
    or a restart would announce every job that was already running.
    """
    fresh = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get('job_id') or '').strip()
        if not job_id or job_id in seen:
            continue
        state = str(job.get('state') or '').strip().upper()
        if state and state != 'RUNNING':
            continue
        fresh.append(job)
    return fresh


def due_jobs(jobs, lead_time: float, already_sent) -> list[dict]:
    """Jobs inside the reminder window that have not been announced yet.

    A job with no parseable time left is skipped rather than announced: an
    unknown remaining time is not evidence that a job is ending, and guessing
    would produce a false alarm every poll.
    """
    due = []
    seen = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get('job_id') or '').strip()
        # One job can span several nodes; announce it once, not per node.
        if not job_id or job_id in already_sent or job_id in seen:
            continue
        seen.add(job_id)
        state = str(job.get('state') or '').strip().upper()
        if state and state != 'RUNNING':
            continue
        remaining = keepalive.parse_time_left(job.get('time'))
        if remaining is None or remaining == math.inf:
            continue
        if 0 <= remaining <= lead_time:
            due.append({**job, 'remaining': remaining})
    return due


def _applescript_quote(value: str) -> str:
    """Escape a Python string into an AppleScript string literal."""
    return value.replace('\\', '\\\\').replace('"', '\\"')


def local_notify_argv(title: str, text: str) -> list[str] | None:
    """The desktop-notification command for this platform, or ``None``.

    The text reaches these tools as argv, never a shell string, so a session or
    job name can never be executed.  Newlines are folded out because both
    backends treat the body as a single line anyway.
    """
    title = ' '.join(str(title).split())[:120]
    text = ' '.join(str(text).split())[:_MAX_TEXT]
    if not text:
        return None
    if sys.platform == 'darwin':
        script = (f'display notification "{_applescript_quote(text)}" '
                  f'with title "{_applescript_quote(title)}"')
        return ['osascript', '-e', script]
    if sys.platform.startswith('linux'):
        return ['notify-send', title, text]
    return None


def local_notify(title: str, text: str, timeout: float = 5.0) -> bool:
    """Best-effort desktop notification on the machine running the TUI.

    A laptop that is asleep or lacks a notification daemon simply gets nothing;
    this must never raise into the refresh path that draws the dashboard.
    """
    argv = local_notify_argv(title, text)
    if not argv:
        return False
    try:
        result = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def jobs_from_state(state: dict) -> list[dict]:
    """Collect one job record per JobID from a dashboard state document.

    The gateway client already receives every field the reminder needs, so it
    can warn locally without a Slurm query of its own.
    """
    jobs: dict[str, dict] = {}
    nodes = state.get('nodes') if isinstance(state, dict) else None
    if not isinstance(nodes, dict):
        return []
    for node, info in nodes.items():
        if not isinstance(info, dict):
            continue
        detail = info.get('info')
        if not isinstance(detail, dict):
            continue
        job_id = str(detail.get('job_id') or '').strip()
        if not job_id or job_id == '-' or job_id in jobs:
            continue
        jobs[job_id] = {
            'job_id': job_id,
            'job_name': detail.get('job_name') or '',
            'state': detail.get('state') or '',
            'time': detail.get('time') or '',
            'node': node,
        }
    return list(jobs.values())


CLAIM_TTL = 7 * 24 * 3600
_CLAIM_SWEEP_LIMIT = 4096
_CLAIM_LABEL_RE = re.compile(r'[^A-Za-z0-9_.-]+')


def _claim_name(key: str) -> str:
    """A filename for an arbitrary claim key.

    Keys carry node and session names, which the user chooses and which may
    hold ``/`` or ``..``, so the readable part is only a label: identity comes
    from a digest of the original key, and two distinct keys can never land on
    one file.
    """
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    label = _CLAIM_LABEL_RE.sub('-', key).strip('-')[:48]
    return f'{label}.{digest}' if label else digest


def _claim_age(path: str, wall: float) -> float | None:
    """Seconds since a claim was taken, or None if that cannot be read."""
    try:
        with open(path, encoding='utf-8') as handle:
            stamp = float(handle.read(64).strip())
    except (OSError, ValueError):
        # Empty because a write was cut short, or written by an older
        # release. The NFS server's mtime is one clock for every login node,
        # which beats treating the claim as ageless and never expiring it.
        try:
            stamp = os.stat(path).st_mtime
        except OSError:
            return None
    # A stamp from the future -- one login node's clock running ahead of
    # another's -- means "just taken", not "aged out". Never let it read as a
    # negative age, which would compare as younger than any TTL and pin the
    # claim until the clocks agreed again.
    return max(0.0, wall - stamp)


def _take_claim(path: str, wall: float) -> bool:
    """Create the claim, or report that another daemon got there first."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        # Full precision, not a rounded string: %.3f rounds *up*, so a claim
        # taken at t could be stamped t+0.0005 and read back as negative age.
        os.write(fd, f'{wall!r}\n'.encode('ascii'))
    except OSError:
        # The claim is ours either way -- the file exists, and its mtime
        # still dates it well enough to expire.
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _sweep_claims(directory: str, wall: float) -> None:
    """Drop claims past the longest TTL any caller uses.

    Always ``CLAIM_TTL``, never the caller's: the record used to be one JSON
    file pruned with whichever TTL happened to be passed, so an idle notice
    (1 h) silently evicted the job-expiry claims that were meant to last a
    week. Per-key files make each TTL its own business; this only stops a
    long-lived home accumulating every session name the user has ever used.
    """
    try:
        names = os.listdir(directory)[:_CLAIM_SWEEP_LIMIT]
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        age = _claim_age(path, wall)
        if age is not None and age >= CLAIM_TTL:
            try:
                os.unlink(path)
            except OSError:
                pass


def claim_job(directory: str, job_id: str, *, ttl: float = CLAIM_TTL,
              now: float | None = None) -> bool:
    """Whether *this* daemon is the one that should announce ``job_id``.

    Runtime state is node-local, but a cluster runs one daemon per login node
    against the same ``squeue`` -- so every one of them reaches the same
    conclusion at the same moment and, without a shared record, the user gets
    one message per login node.

    One file per claim, taken with ``O_CREAT|O_EXCL``. The previous design
    held an ``flock`` around a single shared JSON record, and on FASRC's
    NFSv3 home that lock returned ENOLCK whenever four daemons contended for
    it -- every one of them then took the fail-open path below and posted, so
    a single quiet session produced four identical Slack messages within the
    same second. ``O_EXCL`` creation does not go through the NFS lock manager
    at all; raced from four login nodes at once it yielded exactly one winner,
    eight rounds out of eight.

    Still fails open on an unusable directory: a duplicate message is a far
    smaller harm than a silent one. That is only sound because the failure is
    now genuinely rare rather than the normal path.
    """
    job_id = str(job_id)
    wall = time.time() if now is None else float(now)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as error:
        log.warning(f'reminder claim unavailable ({error}); sending anyway')
        return True
    path = os.path.join(directory, _claim_name(job_id))
    try:
        if _take_claim(path, wall):
            _sweep_claims(directory, wall)
            return True
        age = _claim_age(path, wall)
        if age is None or age < ttl:
            return False
        # Aged out. Two daemons may both unlink here, but only one O_EXCL
        # create can succeed afterwards, so the notice still goes out once.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return _take_claim(path, wall)
    except OSError as error:
        log.warning(f'reminder claim failed ({error}); sending anyway')
        return True


def idle_sessions(node: str, info: dict, threshold: float) -> list[dict]:
    """Sessions on one node that have gone quiet past ``threshold``.

    A pane that stops changing is the observable end of a run: the work
    finished, or it wedged. Only compute nodes are considered -- a shell left
    open on a login node or the laptop is idle by design and would be pure
    noise.
    """
    if threshold <= 0 or not isinstance(info, dict):
        return []
    job_id = str(info.get('job_id') or '').strip()
    if not job_id or job_id == '-':
        return []
    found = []
    for entry in info.get('sessions') or ():
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        name = str(entry[0])
        idle = entry[2]
        if (not name or isinstance(idle, bool)
                or not isinstance(idle, (int, float))
                or not math.isfinite(idle) or idle < threshold):
            continue
        found.append({
            'node': node, 'session': name, 'idle': int(idle),
            'job_id': job_id, 'job_name': info.get('job_name') or '',
        })
    return found


URL_SCHEME = 'atmux'
_URL_PREFIX = f'{URL_SCHEME}://attach/'
# Deliberately narrower than a tmux session name can be. This value arrives
# from a chat message, so anything outside a conservative set is refused rather
# than escaped -- there is no legitimate session whose name needs more.
_URL_SESSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._@:+-]{0,127}$')


def attach_url(node: str, session: str) -> str:
    """A link that opens this session, or "" if it cannot be expressed safely."""
    node, session = str(node), str(session)
    if not _NODE_RE.fullmatch(node) or not _URL_SESSION_RE.fullmatch(session):
        return ''
    return (_URL_PREFIX + urllib.parse.quote(node, safe='')
            + '/' + urllib.parse.quote(session, safe=''))


def web_attach_url(base: str, node: str, session: str) -> str:
    """The same session in the browser client, or "" if it cannot be built.

    Nothing on a phone resolves ``atmux://``, so a notice read where notices
    are actually read -- away from the machine that has the handler installed
    -- has a dead link on it. This is the one it can follow.

    `base` comes from the user's own config; node and session come off the
    cluster, so they go through the same refusal as attach_url and are then
    percent-encoded. The result is a query value, never a path segment: a
    session named `../..` must not be able to climb out of /console/.
    """
    base = str(base).strip()
    if not base.startswith(('http://', 'https://')):
        return ''
    node, session = str(node), str(session)
    if not _NODE_RE.fullmatch(node) or not _URL_SESSION_RE.fullmatch(session):
        return ''
    target = urllib.parse.quote(f'{node}:{session}', safe='')
    return base.rstrip('/') + '/console/?attach=' + target


def parse_attach_url(url: str) -> tuple[str, str] | None:
    """Validate an ``atmux://attach/<node>/<session>`` link.

    Whatever produced the link is untrusted -- anyone who can post to the
    channel can craft one -- so this refuses anything that is not exactly a
    plain node and session, and the caller passes them on as argv, never
    through a shell.
    """
    if not isinstance(url, str) or not url.startswith(_URL_PREFIX):
        return None
    rest = url[len(_URL_PREFIX):]
    if '?' in rest or '#' in rest:
        rest = re.split(r'[?#]', rest, 1)[0]
    parts = rest.split('/')
    if len(parts) != 2:
        return None
    try:
        node = urllib.parse.unquote(parts[0])
        session = urllib.parse.unquote(parts[1])
    except (ValueError, UnicodeError):
        return None
    if not _NODE_RE.fullmatch(node) or not _URL_SESSION_RE.fullmatch(session):
        return None
    return node, session


# CSI sequences, OSC strings (which tmux emits for titles), and lone two-byte
# escapes. A captured pane is full of these and none of them mean anything in
# a chat message.
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[@-Z\\-_]')
_CONTROL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')

TAIL_LIMIT = 120

# Characters a full-screen program draws its furniture from. Prose does not
# use them; status bars, separators and progress meters are made of them.
_CHROME_GLYPHS = frozenset('│┃║╎▏▎▍▌▋▊▉█░▒▓⏵⏸✓✗✻❯➜►▪·×•')
_RULE_CHARS = frozenset('─━═╌┈-_=~')


def _looks_like_rule(line: str) -> bool:
    """A horizontal separator, whatever words a program centres inside it."""
    squeezed = line.replace(' ', '')
    if len(squeezed) < 8:
        return False
    ruled = sum(1 for ch in squeezed if ch in _RULE_CHARS)
    return ruled / len(squeezed) >= 0.6


def _strip_trailing_chrome(lines: list[str]) -> list[str]:
    """Drop a status-bar block sitting below the pane's last separator.

    A full-screen program keeps its furniture pinned to the bottom, so the last
    line of the screen says what the *program* is rather than what it has been
    doing -- for a Claude Code session, always ``auto mode on``.  The furniture
    is identifiable by position: it hangs below the last horizontal rule and is
    built out of box glyphs rather than words.

    Both conditions are required.  A block below a rule that reads like output
    -- the rows under a table header, say -- has no chrome glyphs and is kept,
    because discarding a real last line is worse than quoting a status bar.
    """
    rules = [i for i, line in enumerate(lines) if _looks_like_rule(line)]
    if not rules:
        return lines
    cut = rules[-1]
    below = [line for line in lines[cut + 1:]
             if line and any(ch.isalnum() for ch in line)]
    if len(below) < 2:
        return lines
    furniture = sum(1 for line in below
                    if any(ch in _CHROME_GLYPHS for ch in line))
    return lines[:cut] if furniture / len(below) >= 0.6 else lines


def last_output_line(content, limit: int = TAIL_LIMIT) -> str:
    """The last line of a pane that actually says something.

    "Session X has been quiet for 15m" tells you to go and look; the line it
    stopped on usually tells you whether you need to.  ``Epoch 40/40 done`` and
    ``CUDA out of memory`` call for very different responses.

    Everything a terminal puts on screen that is not text -- colour, cursor
    moves, title sequences -- is removed rather than escaped, and the result is
    capped, because it is going into a chat message.

    This reads a screen, not a log, so it answers well for the batch jobs the
    idle notice exists for and poorly for a full-screen program, whose bottom
    line is a status bar no matter what it has been doing.  There is no fixing
    that from here: for a TUI the last line genuinely is the status bar.
    """
    if not isinstance(content, str) or not content:
        return ''
    lines = [' '.join(_CONTROL_RE.sub('', _ANSI_RE.sub('', raw)).split())
             for raw in content.splitlines()[-200:]]
    for line in reversed(_strip_trailing_chrome(lines)):
        # Require a letter or a digit somewhere, and reject separators even
        # when a program has centred a word inside one. Rules, borders,
        # spinners and bare prompts are the last thing on screen often enough
        # to be worth stepping over, and none of them say anything.
        if line and any(ch.isalnum() for ch in line) and not _looks_like_rule(line):
            limit = max(8, int(limit))
            return line if len(line) <= limit else line[:limit - 1] + '…'
    return ''


def build_idle_message(entry: dict, *, link: bool = False,
                       web: str = '') -> str:
    """Say what stopped and for how long, and why that is worth a look.

    ``link`` appends a Slack-formatted ``atmux://`` link. It is opt-in because
    the scheme only resolves on a machine where the handler is installed, and
    a dead link is worse than none.
    """
    session = str(entry.get('session') or '?')
    node = str(entry.get('node') or '?')
    quiet = format_remaining(entry.get('idle') or 0)
    job = str(entry.get('job_name') or '').strip()
    job_part = f' (job {job})' if job else ''
    text = (f'AutoTmux: tmux session {session} on {node}{job_part} has shown '
            f'no output for {quiet} — it has probably finished or stalled.')
    tail = str(entry.get('tail') or '').strip()
    if tail:
        text += f'\nLast line: {tail}'
    if link:
        url = attach_url(node, session)
        if url:
            text += f'  <{url}|Attach>'
    # Offered alongside rather than instead: the scheme link is the better one
    # on the machine that has the handler, and the browser link is the only one
    # that works anywhere else. Which device is reading cannot be told from
    # here -- and cannot reliably be told from a User-Agent either, because
    # iPadOS reports itself as MacIntel -- so both are shown and the reader,
    # who does know, picks.
    if web:
        url = web_attach_url(web, node, session)
        if url:
            text += f'  <{url}|Browser>'
    return text[:_MAX_TEXT]


def release_claim(directory: str, key: str) -> None:
    """Give a claim back after a failed send.

    The claim has to be taken before posting, or two daemons both post while
    each waits for the other's write. But holding it after a failure would
    silence the notice until the TTL expires -- so hand it back and let the
    next poll, on this host or another, try again.
    """
    try:
        os.unlink(os.path.join(directory, _claim_name(str(key))))
    except OSError:
        pass


def post(url: str, text: str, timeout: float) -> tuple[bool, str]:
    """POST one reminder.  Returns ``(delivered, error)``; never raises."""
    body = json.dumps({'text': text}).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = getattr(response, 'status', None) or response.getcode()
            if 200 <= int(code) < 300:
                return True, ''
            return False, f'webhook returned HTTP {code}'
    except urllib.error.HTTPError as error:
        return False, f'webhook returned HTTP {error.code}'
    except Exception as error:                      # URLError, timeout, TLS…
        return False, str(error)[:200]
