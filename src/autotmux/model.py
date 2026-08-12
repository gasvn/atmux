"""The dashboard's model: daemon state in, display rows out.

Extracted from the TUI rather than copied out of it. The browser client
renders a list rather than a terminal, so it needs the same rows the table
is built from -- and two derivations of one thing is how they end up
disagreeing about which session is stale.

Deliberately free of Textual and Rich. Colour and cell rendering belong to
whichever view is drawing; what is here is what the sessions *are*.
"""

from __future__ import annotations

import math
import re

from . import config

# Sessions the daemon reports for a node it could not reach, and for one it
# has never had a shell on. Both are placeholders, not tmux sessions.
_OFFLINE_SESSION = '\x00autotmux:offline'
_START_SHELL_SESSION = '\x00autotmux:start-shell'

# Idle thresholds. Module state because the derivation is a plain function
# shared by every view; apply_idle_thresholds() adopts the configured pair
# and a failed read simply leaves these.
IDLE_HINT_SECONDS = 300
IDLE_STALE_SECONDS = 3600
_IDLE_DOT = '\u25cf'


def apply_idle_thresholds() -> None:
    """Adopt the configured idle thresholds, if the config can be read."""
    global IDLE_HINT_SECONDS, IDLE_STALE_SECONDS
    try:
        cfg = config.load_client()
        hint = int(cfg['idle_hint'])
        stale = int(cfg['idle_stale'])
    except Exception:
        return
    IDLE_HINT_SECONDS = hint
    # A "stale" tier below the hint would colour every flagged session red.
    IDLE_STALE_SECONDS = max(stale, hint)


def _attention_rank(row) -> int:
    """Where a row belongs in the table, by how much it wants a decision.

    Ordering by node name gave the top of the table to whichever host sorted
    first, which says nothing.  What earns the top is *recent change*: a
    session that has just gone quiet is a run that has probably finished or
    wedged, and that is the moment a decision is worth making.

    The rank is deliberately coarse and slow-moving -- a row changes tier at
    most twice per quiet spell -- because a table that re-sorts while the
    cursor is in it is worse than one that is merely ordered badly.  A session
    quiet for hours sinks *below* the working ones: it is not news any more,
    and leaving it on top would push live work off the screen.
    """
    session = row[1]
    status = str(row[4])
    if session == _OFFLINE_SESSION or status.startswith('DEGRADED'):
        return 0
    if session == _START_SHELL_SESSION:
        return 4                       # a placeholder, not somebody's work
    marker, _rest = _split_idle_marker(status)
    if marker:
        return 3 if _looks_stale(marker) else 1
    return 2


# The bands the ranks are drawn as. The ranks have always existed and have
# always sorted the table; nothing on screen ever said so, so the reader
# re-derived the judgement the daemon had already made by reading every row.
# A sort you cannot see is not a sort.
#
# Four titles for five ranks: the last rank is not somebody's work and leaves
# the list entirely. Only non-empty bands are drawn, so the usual screen has
# two -- few bands over many rows, which is the ratio grouping needs. (Grouping
# by node is the other way round: eight nodes over ten rows, so the headings
# would outnumber what they head.)
BANDS = (
    (0, 'not reachable'),
    (1, 'just stopped'),
    (2, 'working'),
    (3, 'quiet a while'),
)
BAND_TITLES = dict(BANDS)
# Rank 4 -- the "start a shell here" offers. Real, but not sessions, and
# mixing an offer into a list of states is what made 40% of the rows things
# you cannot attach to.
OFFER_RANK = 4


def session_rank(row) -> int:
    """How much this row wants a decision. See _attention_rank."""
    return _attention_rank(row)


def filter_rows(rows, query: str):
    """The rows a query narrows to.

    Substring rather than fuzzy, and deliberately: this narrows a list you
    are looking at, so what disappears has to be explainable by what you
    typed. A fuzzy match that keeps `tu_harness` for the query `sh` is
    correct by its own rules and looks like a bug from the outside -- the
    palette is where fuzzy belongs, because there you are searching a set you
    cannot see.

    Matched against the name and the machine together: "which of these is on
    15304" is the same question as "where is newclaw", and both are asked by
    typing part of what you remember.
    """
    query = str(query or '').strip().lower()
    if not query:
        return list(rows)
    kept = []
    for row in rows:
        hay = f'{_session_label(row[1])} {node_label(row[0])}'.lower()
        if query in hay:
            kept.append(row)
    return kept


