"""Load daemon tunables from ~/.config/autotmux/config.toml.

Returns DEFAULTS merged with file overrides. Never raises: a missing,
malformed, or unparseable file falls back to defaults (with a logged
warning). The AUTOTMUX_CONFIG env var overrides the path (used by tests).
"""
import os
import glob
import json
import logging
import math
import re
import shlex
import stat
import uuid

log = logging.getLogger('autotmux_daemon.config')

CONFIG_PATH = os.environ.get(
    'AUTOTMUX_CONFIG',
    os.path.expanduser('~/.config/autotmux/config.toml'),
)

# The keep-alive intent registry (written by the TUI, read by the daemon).
# Persistent, sits next to config.toml. Env override is for tests.
KEEPALIVE_PATH = os.environ.get(
    'AUTOTMUX_KEEPALIVE',
    os.path.expanduser('~/.config/autotmux/keepalive.json'),
)

# Which job expiry reminders have already been announced. Shared home, not the
# node-local runtime dir: one daemon runs per login node and they all reach the
# same conclusion at once, so the record has to be visible to all of them.
NOTIFY_CLAIM_PATH = os.environ.get(
    'AUTOTMUX_NOTIFY_CLAIM',
    os.path.expanduser('~/.config/autotmux/notified-jobs.json'),
)

# Gateway choices made in the TUI live separately from the hand-written
# daemon configuration.  This keeps the common local-client workflow out of
# TOML entirely while preserving config.toml as an advanced/compatible input.
CLIENT_STATE_PATH = os.environ.get(
    'AUTOTMUX_CLIENT_STATE',
    os.path.expanduser('~/.config/autotmux/connections.json'),
)
SSH_CONFIG_PATH = os.environ.get(
    'AUTOTMUX_SSH_CONFIG',
    os.path.expanduser('~/.ssh/config'),
)
_CLIENT_STATE_LIMIT = 64 * 1024
_SSH_CONFIG_FILE_LIMIT = 1024 * 1024
_SSH_CONFIG_TOTAL_LIMIT = 2 * 1024 * 1024
_SSH_CONFIG_FILE_COUNT_LIMIT = 32

# Keep-alive auto-renew tunables (the [keepalive] table).
KEEPALIVE_DEFAULTS = {
    'enabled': True,        # master switch for the whole feature
    'lead_time': 900,       # seconds before expiry to submit the replacement
    'cooldown': 600,        # seconds to suppress re-submit after a submit
    'max_failures': 3,      # consecutive failed submits before pausing an entry
    'submit_timeout': 60,   # seconds to wait for sbatch to return
}

# Reminders for jobs nearing their time limit.  Off until a webhook is set:
# the daemon must never post anywhere the user has not named.
NOTIFY_DEFAULTS = {
    'enabled': True,        # honoured only once webhook_url is set
    'webhook_url': '',      # Slack-shaped {"text": ...} endpoint
    'lead_time': 3600,      # seconds before expiry to send the reminder
    'timeout': 10,          # seconds to wait for the webhook
    # Desktop notification on whichever machine runs the TUI.  Needs no
    # endpoint, so unlike the webhook it is on by default.
    'desktop': True,
}

_NOTIFY_NUMBER_RULES = {
    'lead_time': (float, 60.0, 86_400.0),
    'timeout':   (float, 1.0, 120.0),
}

# Defaults mirror the current daemon.py constants exactly.
DEFAULTS = {
    'squeue_interval': 30,
    'health_interval': 30,
    'session_interval': 15,
    'snapshot_interval': 120,
    'connect_timeout': 8,
    'deep_probe_timeout': 8,
    'shallow_check_timeout': 8,
    'squeue_timeout': 15,
    'ctl_persist': 3600,
    'server_alive_int': 30,
    'server_alive_max': 3,
    'gone_node_threshold': 2,
    'backoff_base': 30,
    'backoff_cap': 600,
    'network_backoff_base': 2,
    'network_backoff_cap': 60,
    'warm_orphan_interval': 30,
}


