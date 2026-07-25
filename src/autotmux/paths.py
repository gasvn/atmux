"""Single source of truth for autotmux runtime file locations.

Prefers $XDG_RUNTIME_DIR (/run/user/<uid>, tmpfs, node-local, short path),
falling back to /tmp/autotmux_<uid> (the pre-XDG behavior). ~/.cache is
deliberately NOT used: it is NFS on HPC clusters, which breaks SSH
ControlMaster sockets — both because Unix-domain sockets misbehave on NFS
and because of the ~104-char sun_path length limit.
"""
import os
import stat as _stat

_UID = os.getuid()


def _secure_dir(base: str) -> str:
    """Create `base` mode 0700 and verify it's a real directory we own.

    The runtime base holds the SSH ControlMaster sockets: anyone who can place
    or read files here can multiplex commands over our authenticated SSH
    connections. `makedirs(exist_ok=True)` alone would happily adopt a
    pre-existing attacker-owned dir or a symlink (a real risk for the
    /tmp/autotmux_<uid> fallback on a shared node), so we fail CLOSED if the
    directory isn't a non-symlink dir owned by us, and tighten loose perms."""
    os.makedirs(base, mode=0o700, exist_ok=True)
    st = os.lstat(base)                      # lstat: never follow a symlink here
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f'runtime dir {base!r} is a symlink or not a directory '
                           '— refusing (possible hijack)')
    if st.st_uid != _UID:
        raise RuntimeError(f'runtime dir {base!r} is owned by uid {st.st_uid}, '
                           f'not {_UID} — refusing (possible hijack)')
    if _stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(base, 0o700)
    return base


def _pick_base() -> str:
    """Choose a writable, node-local runtime base dir and create it securely."""
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK):
        base = os.path.join(xdg, 'autotmux')
    else:
        base = f'/tmp/autotmux_{_UID}'
    return _secure_dir(base)


BASE          = _pick_base()
CTL_DIR       = os.path.join(BASE, 'ctl')
PID_FILE      = os.path.join(BASE, 'daemon.pid')
LOG_FILE      = os.path.join(BASE, 'daemon.log')
STATE_FILE    = os.path.join(BASE, 'daemon.json')
SNAPSHOT_FILE = os.path.join(BASE, 'snapshots.json')
