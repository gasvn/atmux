"""What the browser dashboard is doing, and how to change it.

Everything here is a bounded read or a short command. It exists so the TUI can
answer "is it up, and what is the address" without anyone keeping a page of
shell incantations in their head -- which is the actual failure mode: a tool
you have to look up is a tool you stop reaching for.

Pure functions parse; the callers run the commands. That keeps the parsing
testable without a systemd or a tailscaled anywhere near the test.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess

SERVICE = 'atmux-web.service'
UNIT_NAME = 'atmux-web'
DEFAULT_PORT = 7681

# Long enough for a loaded machine, short enough that a wedged systemd or
# tailscaled degrades one line of a dialog instead of freezing the app.
COMMAND_TIMEOUT = 4.0


def _run(argv: list[str], timeout: float = COMMAND_TIMEOUT) -> tuple[int, str]:
    """Run a short command. Never raises; a missing binary is just a failure."""
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, ' '.join(str(error).split())[:200]
    return result.returncode, (result.stdout or '').strip()


def has_systemd() -> bool:
    """Whether this machine manages the server with a user unit."""
    return bool(shutil.which('systemctl')) and os.path.exists(
        os.path.expanduser(f'~/.config/systemd/user/{SERVICE}'))


def port_is_open(port: int = DEFAULT_PORT, host: str = '127.0.0.1') -> bool:
    """Whether something is listening. The authority on "is it up".

    A unit can be `active` while the process is wedged, and the server can be
    running without a unit at all -- someone launched it by hand. The socket
    is what the phone will actually meet.
    """
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def parse_serve_url(raw: str, port: int = DEFAULT_PORT) -> str:
    """The address `tailscale serve status --json` is publishing, or ''.

    Reads the config rather than the human-readable output: after a tailnet
    rename the text form kept printing the old hostname for a while, and a URL
    that looks right and 404s is worse than none.
    """
    try:
        config = json.loads(raw)
    except (ValueError, TypeError):
        return ''
    web = config.get('Web') if isinstance(config, dict) else None
    if not isinstance(web, dict):
        return ''
    for target, entry in web.items():
        handlers = entry.get('Handlers') if isinstance(entry, dict) else None
        if not isinstance(handlers, dict):
            continue
        for path, handler in handlers.items():
            proxy = handler.get('Proxy') if isinstance(handler, dict) else ''
            if not isinstance(proxy, str) or f':{port}' not in proxy:
                continue
            host, _, hostport = str(target).partition(':')
            if not host:
                continue
            # The mount path is the point of serving several things on one
            # hostname; dropping it here would print an address that loads
            # somebody else's service, or nothing.
            mount = str(path) if isinstance(path, str) else '/'
            if not mount.startswith('/'):
                mount = '/' + mount
            if not mount.endswith('/'):
                mount += '/'
            # 443 is implied and never written; anything else has to show.
            if hostport in ('', '443'):
                return f'https://{host}{mount}'
            return f'http://{host}:{hostport}{mount}'
    return ''


def parse_tailnet_host(raw: str) -> str:
    """This node's MagicDNS name from `tailscale status --json`, or ''."""
    try:
        status = json.loads(raw)
    except (ValueError, TypeError):
        return ''
    self_node = status.get('Self') if isinstance(status, dict) else None
    name = self_node.get('DNSName') if isinstance(self_node, dict) else ''
    return str(name).rstrip('.') if isinstance(name, str) else ''


def describe(port: int = DEFAULT_PORT) -> dict:
    """Everything the dialog shows, gathered with bounded commands."""
    listening = port_is_open(port)
    unit = ''
    enabled = ''
    if has_systemd():
        _rc, unit = _run(['systemctl', '--user', 'is-active', UNIT_NAME])
        _rc, enabled = _run(['systemctl', '--user', 'is-enabled', UNIT_NAME])

    served = ''
    tailnet = ''
    if shutil.which('tailscale'):
        rc, raw = _run(['tailscale', 'serve', 'status', '--json'])
        if rc == 0:
            served = parse_serve_url(raw, port)
        rc, raw = _run(['tailscale', 'status', '--json'])
        if rc == 0:
            tailnet = parse_tailnet_host(raw)

    url = served
    if not url and listening and tailnet:
        # Not published through serve, but the server may be bound to the
        # tailnet address directly -- which reaches the same peers.
        if port_is_open(port, tailnet):
            url = f'http://{tailnet}:{port}/'
    return {
        'listening': listening,
        'unit': unit,
        'enabled': enabled,
        'url': url,
        'tailnet': tailnet,
        'port': port,
        'systemd': has_systemd(),
    }


def summary(state: dict) -> str:
    """One line: is it up, and can it be reached."""
    if not state.get('listening'):
        return 'stopped'
    if state.get('url'):
        return 'running · reachable'
    return 'running · local only (not published to your tailnet)'


def control(verb: str) -> tuple[bool, str]:
    """start / stop / restart the user unit. Returns (ok, message)."""
    if verb not in ('start', 'stop', 'restart'):
        return False, f'unknown action {verb!r}'
    if not has_systemd():
        return False, ('no systemd user unit here — run `atmux-web` directly, '
                       'or install the unit shown below')
    rc, out = _run(['systemctl', '--user', verb, UNIT_NAME], timeout=10.0)
    if rc == 0:
        return True, f'{verb}ed'
    return False, out or f'systemctl {verb} failed'


def commands(state: dict) -> list[tuple[str, str]]:
    """The commands worth showing, as (what it does, command)."""
    if state.get('systemd'):
        return [
            ('start', f'systemctl --user start {UNIT_NAME}'),
            ('stop', f'systemctl --user stop {UNIT_NAME}'),
            ('always on', f'systemctl --user enable --now {UNIT_NAME}'),
            ('logs', f'journalctl --user -u {UNIT_NAME} -f'),
        ]
    return [
        ('start', f'atmux-web --port {state.get("port", DEFAULT_PORT)}'),
        ('publish', f'tailscale serve --bg {state.get("port", DEFAULT_PORT)}'),
    ]