# Optional local-client mode.  An empty gateway list preserves the historical
# behaviour exactly: atmux and its daemon run together on the login host.  When
# gateways are configured, the local frontend talks to the existing daemon on
# one of those hosts through ``atmux-agent`` over SSH.
CLIENT_DEFAULTS = {
    'mode': 'auto',
    'gateways': [],
    'connect_timeout': 5,
    'state_timeout': 10.0,
    'hedge_delay': 0.35,
    'sticky_ttl': 300.0,
    'backoff_base': 2.0,
    'backoff_cap': 60.0,
    'probe_interval': 60.0,
    'control_persist': 3600,
    'server_alive_int': 15,
    'server_alive_max': 3,
    'agent_command': ['atmux-agent'],
    # Reuse SSH ControlMasters owned by something else (an MFA helper that
    # keeps authenticated masters alive, for example) instead of opening our
    # own.  Empty means AutoTmux manages its own gateway masters, as before.
    # OpenSSH expands the usual tokens, so "~/.ssh/cm-2fa-%n" resolves %n to
    # the gateway alias.
    'control_path': '',
    # How long a tmux session must be quiet before the list flags it.
    'idle_hint': 300,
    'idle_stale': 3600,
}

_CLIENT_NUMBER_RULES = {
    'connect_timeout':   (int,   1,   120),
    'state_timeout':     (float, 1.0, 120.0),
    'hedge_delay':       (float, 0.0, 10.0),
    'sticky_ttl':        (float, 0.0, 86_400.0),
    'backoff_base':      (float, 0.1, 3_600.0),
    'backoff_cap':       (float, 0.1, 86_400.0),
    'probe_interval':    (float, 5.0, 86_400.0),
    'control_persist':   (int,   1,   86_400),
    'server_alive_int':  (int,   1,   3_600),
    'server_alive_max':  (int,   1,   100),
    'idle_hint':         (int,   10,  86_400),
    'idle_stale':        (int,   10,  604_800),
}

# SSH destinations are argv items, never shell fragments.  Supporting ordinary
# aliases plus user@host covers the common HPC cases; ports belong in
# ~/.ssh/config so a value can never be mistaken for an option or command.
_GATEWAY_RE = re.compile(
    r'^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._:-]*$')

# Terminal control sequences must never reach argv or a status line.
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f]')


# Type/range validation is part of the daemon's liveness contract.  Values such
# as ``squeue_interval = -1`` used to turn Event.wait() into a busy loop, while
# ``connect_timeout = nan`` could strand a freshly-spawned ssh process before
# its cleanup path ran.  The upper limits catch unit mistakes (milliseconds
# supplied as seconds) that otherwise make the daemon look permanently stuck.
_DAEMON_RULES = {
    'squeue_interval':       (float, 0.1, 86_400),
    'health_interval':       (float, 0.1, 86_400),
    'session_interval':      (float, 0.1, 86_400),
    'snapshot_interval':     (float, 0.1, 604_800),
    'connect_timeout':       (int,   1,   600),
    'deep_probe_timeout':    (float, 0.1, 600),
    'shallow_check_timeout': (float, 0.1, 600),
    'squeue_timeout':        (float, 0.1, 600),
    'ctl_persist':           (int,   1,   604_800),
    'server_alive_int':      (int,   1,   3_600),
    'server_alive_max':      (int,   1,   100),
    'gone_node_threshold':   (int,   1,   100),
    'backoff_base':          (float, 0.1, 86_400),
    'backoff_cap':           (float, 0.1, 604_800),
    'network_backoff_base':  (float, 0.1, 3_600),
    'network_backoff_cap':   (float, 0.1, 86_400),
    'warm_orphan_interval':  (float, 1.0, 86_400),
}


def _validated_number(key, value, default, rule):
    typ, minimum, maximum = rule
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value)):
        log.warning(f'ignoring invalid config value for {key!r}: {value!r}')
        return default
    if typ is int and not float(value).is_integer():
        log.warning(f'ignoring non-integral config value for {key!r}: {value!r}')
        return default
    value = typ(value)
    if not minimum <= value <= maximum:
        log.warning(f'ignoring out-of-range config value for {key!r}: {value!r} '
                    f'(expected {minimum}..{maximum})')
        return default
    return value


def load() -> dict:
    """Return DEFAULTS merged with overrides from CONFIG_PATH. Never raises."""
    cfg = dict(DEFAULTS)
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            if os.path.exists(CONFIG_PATH):
                log.warning('config present but tomllib/tomli unavailable '
                            '(Python <3.11); using defaults')
            return cfg
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, 'rb') as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning(f'failed to parse {CONFIG_PATH}: {e}; using defaults')
        return cfg
    # Accept the historical flat daemon keys, but do not mistake a file which
    # contains only [client] for a flat daemon table and warn on every launch.
    section = data.get('daemon')
    if section is None:
        section = data if any(key in data for key in DEFAULTS) else {}
    if not isinstance(section, dict):
        log.warning('ignoring invalid [daemon] config (expected a table)')
        return cfg
    for k, v in section.items():
        if k in cfg:
            cfg[k] = _validated_number(k, v, DEFAULTS[k], _DAEMON_RULES[k])
        else:
            log.warning(f'ignoring unknown/invalid config key: {k!r}')
    if cfg['backoff_cap'] < cfg['backoff_base']:
        log.warning('backoff_cap is below backoff_base; raising cap to base')
        cfg['backoff_cap'] = cfg['backoff_base']
    if cfg['network_backoff_cap'] < cfg['network_backoff_base']:
        log.warning('network_backoff_cap is below network_backoff_base; '
                    'raising cap to base')
        cfg['network_backoff_cap'] = cfg['network_backoff_base']
    return cfg


