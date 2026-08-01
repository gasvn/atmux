"""Keep-alive auto-renew for SLURM jobs.

When the user marks a job "keep-alive" (one keystroke in the TUI), atmux records
its launch script (auto-detected via `scontrol show job`) in a small registry.
The daemon tracks the selected Slurm job identity: when TIME_LEFT drops below
`lead_time` (or the job vanishes) it resubmits the script with `sbatch` and
advances the registry to the replacement JobID.  Job names remain display
metadata and a compatibility fallback for registries written by older builds.

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
import socket
import subprocess
import threading
import time
import fcntl
import uuid
from contextlib import contextmanager

from autotmux import lifecycle

log = logging.getLogger('autotmux_daemon.keepalive')

_SBATCH_CAPACITY_ERROR = 'sbatch capacity exhausted by stuck commands'
_REGISTRY_FILE_LIMIT = 4 * 1024 * 1024


_JOB_ID_RE = re.compile(r'^(\d+)(?:[+_][0-9\[\],%\-]+)*$')


def job_family_id(value) -> str | None:
    """Return the stable numeric Slurm job family for a queue JobID.

    Array rows may appear as ``123_4`` or ``123_[1-8%2]`` and heterogeneous
    components as ``123+1``.  Resubmitting their batch script creates a new
    *family*, so one keep-alive intent should follow the leading numeric ID.
    Requiring the complete value to match a narrow grammar also keeps callers
    from accidentally passing option-looking text to Slurm commands.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    match = _JOB_ID_RE.fullmatch(text)
    return match.group(1) if match else None


def _entry_identity(entry: dict) -> str | None:
    """Stable manager/status key for a registry entry.

    Current entries carry a UUID so their identity survives renewal JobID
    changes.  A job-id-only form is accepted for hand-written/intermediate
    registries, while the bare job name preserves pre-JobID behaviour.
    """
    entry_id = entry.get('entry_id')
    if isinstance(entry_id, str) and entry_id:
        return entry_id
    job_id = job_family_id(entry.get('job_id'))
    if job_id:
        return f'job:{job_id}'
    name = entry.get('job_name')
    return name if isinstance(name, str) and name else None


# ── parsing helpers (pure) ───────────────────────────────────────────────────

def parse_time_left(s):
    """Parse a SLURM TIME_LEFT (%L) string into seconds.

    Formats: 'D-HH:MM:SS', 'HH:MM:SS', 'MM:SS', 'SS'. 'UNLIMITED'/'INFINITE'
    -> math.inf (a job that never expires). Empty / 'NOT_SET' / 'INVALID' /
    'N/A' -> None (undecidable — callers treat it conservatively)."""
    if not isinstance(s, str):
        return None
    s = s.strip()
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
    if min(days, h, m, sec) < 0:
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
    # A manually-started or otherwise recovered healthy replacement clears a
    # prior failure pause. Checking the cap first made PAUSED permanent until a
    # daemon restart/toggle even after the job was healthy again.
    if any(_is_fresh(j, cfg['lead_time']) for j in matching):
        return ('none', 'healthy')
    if runtime.get('attempts', 0) >= cfg['max_failures']:
        return ('paused', 'paused')
    if runtime.get('in_flight'):
        return ('none', 'renewing')
    defer_until = runtime.get('defer_until')
    if (isinstance(defer_until, (int, float))
            and not isinstance(defer_until, bool)
            and math.isfinite(defer_until) and now < defer_until):
        return ('wait', 'renewing')
    last = runtime.get('last_submit')
    if last is not None and (now - last) < cfg['cooldown']:
        return ('wait', 'renewing')
    return ('submit', 'renewing')


# ── registry I/O (shared by TUI writer and daemon reader) ────────────────────

def load_registry(path: str) -> list:
    """Read the intent registry. Returns a list of entry dicts (possibly empty).
    Never raises."""
    ok, entries = _load_registry_checked(path)
    return entries if ok else []


def _load_registry_checked(path: str) -> tuple[bool, list]:
    """Return ``(read_ok, entries)`` so the daemon can preserve a last-good
    registry across a transient NFS/read error instead of caching emptiness."""
    try:
        raw = lifecycle.read_owned_regular_file(path, _REGISTRY_FILE_LIMIT)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return False, []
        entries = data.get('entries', [])
        return (True, entries) if isinstance(entries, list) else (False, [])
    except FileNotFoundError:
        return True, []
    except Exception:
        return False, []


