"""Where the browser dashboard gets its state.

The TUI reaches the clusters through a ClusterPool and refreshes on a timer.
This is the same thing for a process that is not the TUI: one pool, one
background refresh, and a cached answer that every request shares.

Per-request fetching would be wrong twice over. Each fetch is an SSH round
trip to every gateway, so a phone polling every few seconds would open more
connections than a person ever did; and two browser tabs would each pay for
their own. The dashboard's own refresh loop has the same shape for the same
reason.
"""

from __future__ import annotations

import threading
import time

import json
import os

from . import config
from . import gateway as gateway_client
from . import keepalive
from . import model
from . import paths

# What the daemon writes when there is no cluster to reach -- a single
# machine, or a login node running its own daemon. Bounded like every other
# read of it: the file is attacker-adjacent only in the sense that a runaway
# daemon could grow it, and a dashboard that OOMs is a dashboard that is down.
_STATE_FILE_LIMIT = 8 * 1024 * 1024


def _read_local_state() -> tuple[bool, dict]:
    try:
        if os.path.getsize(paths.STATE_FILE) > _STATE_FILE_LIMIT:
            return False, {}
        with open(paths.STATE_FILE, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False, {}
    return (True, data) if isinstance(data, dict) else (False, {})

# The TUI refreshes on this cadence. Matching it means the two views of one
# cluster never disagree by more than one tick, and the clusters see the same
# load they always did.
REFRESH_SECONDS = 5.0

# Long enough that a stalled gateway does not pin the thread forever, short
# enough that a caller waiting on the first fetch gives up in human time.
FIRST_FETCH_TIMEOUT = 20.0


class StateSource:
    """A cached, periodically refreshed view of every cluster.

    Never raises at the call site: a failed refresh keeps the previous answer
    and says how old it is. A dashboard that goes blank because one gateway
    was slow is worse than one that says "12s ago".
    """

    def __init__(self, pool=None, refresh: float = REFRESH_SECONDS,
                 clock=time.monotonic) -> None:
        self._pool = pool
        self._refresh = max(1.0, float(refresh))
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict = {}
        self._fetched_at: float | None = None
        self._ok = False
        self._error = ''
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── the pool ─────────────────────────────────────────────────────────

    @staticmethod
    def build_pool():
        """The same pool the TUI builds, or None if this is not gateway mode.

        Returns None rather than raising: a single-machine install has no
        gateways and is not broken, it just has nothing remote to reach.
        """
        try:
            settings = config.load_client()
        except Exception:
            return None
        if not isinstance(settings, dict) or not settings.get('gateways'):
            return None
        try:
            return gateway_client.ClusterPool(
                config.client_clusters(settings), settings)
        except Exception:
            return None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._pool is None:
            self._pool = self.build_pool()
        self._thread = threading.Thread(
            target=self._loop, name='atmux-state', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._refresh)

    def refresh(self) -> bool:
        """Fetch once. Returns whether it produced a usable state."""
        try:
            if self._pool is not None:
                ok, state = self._pool.fetch_state()
            else:
                ok, state = _read_local_state()
        except Exception as error:
            with self._lock:
                self._error = ' '.join(str(error).split())[:200]
            return False
        if not ok or not isinstance(state, dict):
            with self._lock:
                self._error = self._error or 'no state'
            return False
        with self._lock:
            self._state = state
            self._fetched_at = self._clock()
            self._ok = True
            self._error = ''
        return True

    # ── reading ──────────────────────────────────────────────────────────

    def age(self) -> float | None:
        with self._lock:
            if self._fetched_at is None:
                return None
            return max(0.0, self._clock() - self._fetched_at)

    def snapshot(self) -> dict:
        """Everything a client needs to draw the dashboard, once."""
        with self._lock:
            state = self._state
            error = self._error
        age = self.age()
        entries = ()
        try:
            ok, entries = keepalive._load_registry_checked(
                config.KEEPALIVE_PATH)
            if not ok:
                entries = ()
        except Exception:
            entries = ()
        return {
            'sessions': model.sessions(state, keepalive_entries=entries),
            'queue': model.queue(state),
            'updated': str(state.get('updated', '')) if state else '',
            'age': None if age is None else round(age, 1),
            'stale': age is not None and age > self._refresh * 4,
            'error': error,
        }
