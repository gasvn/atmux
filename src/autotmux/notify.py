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

import errno
import fcntl
import json
import logging
import math
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import keepalive

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
_CLAIM_LIMIT = 4096


def claim_job(path: str, job_id: str, *, ttl: float = CLAIM_TTL,
              now: float | None = None) -> bool:
    """Whether *this* daemon is the one that should announce ``job_id``.

    Runtime state is node-local, but a cluster runs one daemon per login node
    against the same ``squeue`` -- so every one of them reaches the same
    conclusion at the same moment and, without a shared record, the user gets
    one message per login node. The record lives on shared home beside the
    other config, guarded by the same advisory lock the keep-alive registry
    uses.

    Fails open: if the record cannot be read or written, the reminder is still
    sent. A duplicate message is a far smaller harm than a silent one.
    """
    job_id = str(job_id)
    wall = time.time() if now is None else float(now)
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        lock_fd = os.open(f'{path}.lock', os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        log.warning(f'reminder claim unavailable ({error}); sending anyway')
        return True
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(path, encoding='utf-8') as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict):
            record = {}
        # Drop expired entries so a long-lived home does not accumulate every
        # JobID the user has ever run.
        fresh = {
            key: value for key, value in list(record.items())[:_CLAIM_LIMIT]
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            and wall - float(value) < ttl
        }
        if job_id in fresh:
            return False
        fresh[job_id] = wall
        tmp = f'{path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(fresh, handle)
            os.replace(tmp, path)
        except OSError as error:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            log.warning(f'could not record reminder claim ({error}); '
                        'sending anyway')
        return True
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EAGAIN):
            log.warning(f'reminder claim failed ({error}); sending anyway')
        return True
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


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


def build_idle_message(entry: dict) -> str:
    """Say what stopped and for how long, and why that is worth a look."""
    session = str(entry.get('session') or '?')
    node = str(entry.get('node') or '?')
    quiet = format_remaining(entry.get('idle') or 0)
    job = str(entry.get('job_name') or '').strip()
    job_part = f' (job {job})' if job else ''
    return (f'AutoTmux: tmux session {session} on {node}{job_part} has shown '
            f'no output for {quiet} — it has probably finished or '
            f'stalled.')[:_MAX_TEXT]


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
