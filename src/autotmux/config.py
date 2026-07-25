"""Load daemon tunables from ~/.config/autotmux/config.toml.

Returns DEFAULTS merged with file overrides. Never raises: a missing,
malformed, or unparseable file falls back to defaults (with a logged
warning). The AUTOTMUX_CONFIG env var overrides the path (used by tests).
"""
import os
import logging

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
}


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
    section = data.get('daemon', data)  # accept [daemon] table or flat
    for k, v in section.items():
        if k in cfg and isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[k] = v
        else:
            log.warning(f'ignoring unknown/invalid config key: {k!r}')
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
    cfg['max_failures'] = max(1, int(cfg['max_failures']))
    cfg['submit_timeout'] = max(1, int(cfg['submit_timeout']))
    cfg['cooldown'] = max(0, int(cfg['cooldown']))
    cfg['lead_time'] = max(0, int(cfg['lead_time']))
    return cfg