def plan_rows(rows):
    """Lay rows out as bands, in the order they already sort in.

    Returns a flat list of ('band', title, count) and ('row', row), which is
    what the table is built from -- and, deliberately, a pure function of the
    rows, so the layout can be checked without a terminal.

    The offers get a band of their own rather than leaving the list. Folding
    them to a single line was the first attempt and it was wrong twice over:
    `s` opens a shell on the *selected* node and `k` puts the *selected*
    node's job on auto-renew, so a row you cannot select is a node you can no
    longer SSH to or keep alive. rank 4 calls them "not somebody's work",
    which is true of the session and not of the machine.

    A band gets the separation that was the point -- they stop being
    interleaved with things you can attach to -- and costs nothing.
    """
    ranked = [(session_rank(r), r) for r in rows]
    plan = []
    for rank, title in BANDS + ((OFFER_RANK, 'start a shell here'),):
        band = [r for got, r in ranked if got == rank]
        if not band:
            continue                    # an empty band is a heading over nothing
        plan.append(('band', title, len(band)))
        plan.extend(('row', r) for r in band)
    return plan


def _coerce_idle_seconds(value):
    """Accept only a real, finite, non-negative idle count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _format_idle(idle: int) -> str:
    if idle >= 86400:
        return f'{idle // 86400}d'
    if idle >= 3600:
        return f'{idle // 3600}h'
    return f'{idle // 60}m'


def _idle_marker(idle) -> str:
    """``● 12m`` once a session has been quiet past the hint threshold."""
    if not _idle_tier(idle):
        return ''
    return f'{_IDLE_DOT} {_format_idle(_coerce_idle_seconds(idle))}'


def _idle_tier(idle) -> str:
    idle = _coerce_idle_seconds(idle)
    if idle is None or idle < IDLE_HINT_SECONDS:
        return ''
    return 'stale' if idle >= IDLE_STALE_SECONDS else 'idle'


def _looks_stale(text: str) -> bool:
    """Whether a rendered marker represents the longer, redder tier."""
    unit = text[len(_IDLE_DOT):].strip().split()[0] if len(text) > 1 else ''
    if unit.endswith(('h', 'd')):
        return True
    if unit.endswith('m'):
        try:
            return int(unit[:-1]) * 60 >= IDLE_STALE_SECONDS
        except ValueError:
            return False
    return False


def _session_label(session: str) -> str:
    """Visible label for an internal row target token."""
    if session == _OFFLINE_SESSION:
        return '<offline>'
    if session == _START_SHELL_SESSION:
        # Short on purpose: the placeholder appears on every node without a
        # session, so its width sets the SESSION column for the whole table.
        return '<shell>'
    return str(session)


def _split_idle_marker(status) -> tuple[str, str]:
    """Separate a leading ``● 15m`` marker from the rest of the STATUS text."""
    text = str(status)
    if not text.startswith(_IDLE_DOT):
        return '', text
    parts = text.split(' ', 2)
    if len(parts) < 3:
        return text, ''
    return f'{parts[0]} {parts[1]}', parts[2]


def build_session_rows(state: dict) -> list:
    """Turn daemon state into a flat list of display rows.

    Each row: (node, session, wins, time_left, status, cpu, load)
    `cpu` is how many processors the machine has and `load` is its 1-min load
    average -- both node-wide, so dividing one by the other means something.
    Together they tell the user whether attaching will be snappy or sluggish.
    """
    rows = []
    if not isinstance(state, dict):
        return rows
    nodes = state.get('nodes', {})
    if not isinstance(nodes, dict):
        return rows
    for node, nd in nodes.items():
        if not isinstance(node, str) or not isinstance(nd, dict):
            continue
        info = nd.get('info', {})
        if not isinstance(info, dict):
            info = {}
        time_left = str(info.get('time', '') or '')
        cpu = str(info.get('nproc', '') or '')
        load = str(info.get('load', '') or '')
        try:
            escape_time = int(str(info.get('escape_time', '') or ''))
        except (TypeError, ValueError):
            escape_time = 0
        latency_warning = (
            f'⚠ ESC {escape_time}ms'
            if node != 'localhost' and escape_time > 50 else '')
        last_error = str(nd.get('last_error') or '')
        network_info = nd.get('network')
        network_warning = ''
        if isinstance(network_info, dict) and network_info.get('state') in {
                'suspect', 'offline', 'half-open'}:
            retry = network_info.get('retry_in', 0)
            if not isinstance(retry, (int, float)) or isinstance(retry, bool):
                retry = 0
            reason = ' '.join(str(network_info.get('reason') or '').split())[:24]
            phase = 'testing' if network_info.get('state') == 'half-open' else 'retry'
            network_warning = f'⚠ NET {phase} {max(0, int(math.ceil(retry)))}s'
            if reason:
                network_warning += f': {reason}'
        if not nd.get('alive'):
            status = f'OFFLINE: {last_error[:30]}' if last_error else 'OFFLINE'
            rows.append((node, _OFFLINE_SESSION, '-', time_left, status, cpu, load))
            continue
        sessions = nd.get('sessions', [])
        if not isinstance(sessions, (list, tuple)):
            sessions = []
        added = False
        if sessions:
            for s in sessions:
                idle = None
                if isinstance(s, (list, tuple)):
                    if not s:
                        continue
                    name = str(s[0])
                    wins = str(s[1]) if len(s) > 1 else '?'
                    if len(s) > 2:
                        idle = _coerce_idle_seconds(s[2])
                elif isinstance(s, str):
                    name, wins = s, '?'
                else:
                    continue
                status = (f'DEGRADED: {last_error[:30]}'
                          if last_error else 'Active')
                marker = _idle_marker(idle)
                if marker:
                    status = f'{marker} {status}'
                if latency_warning:
                    status = f'{status} · {latency_warning}'
                if network_warning:
                    status = f'{status} · {network_warning}'
                rows.append((node, name, wins, time_left, status, cpu, load))
                added = True
        if not added:
            status = (f'DEGRADED: {last_error[:30]}'
                      if last_error else 'No sessions')
            if network_warning:
                status = f'{status} · {network_warning}'
            rows.append((node, _START_SHELL_SESSION, '-', time_left, status, cpu, load))
    rows.sort(key=lambda r: (_attention_rank(r), r[0], _session_label(r[1])))
    return rows


# ── the same rows, as data ────────────────────────────────────────────────
# build_session_rows returns tuples in table-column order, which is the shape
# a DataTable wants and a poor shape for anything else: position carries the
# meaning, the idle marker is glued onto the front of the status string, and
# the placeholder sessions are sentinels a caller has to know about. A client
# rendering a list of rows rather than a grid of cells needs the fields.
#
# Derived from build_session_rows rather than from the state, so there is one
# answer to "which session is stale" and one sort order. A second walk over
# the state would be a second opinion.

LOGIN_NODE_PREFIX = 'login--'


def node_label(node: str) -> str:
    """Compact display name for a node.

    The daemon encodes a login host as ``login--<fqdn>``; nothing outside
    this function should ever show that form. A fully-qualified login host
    runs to ~37 characters, and the table sizes NODE to its widest value, so
    one such row pushed SESSION -- the column users navigate by -- clean off
    a narrow terminal. Routing always uses the real name; this is display.
    """
    name = str(node)
    if name.startswith(LOGIN_NODE_PREFIX):
        host = name[len(LOGIN_NODE_PREFIX):]
        return 'login:' + (host.split('.', 1)[0] or host)
    return name.split('.', 1)[0] or name


def _placeholder(name: str) -> str:
    """What this row is, when it is not a session: '' for a real one."""
    if name == _OFFLINE_SESSION:
        return 'offline'
    if name == _START_SHELL_SESSION:
        return 'empty'
    return ''


def sessions(state: dict, keepalive_entries=()) -> list[dict]:
    """The dashboard's rows, as records.

    `keepalive_entries` is the auto-renew registry, matched here by the same
    job-family rule the table uses. Passing the registry rather than a set of
    names is the point: a renewed batch job comes back with a new id under
    the same name, so a name match would claim jobs nobody armed.
    """
    out = []
    for node, name, wins, time_left, status, cpu, load in \
            build_session_rows(state):
        marker, rest = _split_idle_marker(status)
        idle_seconds = None
        if marker:
            # The marker carries the tier; the tier is what a client colours
            # by, and re-deriving it from a formatted string would be a third
            # opinion about the same number.
            idle_seconds = _idle_seconds_of(state, node, name)
        kind = _placeholder(name)
        out.append({
            # Both: `node` is what routes a command, `node_label` is what a
            # person reads. Showing the routing name is how a phone ends up
            # calling a machine login--zgx while the table beside it calls
            # the same machine login:zgx.
            'node': node,
            'node_label': node_label(node),
            'session': '' if kind else name,
            'kind': kind or 'session',
            'label': _session_label(name),
            'windows': wins,
            'left': time_left,
            'status': rest,
            # The duration alone. The dot is how a terminal shows a tier
            # when it has one colour and no shapes; a page has a coloured
            # element for that, and printing the glyph beside it says the
            # same thing twice.
            'idle_label': (_format_idle(idle_seconds)
                           if idle_seconds is not None else ''),
            'idle_seconds': idle_seconds,
            'tier': _idle_tier(idle_seconds) if idle_seconds is not None else '',
            'cpu': cpu,
            'load': load,
            'keepalive': _keepalive_field(state, node, keepalive_entries),
            'attention': _attention_rank((node, name, wins, time_left,
                                          status, cpu, load)),
        })
    return out


def _idle_seconds_of(state: dict, node: str, session: str):
    """The raw idle count behind a row's marker, or None.

    Looked up rather than parsed back out of the marker: '5m' has already
    lost the precision a client would want to sort or colour by.
    """
    try:
        entries = state['nodes'][node]['info']['sessions']
    except (KeyError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if (isinstance(entry, (list, tuple)) and entry
                and str(entry[0]) == session and len(entry) > 2):
            return _coerce_idle_seconds(entry[2])
    return None


def queue(state: dict) -> dict:
    """The Slurm queue as the daemon captured it.

    Text, not records: it is `squeue -l` output and the daemon stores it
    verbatim. Parsing it here to re-render it would be inventing a schema
    Slurm did not promise, and the columns differ between sites.
    """
    if not isinstance(state, dict):
        return {'long': '', 'pending': '', 'updated': ''}
    return {
        'long': str(state.get('squeue_long') or ''),
        'pending': str(state.get('squeue_pending') or ''),
        'updated': str(state.get('squeue_updated') or ''),
    }


# ── auto-renew ────────────────────────────────────────────────────────────
# Which rows have keep-alive armed, and what it is doing. Matched by job
# *family*, not by name: a renewed batch job comes back with a new id under
# the same name, and a name-only match would claim every job that ever
# shared it. The name path is only there for registry entries written before
# ids were recorded.

def entry_matches(entry: dict, job_id, job_name) -> bool:
    from . import keepalive
    tracked = keepalive.job_family_id(entry.get('job_id'))
    current = keepalive.job_family_id(job_id)
    if tracked is not None:
        return tracked == current
    return (isinstance(job_name, str)
            and entry.get('job_name') == job_name)


def find_entry(entries, job_id, job_name) -> dict | None:
    return next((entry for entry in (entries or ())
                 if entry_matches(entry, job_id, job_name)), None)


def status_for_entry(ka_status: dict, entry: dict) -> dict:
    from . import keepalive
    if not isinstance(ka_status, dict):
        return {}
    entry_id = entry.get('entry_id')
    if isinstance(entry_id, str) and isinstance(ka_status.get(entry_id), dict):
        return ka_status[entry_id]
    name = entry.get('job_name')
    if isinstance(name, str) and isinstance(ka_status.get(name), dict):
        return ka_status[name]
    tracked = keepalive.job_family_id(entry.get('job_id'))
    if tracked:
        for status in ka_status.values():
            if (isinstance(status, dict)
                    and keepalive.job_family_id(status.get('job_id')) == tracked):
                return status
    return {}


def keepalive_suffix(ka_state: dict) -> str:
    """Status text appended to a registered row's STATUS cell."""
    if not isinstance(ka_state, dict):
        ka_state = {}
    st = ka_state.get('state', 'healthy')
    if st == 'paused':
        n = ka_state.get('attempts', 0)
        error = str(ka_state.get('last_error') or '').strip()
        detail = f': {error[:28]}' if error else ''
        return f' · ⚠ keep-alive PAUSED ✕{n}{detail}'
    if st == 'renewing':
        return ' · ⟳ renewing…'
    return ' · ⟳ keep-alive'


