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

import json
import logging
import math
import subprocess
import sys
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