@contextmanager
def _registry_lock(path: str, timeout: float = 5.0):
    """Cross-process lock for registry read-modify-write, with a hard bound."""
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    fd = lifecycle.open_lock_file(path + '.lock', create=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError('timed out waiting for keepalive registry lock')
            time.sleep(0.05)
        except BaseException:
            os.close(fd)
            raise
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _save_registry_unlocked(path: str, entries: list) -> None:
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}'
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'entries': entries}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_registry(path: str, entries: list) -> None:
    """Atomically write the intent registry (create parent dir as needed)."""
    with _registry_lock(path):
        _save_registry_unlocked(path, entries)


def toggle_entry(path: str, job_name: str, command: str, workdir: str) -> bool:
    """Toggle keep-alive for `job_name`. Toggles on *enabled* state (not mere
    presence): an already-enabled entry is removed; an absent OR disabled entry
    is (re)added enabled. Returns the new state (True = on, False = off). This
    keeps the readers (which filter on `enabled`) and the writer in agreement."""
    with _registry_lock(path):
        ok, loaded = _load_registry_checked(path)
        if not ok:
            # Never turn a transient read/corruption into a valid empty registry
            # and overwrite every existing renewal intent.
            raise OSError(f'cannot safely read keep-alive registry {path!r}')
        entries = [e for e in loaded if isinstance(e, dict)]
        existing = next((e for e in entries if e.get('job_name') == job_name), None)
        if existing and existing.get('enabled'):
            entries = [e for e in entries if e.get('job_name') != job_name]
            _save_registry_unlocked(path, entries)
            return False
        # Absent or disabled → (re)add as enabled, replacing any stale copy.
        entries = [e for e in entries if e.get('job_name') != job_name]
        entries.append({'job_name': job_name, 'command': command,
                        'workdir': workdir, 'enabled': True})
        _save_registry_unlocked(path, entries)
        return True


def set_entry_enabled(path: str, job_name: str, enabled: bool,
                      command: str = '', workdir: str = '', *,
                      job_id: str | int | None = None,
                      entry_id: str | None = None) -> bool:
    """Atomically set, rather than toggle, one renewal intent.

    The TUI decides whether the keypress means ON or OFF before doing NFS I/O.
    A toggle at write time can invert that intent if another frontend changed
    the registry in between. Explicit target state also lets the UI trust the
    completed write without immediately rereading NFS and falsely reporting a
    successful operation as failed when only that verification read timed out.
    """
    if not isinstance(job_name, str) or not job_name:
        raise ValueError('job_name must be a non-empty string')
    if enabled and (not isinstance(command, str) or not command.strip()):
        raise ValueError('enabled keep-alive entry needs a command')
    normalized_job_id = job_family_id(job_id) if job_id is not None else None
    if job_id is not None and normalized_job_id is None:
        raise ValueError(f'invalid Slurm job id: {job_id!r}')
    if entry_id is not None and (not isinstance(entry_id, str) or not entry_id):
        raise ValueError('entry_id must be a non-empty string')
    with _registry_lock(path):
        ok, loaded = _load_registry_checked(path)
        if not ok:
            raise OSError(f'cannot safely read keep-alive registry {path!r}')
        valid = [e for e in loaded if isinstance(e, dict)]

        def targets(entry):
            if normalized_job_id is None:
                # Compatibility mode for callers/registries from the original
                # name-keyed implementation.
                return entry.get('job_name') == job_name
            if entry_id is not None and entry.get('entry_id') == entry_id:
                return True
            # Enabling is idempotent per concrete Slurm job.  Disabling with a
            # known UUID removes exactly that logical intent even after the
            # daemon has advanced it to a replacement JobID.
            if not enabled and entry_id is not None:
                return False
            return job_family_id(entry.get('job_id')) == normalized_job_id

        entries = [e for e in valid if not targets(e)]
        if enabled:
            new_entry = {
                'job_name': job_name,
                'command': command,
                'workdir': workdir,
                'enabled': True,
            }
            if normalized_job_id is not None:
                new_entry['job_id'] = normalized_job_id
                new_entry['entry_id'] = entry_id or uuid.uuid4().hex
            entries.append(new_entry)
        if entries != loaded:
            _save_registry_unlocked(path, entries)
    return bool(enabled)