def _node_jobs(state: dict) -> dict:
    nodes = state.get('nodes', {}) or {} if isinstance(state, dict) else {}
    if not isinstance(nodes, dict):
        return {}
    out = {}
    for name, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        info = nd.get('info', {}) or {}
        if isinstance(info, dict):
            out[name] = (info.get('job_id'), info.get('job_name'))
    return out


def keepalive_state(state: dict, node: str, entries) -> dict | None:
    """What auto-renew is doing for a node's job, or None if not armed."""
    if not entries or not isinstance(state, dict):
        return None
    job_id, job_name = _node_jobs(state).get(node, (None, None))
    entry = find_entry(entries, job_id, job_name)
    if entry is None:
        return None
    ka_status = state.get('keepalive', {}) or {}
    return status_for_entry(ka_status, entry) or {'state': 'healthy'}


def decorate_keepalive(rows, state: dict, entries) -> list:
    """Fold the keep-alive marker into the STATUS cell of registered rows.

    Renewal is driven from Slurm on the login node, independently of whether
    this node's SSH master is reachable, so the intent shows on offline and
    <Start Shell> rows too.
    """
    if not entries or not isinstance(state, dict):
        return list(rows)
    node_job = _node_jobs(state)
    ka_status = state.get('keepalive', {}) or {}
    out = []
    for r in rows:
        job_id, job_name = node_job.get(r[0], (None, None))
        entry = find_entry(entries, job_id, job_name)
        if entry is not None:
            suffix = keepalive_suffix(status_for_entry(ka_status, entry))
            r = (r[0], r[1], r[2], r[3], r[4] + suffix, r[5], r[6])
        out.append(r)
    return out


def _keepalive_field(state, node, entries):
    """What the client shows in the auto-renew column: '' when not armed."""
    ka = keepalive_state(state, node, entries)
    if ka is None:
        return ''
    return str(ka.get('state') or 'healthy')
