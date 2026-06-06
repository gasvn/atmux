"""Single source of truth for autotmux runtime file locations.

Prefers $XDG_RUNTIME_DIR (/run/user/<uid>, tmpfs, node-local, short path),
falling back to /tmp/autotmux_<uid> (the pre-XDG behavior). ~/.cache is
deliberately NOT used: it is NFS on HPC clusters, which breaks SSH
ControlMaster sockets — both because Unix-domain sockets misbehave on NFS
and because of the ~104-char sun_path length limit.
"""
import os

_UID = os.getuid()


def _pick_base() -> str:
    """Choose a writable, node-local runtime base dir and create it."""
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK):
        base = os.path.join(xdg, 'autotmux')
    else:
        base = f'/tmp/autotmux_{_UID}'
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base


BASE          = _pick_base()
CTL_DIR       = os.path.join(BASE, 'ctl')
PID_FILE      = os.path.join(BASE, 'daemon.pid')
LOG_FILE      = os.path.join(BASE, 'daemon.log')
STATE_FILE    = os.path.join(BASE, 'daemon.json')
SNAPSHOT_FILE = os.path.join(BASE, 'snapshots.json')