def update_entry_after_submit(path: str, entry_id: str, job_id: str | int,
                              submitted_at: float | None = None,
                              submitted_monotonic: float | None = None,
                              clock_id: str | None = None) -> bool:
    """Persist the replacement JobID for one still-enabled current entry.

    This closes two duplicate-submit windows: daemon restarts no longer forget
    which differently-named replacement belongs to an intent, and same-name
    jobs belonging to other rows are never mistaken for it.  If the user
    toggled the entry off while ``sbatch`` was running, the UUID will be absent
    and the completion is deliberately not resurrected.
    """
    normalized = job_family_id(job_id)
    if not isinstance(entry_id, str) or not entry_id or normalized is None:
        return False
    if submitted_at is None:
        submitted_at = time.time()
    with _registry_lock(path):
        ok, loaded = _load_registry_checked(path)
        if not ok:
            raise OSError(f'cannot safely read keep-alive registry {path!r}')
        entries = [e for e in loaded if isinstance(e, dict)]
        changed = False
        for entry in entries:
            if entry.get('entry_id') != entry_id or not entry.get('enabled'):
                continue
            if (entry.get('job_id') != normalized
                    or entry.get('last_submit_at') != submitted_at
                    or (submitted_monotonic is not None
                        and entry.get('last_submit_monotonic')
                        != submitted_monotonic)):
                entry['job_id'] = normalized
                entry['last_submit_at'] = submitted_at
                if submitted_monotonic is not None:
                    entry['last_submit_monotonic'] = submitted_monotonic
                    if isinstance(clock_id, str) and clock_id:
                        entry['last_submit_clock_id'] = clock_id
                changed = True
            break
        else:
            return False
        if changed:
            _save_registry_unlocked(path, entries)
    return True


def _monotonic_clock_id() -> str:
    """Identity for a host's current monotonic clock epoch.

    Monotonic timestamps survive a process restart but are not comparable
    between login hosts (or across a reboot).  Pairing them with hostname and
    Linux boot ID lets a replacement daemon use the precise elapsed clock only
    when it is genuinely the same clock; everyone else falls back to the
    persisted wall timestamp.
    """
    return lifecycle.monotonic_clock_id()


def claim_entry_for_submit(path: str, identity: str,
                           expected_job_id: str | int | None, *,
                           owner_id: str, cooldown: float,
                           lease_seconds: float,
                           now: float | None = None) -> dict:
    """Atomically claim one registry entry before invoking ``sbatch``.

    Runtime singleton locks are node-local, while the registry normally lives
    on shared home storage. Without this per-entry lease, daemons on two login
    hosts can both decide an allocation is expiring and submit duplicate jobs.
    The returned mapping has ``token`` on success; a denied claim reports a
    bounded ``retry_after`` and reason without treating coordination as an
    sbatch failure.
    """
    if not isinstance(identity, str) or not identity:
        return {'token': None, 'retry_after': 0.0, 'reason': 'entry missing'}
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError('owner_id must be a non-empty string')
    wall_now = time.time() if now is None else float(now)
    if not math.isfinite(wall_now):
        raise ValueError('claim timestamp must be finite')
    cooldown = max(0.0, float(cooldown))
    lease_seconds = max(1.0, float(lease_seconds))
    expected = job_family_id(expected_job_id)
    token = uuid.uuid4().hex

    with _registry_lock(path):
        ok, loaded = _load_registry_checked(path)
        if not ok:
            raise OSError(f'cannot safely read keep-alive registry {path!r}')
        target = next((
            entry for entry in loaded
            if (isinstance(entry, dict) and entry.get('enabled')
                and _entry_identity(entry) == identity)
        ), None)
        if target is None:
            return {'token': None, 'retry_after': 0.0,
                    'reason': 'entry missing'}
        current = job_family_id(target.get('job_id'))
        if expected is not None and current is not None and current != expected:
            return {'token': None, 'retry_after': 0.0,
                    'reason': 'entry advanced by another daemon'}

        existing = target.get('submit_claim')
        if isinstance(existing, dict):
            expires = existing.get('expires_at')
            if (isinstance(expires, (int, float))
                    and not isinstance(expires, bool)
                    and math.isfinite(expires) and expires > wall_now):
                return {'token': None,
                        'retry_after': expires - wall_now,
                        'reason': 'renewal claimed by another daemon'}

        # A completed submit is a second cross-daemon fence. The originating
        # manager already has an exact monotonic cooldown, so let it follow that
        # local decision; other managers use the shared wall timestamp.
        last_at = target.get('last_submit_at')
        last_owner = target.get('last_submit_owner')
        if (last_owner != owner_id
                and isinstance(last_at, (int, float))
                and not isinstance(last_at, bool)
                and math.isfinite(last_at)):
            age = max(0.0, wall_now - float(last_at))
            if age < cooldown:
                return {'token': None,
                        'retry_after': cooldown - age,
                        'reason': 'renewal cooldown recorded by another daemon'}

        target['submit_claim'] = {
            'token': token,
            'owner': owner_id,
            'host': socket.gethostname(),
            'pid': os.getpid(),
            'created_at': wall_now,
            'expires_at': wall_now + lease_seconds,
        }
        _save_registry_unlocked(path, loaded)
    return {'token': token, 'retry_after': 0.0, 'reason': ''}


