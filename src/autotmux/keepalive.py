"""Keep-alive auto-renew for SLURM jobs.

When the user marks a job "keep-alive" (one keystroke in the TUI), atmux records
its launch script (auto-detected via `scontrol show job`) in a small registry.
The daemon then keeps exactly one job of that name alive: when TIME_LEFT drops
below `lead_time` (or the job vanishes) it resubmits the script with `sbatch`.

This module holds the pure decision logic (easily testable) plus the stateful
`KeepAliveManager` the daemon drives once per squeue poll. See
docs/superpowers/specs/2026-07-22-keepalive-autorenew-design.md.
"""
import json
import logging
import math
import os
import re
import shlex
import subprocess
import threading
import time

log = logging.getLogger('autotmux_daemon.keepalive')


# ── parsing helpers (pure) ───────────────────────────────────────────────────

def parse_time_left(s):
    """Parse a SLURM TIME_LEFT (%L) string into seconds.

    Formats: 'D-HH:MM:SS', 'HH:MM:SS', 'MM:SS', 'SS'. 'UNLIMITED'/'INFINITE'
    -> math.inf (a job that never expires). Empty / 'NOT_SET' / 'INVALID' /
    'N/A' -> None (undecidable — callers treat it conservatively)."""
    s = (s or '').strip()
    if not s:
        return None
    up = s.upper()
    if up in ('UNLIMITED', 'INFINITE'):
        return math.inf
    if up in ('NOT_SET', 'INVALID', 'N/A', 'NONE'):
        return None
    days = 0
    if '-' in s:
        d, s = s.split('-', 1)
        try:
            days = int(d)
        except ValueError:
            return None
    try:
        parts = [int(p) for p in s.split(':')]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, sec = parts
    elif len(parts) == 2:
        h, m, sec = 0, parts[0], parts[1]
    elif len(parts) == 1:
        h, m, sec = 0, 0, parts[0]
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + sec


def parse_scontrol(text: str) -> dict:
    """Extract the launch-script fields from `scontrol show job <id>` output.

    Returns {'job_name', 'command', 'workdir', 'batch'}. `command`/`workdir` may
    contain spaces, so they're read to end-of-line; `job_name`/`batch` are single
    tokens. `batch` is True only for BatchFlag=1 (a resubmittable batch job)."""
    res = {'job_name': None, 'command': None, 'workdir': None, 'batch': False}

    def tok(key):
        m = re.search(rf'\b{key}=(\S+)', text)
        return m.group(1) if m else None

    res['job_name'] = tok('JobName')
    res['batch'] = (tok('BatchFlag') == '1')
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('Command='):
            res['command'] = line[len('Command='):].strip()
        elif line.startswith('WorkDir='):
            res['workdir'] = line[len('WorkDir='):].strip()
    return res


def _is_fresh(job: dict, lead_time: float) -> bool:
    """A matching job counts as a live/queued replacement (no renew needed) if
    it's PENDING, unexpiring, or still has more than `lead_time` seconds left.
    Unknown TIME_LEFT (None) on a running job is treated as fresh — we never
    fire a submit on data we can't read."""
    if job.get('state') == 'PENDING':
        return True
    tl = job.get('time_left')
    if tl is None:
        return True
    return tl > lead_time


def decide(matching, runtime, now, cfg):
    """Pure renewal decision for one entry.

    matching: list of {state, time_left(seconds|None|inf)} for jobs of this name.
    runtime:  {attempts, last_submit(epoch|None), in_flight(bool)}.
    Returns (action, display_state):
      action in {'none','submit','wait','paused'};
      display_state in {'healthy','renewing','paused'}.
    """
    if runtime.get('attempts', 0) >= cfg['max_failures']:
        return ('paused', 'paused')
    if runtime.get('in_flight'):
        return ('none', 'renewing')
    if any(_is_fresh(j, cfg['lead_time']) for j in matching):
        return ('none', 'healthy')
    last = runtime.get('last_submit')
    if last is not None and (now - last) < cfg['cooldown']:
        return ('wait', 'renewing')
    return ('submit', 'renewing')


