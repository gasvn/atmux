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
