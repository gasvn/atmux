"""Load daemon tunables from ~/.config/autotmux/config.toml.

Returns DEFAULTS merged with file overrides. Never raises: a missing,
malformed, or unparseable file falls back to defaults (with a logged
warning). The AUTOTMUX_CONFIG env var overrides the path (used by tests).
"""
import os
import logging
import math
import re
import shlex

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

# Keep-alive auto-renew tunables (the [keepalive] table).
KEEPALIVE_DEFAULTS = {
    'enabled': True,        # master switch for the whole feature
    'lead_time': 900,       # seconds before expiry to submit the replacement
    'cooldown': 600,        # seconds to suppress re-submit after a submit
    'max_failures': 3,      # consecutive failed submits before pausing an entry
    'submit_timeout': 60,   # seconds to wait for sbatch to return
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
}

# SSH destinations are argv items, never shell fragments.  Supporting ordinary
# aliases plus user@host covers the common HPC cases; ports belong in
# ~/.ssh/config so a value can never be mistaken for an option or command.
_GATEWAY_RE = re.compile(
    r'^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._:-]*$')


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
            return cfg
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, 'rb') as handle:
            data = tomllib.load(handle)
    except Exception as error:
        log.warning(f'failed to parse {CONFIG_PATH}: {error}; using client defaults')
        return cfg
    section = data.get('client', {})
    if not isinstance(section, dict):
        log.warning('ignoring invalid [client] config (expected a table)')
        return cfg

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
    return cfg


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