def finish_submit_claim(path: str, token: str, *, success: bool,
                        owner_id: str, job_id: str | int | None = None,
                        submitted_at: float | None = None,
                        submitted_monotonic: float | None = None,
                        clock_id: str | None = None,
                        record_cooldown: bool | None = None) -> bool:
    """Clear an owned submit lease and persist any ambiguous submit attempt.

    A timeout does not prove that Slurm rejected the request, so failed calls
    retain the same cross-daemon cooldown as the originating manager.  The one
    exception is an explicit local-capacity rejection, where no command was
    started; callers express that with ``record_cooldown=False``.
    """
    if (not isinstance(token, str) or not token
            or not isinstance(owner_id, str) or not owner_id):
        return False
    if record_cooldown is None:
        record_cooldown = bool(success)
    normalized = job_family_id(job_id)
    if submitted_at is None:
        submitted_at = time.time()
    with _registry_lock(path):
        ok, loaded = _load_registry_checked(path)
        if not ok:
            raise OSError(f'cannot safely read keep-alive registry {path!r}')
        target = next((
            entry for entry in loaded
            if (isinstance(entry, dict)
                and isinstance(entry.get('submit_claim'), dict)
                and entry['submit_claim'].get('token') == token
                and entry['submit_claim'].get('owner') == owner_id)
        ), None)
        if target is None:
            return False
        target.pop('submit_claim', None)
        if record_cooldown and target.get('enabled'):
            # Registries from the original name-keyed implementation have no
            # stable UUID. Advancing their JobID would silently change their
            # identity key on reload (name -> job:<id>), dropping UI/runtime
            # continuity. Current writers always provide entry_id, so only
            # those entries durably follow a replacement JobID.
            if (success and normalized is not None
                    and isinstance(target.get('entry_id'), str)
                    and target.get('entry_id')):
                target['job_id'] = normalized
            target['last_submit_at'] = float(submitted_at)
            target['last_submit_owner'] = owner_id
            if (isinstance(submitted_monotonic, (int, float))
                    and not isinstance(submitted_monotonic, bool)
                    and math.isfinite(submitted_monotonic)):
                target['last_submit_monotonic'] = float(submitted_monotonic)
                if isinstance(clock_id, str) and clock_id:
                    target['last_submit_clock_id'] = clock_id
            else:
                target.pop('last_submit_monotonic', None)
                target.pop('last_submit_clock_id', None)
        _save_registry_unlocked(path, loaded)
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
        self._runtime = {}          # stable entry identity -> runtime state
        self._mtime = None
        self._entries = {}          # stable entry identity -> enabled entry
        self._owner_id = uuid.uuid4().hex
        self._clock_id = _monotonic_clock_id()
        self._submit_slots = threading.Semaphore(4)
        # Registry persistence happens after a successful sbatch. NFS can hang
        # below Python, so allow at most one such updater and never make it hold
        # a scarce submit slot needed by other renewal intents.
        self._registry_update_slots = threading.Semaphore(1)
        # subprocess.run(timeout=...) can itself remain stuck while killing and
        # waiting for a child in uninterruptible I/O. Keep those cleanup calls
        # behind a second fixed pool so `in_flight` always clears on a hard
        # deadline and repeated polls cannot grow unbounded helper threads.
        self._command_slots = threading.Semaphore(4)
        self._command_cleanup_grace = 2.0

    # -- registry loading --
    def _reload(self):
        # (mtime, size): size catches a same-second rewrite that a coarse
        # filesystem clock would hide behind an unchanged mtime.
        try:
            st = os.lstat(self.path)
            sig = (getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9)),
                   st.st_size, st.st_ino)
        except OSError:
            sig = None
        if sig != self._mtime:
            ok, entries = _load_registry_checked(self.path)
            if not ok:
                log.warning(f'could not read keep-alive registry {self.path!r}; '
                            'retaining last-good contents and retrying')
                return
            self._mtime = sig
            valid = {}
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get('enabled'):
                    continue
                identity = _entry_identity(entry)
                if identity is not None:
                    valid[identity] = entry
            self._entries = valid
            # Drop runtime for entries that are gone / disabled.
            for identity in list(self._runtime):
                if identity not in self._entries:
                    self._runtime.pop(identity, None)

    def _rt(self, identity, entry, now):
        existing = self._runtime.get(identity)
        if existing is not None:
            return existing
        last_submit = None
        persisted_monotonic = entry.get('last_submit_monotonic')
        if (entry.get('last_submit_clock_id') == self._clock_id
                and isinstance(persisted_monotonic, (int, float))
                and not isinstance(persisted_monotonic, bool)
                and math.isfinite(persisted_monotonic)):
            elapsed = now - float(persisted_monotonic)
            if 0 <= elapsed < self.cfg['cooldown']:
                last_submit = float(persisted_monotonic)
        submitted_at = entry.get('last_submit_at')
        if (last_submit is None
                and isinstance(submitted_at, (int, float))
                and not isinstance(submitted_at, bool)
                and math.isfinite(submitted_at)):
            # Translate a persisted wall timestamp to this process's monotonic
            # clock. A future timestamp (clock correction) conservatively gets
            # a full cooldown rather than creating a duplicate allocation.
            age = max(0.0, time.time() - float(submitted_at))
            if age < self.cfg['cooldown']:
                last_submit = now - age
        runtime = {
            'attempts': 0,
            'last_submit': last_submit,
            'in_flight': False,
            'state': 'healthy',
            'last_error': '',
            'submitted_id': job_family_id(entry.get('job_id')),
            'defer_until': None,
        }
        self._runtime[identity] = runtime
        return runtime

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
            # Cooldown is elapsed-time state.  A wall-clock correction moving
            # backwards must not suppress renewals for hours or days.
            now = time.monotonic()
        by_name, by_id = {}, {}
        for j in job_rows:
            rec = {'id': j.get('id'),
                   'state': j.get('state'),
                   'time_left': parse_time_left(j.get('time_left'))}
            by_name.setdefault(j.get('name'), []).append(rec)
            family = job_family_id(rec['id'])
            if family:
                by_id.setdefault(family, []).append(rec)
        with self._lock:
            self._reload()
            for identity, entry in self._entries.items():
                name = entry.get('job_name')
                rt = self._rt(identity, entry, now)
                tracked_id = job_family_id(entry.get('job_id'))
                # Current entries are per Slurm job family.  Only old entries
                # lacking a JobID retain the name-wide compatibility behaviour.
                matching = list(
                    by_id.get(tracked_id, []) if tracked_id
                    else by_name.get(name, []))
                # Also count the job WE submitted (tracked by id) as ours, even
                # if it came up under a different name — e.g. --job-name was set
                # on the sbatch CLI, not in the script. Without this, a healthy
                # renew loop can't recognise its own replacement and would keep
                # resubmitting (duplicate allocations) then falsely PAUSE.
                sid = job_family_id(rt.get('submitted_id'))
                if sid:
                    for record in by_id.get(sid, []):
                        if record not in matching:
                            matching.append(record)
                action, disp = decide(matching, rt, now, self.cfg)
                rt['state'] = disp
                if disp == 'healthy':
                    rt['attempts'] = 0
                    rt['last_error'] = ''
                    rt['defer_until'] = None
                    # NB: keep last_submit — clearing it would drop the cooldown
                    # floor, so a job that briefly flaps fresh→gone could fire a
                    # second sbatch within one poll. A stale last_submit is
                    # harmless: at the next real expiry it's long past cooldown.
                if action == 'submit' and not rt['in_flight']:
                    if not self._submit_slots.acquire(blocking=False):
                        # A large registry must not create one potentially-hung
                        # sbatch thread per entry. Deferred entries retry next
                        # poll as one of the four fixed slots becomes free.
                        continue
                    rt['in_flight'] = True
                    previous_submit = rt.get('last_submit')
                    rt['last_submit'] = now
                    t = threading.Thread(target=self._run_submit,
                                         args=(identity, dict(entry),
                                               previous_submit, now),
                                         daemon=True)
                    try:
                        t.start()
                    except BaseException:
                        rt['in_flight'] = False
                        rt['last_submit'] = previous_submit
                        self._submit_slots.release()
                        raise

    def _run_submit(self, identity, entry, previous_submit=None,
                    attempted_submit=None):
        claim_token = None
        finalize = None
        try:
            lease_seconds = max(
                30.0,
                float(self.cfg.get('cooldown', 0)),
                float(self.cfg.get('submit_timeout', 0))
                + self._command_cleanup_grace + 5.0,
            )
            try:
                claim = claim_entry_for_submit(
                    self.path, identity, entry.get('job_id'),
                    owner_id=self._owner_id,
                    cooldown=self.cfg.get('cooldown', 0),
                    lease_seconds=lease_seconds,
                )
            except Exception as error:
                with self._lock:
                    rt = self._runtime.get(identity)
                    if rt is not None:
                        rt['in_flight'] = False
                        rt['last_submit'] = previous_submit
                        base = (attempted_submit
                                if isinstance(attempted_submit, (int, float))
                                else time.monotonic())
                        rt['defer_until'] = base + 1.0
                        rt['last_error'] = (
                            f'renewal coordination failed: {error}')[:200]
                log.warning(
                    f'could not claim keep-alive renewal for {identity!r}: '
                    f'{error}')
                return

            claim_token = claim.get('token')
            if not claim_token:
                retry_after = claim.get('retry_after', 0.0)
                if (not isinstance(retry_after, (int, float))
                        or isinstance(retry_after, bool)
                        or not math.isfinite(retry_after)):
                    retry_after = 1.0
                retry_after = max(0.1, float(retry_after))
                reason = str(claim.get('reason') or 'renewal deferred')
                with self._lock:
                    rt = self._runtime.get(identity)
                    if rt is not None:
                        rt['in_flight'] = False
                        rt['last_submit'] = previous_submit
                        base = (attempted_submit
                                if isinstance(attempted_submit, (int, float))
                                else time.monotonic())
                        rt['defer_until'] = base + retry_after
                        rt['last_error'] = reason[:200]
                log.info(f'keep-alive renewal for {identity!r} deferred: '
                         f'{reason}')
                return

            ok, err, new_id = False, 'submit crashed', None
            try:
                result = self._submit_fn(entry.get('command', ''),
                                         entry.get('workdir') or None)
                if isinstance(result, tuple) and len(result) == 3:
                    ok, err, new_id = result
                elif isinstance(result, tuple) and len(result) == 2:
                    # 2-tuple submit_fn (older/test stubs)
                    ok, err = result
                else:
                    err = 'submit returned an invalid result'
            except BaseException as e:    # never latch in_flight forever
                err = f'submit crashed: {e}'
            ok = bool(ok)
            submitted_at = time.time()
            normalized_id = job_family_id(new_id)
            record_cooldown = ok or err != _SBATCH_CAPACITY_ERROR
            finalize = {
                'success': ok,
                'owner_id': self._owner_id,
                'job_id': normalized_id,
                'submitted_at': submitted_at,
                'submitted_monotonic': attempted_submit,
                'clock_id': self._clock_id,
                'record_cooldown': record_cooldown,
            }
            with self._lock:
                rt = self._runtime.get(identity)
                if rt is None:
                    current_entry = None
                else:
                    rt['in_flight'] = False
                    rt['defer_until'] = None
                current_entry = self._entries.get(identity)
                if rt is not None and current_entry is not None and ok:
                    rt['attempts'] = 0
                    rt['last_error'] = ''
                    if normalized_id:
                        rt['submitted_id'] = normalized_id
                        current_entry['job_id'] = normalized_id
                    current_entry['last_submit_at'] = submitted_at
                    current_entry['last_submit_owner'] = self._owner_id
                    if isinstance(attempted_submit, (int, float)):
                        current_entry['last_submit_monotonic'] = attempted_submit
                        current_entry['last_submit_clock_id'] = self._clock_id
                elif rt is not None and current_entry is not None:
                    rt['last_error'] = (err or 'sbatch failed')[:200]
                    if err == _SBATCH_CAPACITY_ERROR:
                        # No command was started. Keeping the optimistic
                        # timestamp would impose a full cooldown and could let
                        # the tracked job expire before we even retry.
                        rt['last_submit'] = previous_submit
                    else:
                        rt['attempts'] = rt.get('attempts', 0) + 1
                        if rt['attempts'] >= self.cfg['max_failures']:
                            rt['state'] = 'paused'
                if rt is not None and current_entry is not None:
                    label = current_entry.get('job_name') or identity
                    log.info(f"keep-alive submit for {label!r}: "
                             f"{'ok id=' + str(new_id) if ok else 'FAILED: ' + rt['last_error']} "
                             f"(failures {rt['attempts']})")
        finally:
            self._submit_slots.release()
        if claim_token is not None and finalize is not None:
            if not self._registry_update_slots.acquire(timeout=0.5):
                log.warning(
                    f'deferred keep-alive claim completion for {identity!r}: '
                    'another registry update is still running; the lease will '
                    'continue protecting against duplicate submits')
                return
            try:
                finish_submit_claim(self.path, claim_token, **finalize)
            except Exception as error:
                log.warning(
                    f'could not complete keep-alive claim for {identity!r}: '
                    f'{error}; its lease remains active')
            finally:
                self._registry_update_slots.release()

    def _sbatch(self, command, workdir):
        """Resubmit a batch script. Returns (ok, error_text, new_job_id)."""
        try:
            argv = shlex.split(command or '')   # can raise ValueError on bad quoting
            if not argv:
                return (False, 'empty command', None)
            if not self._command_slots.acquire(blocking=False):
                return (False, _SBATCH_CAPACITY_ERROR, None)
            done = threading.Event()
            result = {}

            def run():
                try:
                    result['value'] = subprocess.run(
                        # ``Command=`` normally begins with a script path, but
                        # a path such as ``--wrap=...`` must never be parsed as
                        # an sbatch option. Slurm accepts the standard ``--``
                        # option terminator before its script operand.
                        ['sbatch', '--parsable', '--', *argv],
                        cwd=workdir or None,
                        capture_output=True, text=True,
                        timeout=self.cfg['submit_timeout'])
                except BaseException as error:
                    result['error'] = error
                finally:
                    self._command_slots.release()
                    done.set()

            try:
                threading.Thread(target=run, daemon=True,
                                 name='keepalive-sbatch-command').start()
            except BaseException:
                self._command_slots.release()
                raise
            hard_timeout = max(0.1, self.cfg['submit_timeout']) + self._command_cleanup_grace
            if not done.wait(timeout=hard_timeout):
                return (False, 'sbatch timeout cleanup is stuck', None)
            if 'error' in result:
                raise result['error']
            r = result['value']
        except subprocess.TimeoutExpired:
            return (False, 'sbatch timed out', None)
        except Exception as e:
            return (False, f'sbatch error: {e}', None)
        if r.returncode == 0:
            output = (r.stdout or '').strip()
            # --parsable emits JOBID or JOBID;CLUSTER. Keep the legacy text
            # fallback for old/test Slurm wrappers which ignore the option.
            m = re.search(r'(?m)^\s*(\d+)(?:;\S+)?\s*$', output)
            if not m:
                m = re.search(r'Submitted batch job (\d+)', output)
            if m:
                return (True, '', m.group(1))
            return (False, 'sbatch succeeded but its JobID could not be parsed', None)
        return (False, (r.stderr or r.stdout or f'rc={r.returncode}').strip(), None)

    def status(self) -> dict:
        """Snapshot of per-entry renewal state for the frontend."""
        # Registry I/O happens while this lock is held.  If an NFS stat/read is
        # wedged, status publication must degrade rather than block every daemon
        # loop that writes the shared state file.
        if not self._lock.acquire(timeout=0.1):
            return {}
        try:
            return {
                identity: {
                    'state': rt.get('state', 'healthy'),
                    'attempts': rt.get('attempts', 0),
                    'last_submit': rt.get('last_submit'),
                    'last_error': rt.get('last_error', ''),
                    'entry_id': entry.get('entry_id'),
                    'job_id': job_family_id(entry.get('job_id')),
                    'job_name': entry.get('job_name'),
                }
                for identity, rt in self._runtime.items()
                if (entry := self._entries.get(identity)) is not None
            }
        finally:
            self._lock.release()