def valid_gateway(value) -> bool:
    """Whether *value* is a safe OpenSSH destination argv item."""
    return (isinstance(value, str) and 0 < len(value) <= 255
            and _GATEWAY_RE.fullmatch(value) is not None)


def _client_agent_command(value) -> list[str] | None:
    if isinstance(value, str):
        try:
            parts = shlex.split(value)
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parts = list(value)
    else:
        return None
    if not 1 <= len(parts) <= 16:
        return None
    if parts[0].startswith('-'):
        return None
    if any(not part or len(part) > 512 or '\x00' in part
           or '\n' in part or '\r' in part for part in parts):
        return None
    return parts


def _client_control_path(value) -> str | None:
    """Validate an externally managed ControlPath template.

    The value reaches OpenSSH as one argv item, so it must not look like an
    option or carry anything that could break the argument.  OpenSSH expands
    ``~`` and its own %-tokens itself; we only expand ``~`` so our own
    ``ssh -O check`` and any diagnostics agree with what ssh will open.
    """
    if not isinstance(value, str):
        return None
    path = os.path.expanduser(value.strip())
    if not path:
        return ''
    if path.startswith('-') or len(path) > 200:
        return None
    if any(ch in path for ch in ('\x00', '\n', '\r')) or _CONTROL_CHARS.search(path):
        return None
    return path


def _read_client_state() -> dict | None:
    """Read the small TUI-owned connection selection, if one is trustworthy."""
    fd = None
    try:
        flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
        fd = os.open(CLIENT_STATE_PATH, flags)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > _CLIENT_STATE_LIMIT):
            return None
        raw = bytearray()
        while len(raw) <= _CLIENT_STATE_LIMIT:
            chunk = os.read(fd, min(
                16 * 1024, _CLIENT_STATE_LIMIT + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > _CLIENT_STATE_LIMIT:
            return None
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if not isinstance(value, dict) or value.get('version') != 1:
        return None
    mode = value.get('mode')
    gateways = value.get('gateways')
    if mode not in {'gateway', 'login'} or not isinstance(gateways, list):
        return None
    clean = []
    for gateway in gateways[:64]:
        if valid_gateway(gateway) and gateway not in clean:
            clean.append(gateway)
    if mode == 'gateway' and not clean:
        return None
    command = _client_agent_command(value.get('agent_command', ['atmux-agent']))
    if command is None:
        return None
    return {'mode': mode, 'gateways': clean, 'agent_command': command}


def client_state_exists() -> bool:
    """Whether the user has completed (or dismissed) the TUI connection setup."""
    return _read_client_state() is not None


def save_client_state(mode: str, gateways: list[str],
                      agent_command) -> None:
    """Atomically persist a connection selection made by the local TUI."""
    if mode not in {'gateway', 'login'}:
        raise ValueError('invalid connection mode')
    clean = []
    for gateway in gateways:
        if not valid_gateway(gateway):
            raise ValueError(f'invalid SSH alias: {gateway!r}')
        if gateway not in clean:
            clean.append(gateway)
    if mode == 'gateway' and not clean:
        raise ValueError('select at least one login gateway')
    command = _client_agent_command(agent_command)
    if command is None:
        raise ValueError('invalid remote agent command')
    value = {
        'version': 1,
        'mode': mode,
        'gateways': clean,
        'agent_command': command,
    }
    raw = (json.dumps(value, ensure_ascii=False, separators=(',', ':'))
           + '\n').encode('utf-8')
    directory = os.path.dirname(CLIENT_STATE_PATH)
    if not directory:
        raise ValueError('connection state path needs a parent directory')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    directory_info = os.lstat(directory)
    if (stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()):
        raise OSError('connection state directory is not a user-owned directory')
    temporary = os.path.join(
        directory, f'.connections.tmp.{os.getpid()}.{uuid.uuid4().hex}')
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, 'O_CLOEXEC', 0)
             | getattr(os, 'O_NOFOLLOW', 0))
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CLIENT_STATE_PATH)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def discover_ssh_aliases(path: str | None = None) -> list[str]:
    """Return literal ``Host`` aliases from bounded user SSH config files.

    Wildcard/negated patterns are intentionally omitted: they are matching
    rules, not destinations a user can select.  ``Include`` is followed with
    strict file-count and byte limits so a connection dialog cannot be held
    forever by an accidentally huge include tree.
    """
    root = os.path.expanduser(path or SSH_CONFIG_PATH)
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    visited: set[str] = set()
    total_bytes = 0

    def visit(filename: str) -> None:
        nonlocal total_bytes
        if len(visited) >= _SSH_CONFIG_FILE_COUNT_LIMIT:
            return
        filename = os.path.abspath(os.path.expanduser(filename))
        identity = os.path.realpath(filename)
        if identity in visited:
            return
        try:
            info = os.stat(filename)
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_size > _SSH_CONFIG_FILE_LIMIT
                    or total_bytes + info.st_size > _SSH_CONFIG_TOTAL_LIMIT):
                return
            with open(filename, 'r', encoding='utf-8', errors='replace') as handle:
                content = handle.read(_SSH_CONFIG_FILE_LIMIT + 1)
        except OSError:
            return
        if len(content.encode('utf-8', 'replace')) > _SSH_CONFIG_FILE_LIMIT:
            return
        visited.add(identity)
        total_bytes += info.st_size
        base = os.path.dirname(filename)
        for line in content.splitlines():
            try:
                parts = shlex.split(line, comments=True, posix=True)
            except ValueError:
                continue
            if len(parts) < 2:
                continue
            keyword = parts[0].lower()
            if keyword == 'host':
                for alias in parts[1:]:
                    if (alias.startswith('!')
                            or any(char in alias for char in '*?[]')
                            or not valid_gateway(alias)
                            or alias in seen_aliases):
                        continue
                    seen_aliases.add(alias)
                    aliases.append(alias)
            elif keyword == 'include':
                for pattern in parts[1:]:
                    expanded = os.path.expanduser(pattern)
                    if not os.path.isabs(expanded):
                        expanded = os.path.join(base, expanded)
                    for included in sorted(glob.glob(expanded)):
                        visit(included)

    visit(root)
    return aliases