# ── registry I/O (shared by TUI writer and daemon reader) ────────────────────

def load_registry(path: str) -> list:
    """Read the intent registry. Returns a list of entry dicts (possibly empty).
    Never raises."""
    try:
        with open(path) as f:
            data = json.load(f)
        entries = data.get('entries', [])
        return entries if isinstance(entries, list) else []
    except Exception:
        return []


def save_registry(path: str, entries: list) -> None:
    """Atomically write the intent registry (create parent dir as needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}'
    with open(tmp, 'w') as f:
        json.dump({'entries': entries}, f, indent=2)
    os.replace(tmp, path)


def toggle_entry(path: str, job_name: str, command: str, workdir: str) -> bool:
    """Toggle keep-alive for `job_name`. Toggles on *enabled* state (not mere
    presence): an already-enabled entry is removed; an absent OR disabled entry
    is (re)added enabled. Returns the new state (True = on, False = off). This
    keeps the readers (which filter on `enabled`) and the writer in agreement."""
    entries = [e for e in load_registry(path) if isinstance(e, dict)]
    existing = next((e for e in entries if e.get('job_name') == job_name), None)
    if existing and existing.get('enabled'):
        entries = [e for e in entries if e.get('job_name') != job_name]
        save_registry(path, entries)
        return False
    # Absent or disabled → (re)add as enabled, replacing any stale copy.
    entries = [e for e in entries if e.get('job_name') != job_name]
    entries.append({'job_name': job_name, 'command': command,
                    'workdir': workdir, 'enabled': True})
    save_registry(path, entries)
    return True


# ── stateful manager (daemon side) ───────────────────────────────────────────

class KeepAliveManager:
    """Drives auto-renew. The daemon calls tick() once per squeue poll and
    reads status() when writing the state file. Submits run on short-lived
    daemon threads so a slow sbatch never stalls the squeue loop."""

    def __init__(self, path: str, cfg: dict, submit_fn=None):
        self.path = path
        self.cfg = cfg
        # Injectable for tests; defaults to a real sbatch call.
        self._submit_fn = submit_fn or self._sbatch
        self._lock = threading.Lock()
        self._runtime = {}          # job_name -> {attempts,last_submit,in_flight,state,last_error}
        self._mtime = None
        self._entries = {}          # job_name -> entry dict (enabled only)

    # -- registry loading --
    def _reload(self):
        # (mtime, size): size catches a same-second rewrite that a coarse
        # filesystem clock would hide behind an unchanged mtime.
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = None
        if sig != self._mtime:
            self._mtime = sig
            entries = load_registry(self.path)
            self._entries = {e['job_name']: e for e in entries
                             if isinstance(e, dict) and e.get('enabled')
                             and e.get('job_name')}
            # Drop runtime for entries that are gone / disabled.
            for name in list(self._runtime):
                if name not in self._entries:
                    self._runtime.pop(name, None)

    def _rt(self, name):
        return self._runtime.setdefault(
            name, {'attempts': 0, 'last_submit': None, 'in_flight': False,
                   'state': 'healthy', 'last_error': '', 'submitted_id': None})

    def poll_needed(self) -> bool:
        """True if the feature is on and at least one entry is enabled. Lets the
        daemon skip the squeue query entirely when keep-alive is unused."""
        if not self.cfg.get('enabled', True):
            return False
        with self._lock:
            self._reload()
            return bool(self._entries)

    # -- main entry point --
    def tick(self, job_rows, now=None):
        """job_rows: list of {'id','name','state','time_left'(raw %L string)}.
        Group by name and id, decide per registered entry, act."""
        if not self.cfg.get('enabled', True):
            with self._lock:
                self._runtime.clear()
            return
        if now is None:
            now = time.time()
        by_name, by_id = {}, {}
        for j in job_rows:
            rec = {'id': j.get('id'),
                   'state': j.get('state'),
                   'time_left': parse_time_left(j.get('time_left'))}
            by_name.setdefault(j.get('name'), []).append(rec)
            if rec['id']:
                by_id[rec['id']] = rec
        with self._lock:
            self._reload()
            for name, entry in self._entries.items():
                rt = self._rt(name)
                matching = list(by_name.get(name, []))
                # Also count the job WE submitted (tracked by id) as ours, even
                # if it came up under a different name — e.g. --job-name was set
                # on the sbatch CLI, not in the script. Without this, a healthy
                # renew loop can't recognise its own replacement and would keep
                # resubmitting (duplicate allocations) then falsely PAUSE.
                sid = rt.get('submitted_id')
                if sid and sid in by_id and by_id[sid] not in matching:
                    matching.append(by_id[sid])
                action, disp = decide(matching, rt, now, self.cfg)
                rt['state'] = disp
                if disp == 'healthy':
                    rt['attempts'] = 0
                    rt['last_error'] = ''
                    # NB: keep last_submit — clearing it would drop the cooldown
                    # floor, so a job that briefly flaps fresh→gone could fire a
                    # second sbatch within one poll. A stale last_submit is
                    # harmless: at the next real expiry it's long past cooldown.
                if action == 'submit' and not rt['in_flight']:
                    rt['in_flight'] = True
                    rt['last_submit'] = now
                    t = threading.Thread(target=self._run_submit,
                                         args=(name, dict(entry)), daemon=True)
                    t.start()

    def _run_submit(self, name, entry):
        ok, err, new_id = False, 'submit crashed', None
        try:
            result = self._submit_fn(entry.get('command', ''),
                                     entry.get('workdir') or None)
            if isinstance(result, tuple) and len(result) == 3:
                ok, err, new_id = result
            else:                         # 2-tuple submit_fn (older/test stubs)
                ok, err = result
        except Exception as e:            # never let in_flight latch True forever
            err = f'submit crashed: {e}'
        with self._lock:
            # If the user toggled this entry OFF while the submit was in flight,
            # _reload() already dropped its runtime — don't resurrect it. But we
            # must still clear in_flight if it's present, or that entry would
            # never renew again.
            if name not in self._runtime:
                return
            rt = self._runtime[name]
            rt['in_flight'] = False
            if name not in self._entries:
                return
            if ok:
                # Success = progress. Remember the id we just queued so we can
                # recognise our own replacement regardless of its name, and
                # reset the failure counter (attempts counts FAILED submits).
                rt['attempts'] = 0
                rt['last_error'] = ''
                if new_id:
                    rt['submitted_id'] = new_id
            else:
                rt['attempts'] = rt.get('attempts', 0) + 1
                rt['last_error'] = (err or 'sbatch failed')[:200]
                if rt['attempts'] >= self.cfg['max_failures']:
                    rt['state'] = 'paused'
            log.info(f"keep-alive submit for {name!r}: "
                     f"{'ok id=' + str(new_id) if ok else 'FAILED: ' + rt['last_error']} "
                     f"(failures {rt['attempts']})")

    def _sbatch(self, command, workdir):
        """Resubmit a batch script. Returns (ok, error_text, new_job_id)."""
        try:
            argv = shlex.split(command or '')   # can raise ValueError on bad quoting
            if not argv:
                return (False, 'empty command', None)
            r = subprocess.run(['sbatch', *argv], cwd=workdir or None,
                               capture_output=True, text=True,
                               timeout=self.cfg['submit_timeout'])
        except subprocess.TimeoutExpired:
            return (False, 'sbatch timed out', None)
        except Exception as e:
            return (False, f'sbatch error: {e}', None)
        if r.returncode == 0 and 'Submitted batch job' in (r.stdout or ''):
            m = re.search(r'Submitted batch job (\d+)', r.stdout)
            return (True, '', m.group(1) if m else None)
        return (False, (r.stderr or r.stdout or f'rc={r.returncode}').strip(), None)

    def status(self) -> dict:
        """Snapshot of per-entry renewal state for the frontend."""
        with self._lock:
            return {
                name: {
                    'state': rt.get('state', 'healthy'),
                    'attempts': rt.get('attempts', 0),
                    'last_submit': rt.get('last_submit'),
                    'last_error': rt.get('last_error', ''),
                }
                for name, rt in self._runtime.items()
                if name in self._entries
            }
