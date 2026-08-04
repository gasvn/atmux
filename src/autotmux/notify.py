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