def _apply_client_state(cfg: dict) -> dict:
    state = _read_client_state()
    if state is not None:
        cfg['mode'] = state['mode']
        cfg['gateways'] = list(state['gateways'])
        cfg['agent_command'] = list(state['agent_command'])
    return cfg


def load_client() -> dict:
    """Load and validate the optional ``[client]`` gateway configuration.

    This parser never raises.  Invalid gateway entries are ignored individually
    so one typo cannot prevent a user from falling back to native login-node
    mode.
    """
    cfg = dict(CLIENT_DEFAULTS)
    cfg['gateways'] = []
    cfg['agent_command'] = list(CLIENT_DEFAULTS['agent_command'])
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return _apply_client_state(cfg)
    if not os.path.exists(CONFIG_PATH):
        return _apply_client_state(cfg)
    try:
        with open(CONFIG_PATH, 'rb') as handle:
            data = tomllib.load(handle)
    except Exception as error:
        log.warning(f'failed to parse {CONFIG_PATH}: {error}; using client defaults')
        return _apply_client_state(cfg)
    section = data.get('client', {})
    if not isinstance(section, dict):
        log.warning('ignoring invalid [client] config (expected a table)')
        return _apply_client_state(cfg)

    mode = section.get('mode', cfg['mode'])
    if isinstance(mode, str) and mode in {'auto', 'gateway', 'login'}:
        cfg['mode'] = mode
    elif 'mode' in section:
        log.warning(f'ignoring invalid client mode: {mode!r}')

    gateways = section.get('gateways', [])
    if isinstance(gateways, list):
        seen = set()
        for gateway in gateways[:64]:
            if valid_gateway(gateway) and gateway not in seen:
                seen.add(gateway)
                cfg['gateways'].append(gateway)
            else:
                log.warning(f'ignoring invalid/duplicate gateway: {gateway!r}')
    elif 'gateways' in section:
        log.warning('ignoring invalid client gateways (expected an array)')

    for key, rule in _CLIENT_NUMBER_RULES.items():
        if key in section:
            cfg[key] = _validated_number(
                key, section[key], CLIENT_DEFAULTS[key], rule)
    if cfg['backoff_cap'] < cfg['backoff_base']:
        log.warning('client backoff_cap is below backoff_base; raising cap to base')
        cfg['backoff_cap'] = cfg['backoff_base']

    if 'agent_command' in section:
        command = _client_agent_command(section['agent_command'])
        if command is None:
            log.warning('ignoring invalid client agent_command')
        else:
            cfg['agent_command'] = command

    if 'control_path' in section:
        control_path = _client_control_path(section['control_path'])
        if control_path is None:
            log.warning('ignoring invalid client control_path')
        else:
            cfg['control_path'] = control_path
    return _apply_client_state(cfg)


def load_keepalive() -> dict:
    """Return KEEPALIVE_DEFAULTS merged with the [keepalive] table from
    CONFIG_PATH. Never raises. `enabled` is a bool; the rest are numeric."""
    cfg = dict(KEEPALIVE_DEFAULTS)
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return cfg
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, 'rb') as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning(f'failed to parse {CONFIG_PATH}: {e}; using keepalive defaults')
        return cfg
    section = data.get('keepalive', {})
    if not isinstance(section, dict):
        log.warning('ignoring invalid [keepalive] config (expected a table)')
        return cfg
    for k, v in section.items():
        if k == 'enabled' and isinstance(v, bool):
            cfg[k] = v
        elif k in cfg and k != 'enabled' and isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[k] = v
        else:
            log.warning(f'ignoring unknown/invalid keepalive key: {k!r}')
    # Sanity floors: a typo must not turn keep-alive into a runaway submitter or
    # a dead-on-arrival entry. max_failures>=1 (0 would pause before ever
    # submitting); submit_timeout>=1; the rest just can't be negative.
    # ``nan``/``inf`` are legal TOML floats but int(nan) raises and inf-sized
    # waits effectively disable renewal forever.  Clamp ordinary negative
    # typos for backwards compatibility; replace non-finite values with sane
    # defaults.
    limits = {
        'max_failures': (1, 100),
        'submit_timeout': (1, 3_600),
        'cooldown': (0, 2_592_000),
        'lead_time': (0, 2_592_000),
    }
    for key, (minimum, maximum) in limits.items():
        value = cfg[key]
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            value = KEEPALIVE_DEFAULTS[key]
        cfg[key] = min(maximum, max(minimum, int(value)))
    return cfg


def _notify_webhook_url(value) -> str | None:
    """Validate the reminder endpoint.

    Only http(s) is accepted: the daemon posts this without further inspection,
    so a ``file:``/``ftp:`` URL would turn a config typo into an unexpected
    local read.  Control characters cannot appear in a real URL and would let a
    malformed value smear across the daemon log.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return ''
    if _CONTROL_CHARS.search(url) or len(url) > 2048:
        return None
    if not url.startswith(('http://', 'https://')):
        return None
    return url


def _notify_normalized(cfg: dict) -> dict:
    """Hook for cross-field rules on the reminder config.

    ``enabled`` is the master switch. Each delivery route carries its own
    precondition instead -- the webhook needs ``webhook_url``, the desktop
    route needs nothing -- so an unset URL must not silence desktop popups.
    """
    return cfg


def load_notify() -> dict:
    """Return NOTIFY_DEFAULTS merged with the ``[notify]`` table. Never raises."""
    cfg = dict(NOTIFY_DEFAULTS)
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return _notify_normalized(cfg)
    if not os.path.exists(CONFIG_PATH):
        return _notify_normalized(cfg)
    try:
        with open(CONFIG_PATH, 'rb') as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning(f'failed to parse {CONFIG_PATH}: {e}; using notify defaults')
        return _notify_normalized(cfg)
    section = data.get('notify', {})
    if not isinstance(section, dict):
        log.warning('ignoring invalid [notify] config (expected a table)')
        return _notify_normalized(cfg)

    for flag in ('enabled', 'desktop'):
        if flag in section:
            if isinstance(section[flag], bool):
                cfg[flag] = section[flag]
            else:
                log.warning(
                    f'ignoring invalid notify {flag} (expected a boolean)')
    if 'webhook_url' in section:
        url = _notify_webhook_url(section['webhook_url'])
        if url is None:
            log.warning('ignoring invalid notify webhook_url '
                        '(expected an http(s) URL)')
        else:
            cfg['webhook_url'] = url
    for key, rule in _NOTIFY_NUMBER_RULES.items():
        if key in section:
            cfg[key] = _validated_number(
                key, section[key], NOTIFY_DEFAULTS[key], rule)
    return _notify_normalized(cfg)
