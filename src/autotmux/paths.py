"""Single source of truth for autotmux runtime file locations.

Prefers $XDG_RUNTIME_DIR (/run/user/<uid>, tmpfs, node-local, short path),
falling back to /tmp/autotmux_<uid> (the pre-XDG behavior). ~/.cache is
deliberately NOT used: it is NFS on HPC clusters, which breaks SSH
ControlMaster sockets — both because Unix-domain sockets misbehave on NFS
and because of the ~104-char sun_path length limit.
"""
import hashlib
import os
import stat as _stat

_UID = os.getuid()
_CONTROL_PATH_LIMIT = 100


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


def _usable_xdg_runtime_dir(xdg: str | None) -> bool:
    """Whether ``xdg`` satisfies the security part of the XDG specification.

    ``XDG_RUNTIME_DIR`` must be an absolute, user-owned, mode-0700 directory.
    Accepting ``.`` or another relative value makes every runtime path depend
    on the caller's working directory: two terminals can then start independent
    daemons which fight over the same SSH destinations.  A symlink or a
    group/world-accessible directory is also unsuitable for authenticated SSH
    multiplex sockets, so invalid values fall back to the private /tmp base.
    """
    if not isinstance(xdg, str) or not xdg or not os.path.isabs(xdg):
        return False
    try:
        st = os.lstat(xdg)
    except OSError:
        return False
    return (
        _stat.S_ISDIR(st.st_mode)
        and not _stat.S_ISLNK(st.st_mode)
        and st.st_uid == _UID
        and _stat.S_IMODE(st.st_mode) == 0o700
        and os.access(xdg, os.W_OK | os.X_OK)
    )


def _runtime_dir() -> str | None:
    """The per-user runtime directory, however this process was started.

    ``XDG_RUNTIME_DIR`` is exported by a login session and by systemd --user,
    and is simply absent over plain ``ssh host command``. Taking that absence
    as "there is no runtime directory" made the path depend on how you
    happened to log in rather than on the machine: a daemon started over SSH
    put its socket in /tmp while atmux-web under systemd looked in
    /run/user/<uid>, so the two never met and every action from the browser
    came back "no daemon is running on this machine" while one was.

    So the variable is preferred and the conventional location is the
    fallback -- checked the same way, because a directory nobody vouched for
    is not somewhere to put an authenticated SSH multiplex socket.
    """
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if _usable_xdg_runtime_dir(xdg):
        return xdg
    guess = f'/run/user/{_UID}'
    return guess if _usable_xdg_runtime_dir(guess) else None


def _pick_base() -> str:
    """Choose a writable, node-local runtime base dir and create it securely."""
    xdg = _runtime_dir()
    if xdg is not None:
        base = os.path.join(xdg, 'autotmux')
        # Unix-domain socket paths are limited to roughly 104–108 bytes.  Even
        # our hashed fallback needs some filename room; a deeply nested custom
        # XDG_RUNTIME_DIR would otherwise make every SSH master fail forever.
        shortest_hashed = os.path.join(base, 'ctl', 'cm_h-' + '0' * 32)
        if len(os.fsencode(shortest_hashed)) >= _CONTROL_PATH_LIMIT:
            base = f'/tmp/autotmux_{_UID}'
    else:
        base = f'/tmp/autotmux_{_UID}'
    return _secure_dir(base)


def _pick_guard_file() -> str:
    """Return one cwd-independent stable singleton-guard path.

    The override exists for isolated integration tests, but a relative value
    defeats the guard: launching from two working directories creates two lock
    inodes and therefore two daemons.  Fail clearly instead of silently
    weakening the singleton guarantee or touching the real default guard.
    """
    default = f'/tmp/autotmux_daemon_{_UID}.guard'
    value = os.environ.get('AUTOTMUX_GUARD_FILE', default)
    if not value or not os.path.isabs(value):
        raise RuntimeError('AUTOTMUX_GUARD_FILE must be an absolute path')
    return os.path.normpath(value)


BASE          = _pick_base()
CTL_DIR       = os.path.join(BASE, 'ctl')
PID_FILE      = os.path.join(BASE, 'daemon.pid')
LOG_FILE      = os.path.join(BASE, 'daemon.log')
STATE_FILE    = os.path.join(BASE, 'daemon.json')
SNAPSHOT_FILE = os.path.join(BASE, 'snapshots.json')
PREVIEW_SOCKET = os.path.join(BASE, 'preview.sock')
WARM_DIR      = os.path.join(BASE, 'warm')
# Interactive terminals must not share the daemon's compute-node master.  The
# daemon continuously runs health/session/preview commands; multiplexing those
# payloads with a user's keystrokes creates head-of-line stalls on one TCP
# stream.  Keep latency-sensitive masters in a directory the daemon never
# adopts, checks, or tears down.
INTERACTIVE_CTL_DIR = os.path.join(BASE, 'interactive-ctl')
GATEWAY_CTL_DIR = os.path.join(BASE, 'gateway-ctl')
GATEWAY_STATE_CACHE = os.path.join(BASE, 'gateway-state.json')
GATEWAY_SNAPSHOT_CACHE = os.path.join(BASE, 'gateway-snapshots.json')
# A second singleton guard lives outside XDG_RUNTIME_DIR.  systemd may remove
# the latter between logins while a double-forked daemon is still alive; an
# unlinked flock inode cannot be rediscovered by the next frontend, which used
# to allow a second daemon to start.  /tmp is node-local and survives that
# per-login cleanup.
GUARD_FILE    = _pick_guard_file()


def control_path(node: str, ctl_dir: str | None = None) -> str:
    """Return a deterministic ControlPath that fits the Unix socket limit."""
    ctl_dir = CTL_DIR if ctl_dir is None else ctl_dir
    safe = str(node).replace('/', '_').replace(':', '_')
    path = os.path.join(ctl_dir, f'cm_{safe}')
    if len(os.fsencode(path)) >= _CONTROL_PATH_LIMIT:
        digest = hashlib.sha256(str(node).encode('utf-8', errors='surrogatepass')).hexdigest()[:32]
        path = os.path.join(ctl_dir, f'cm_h-{digest}')
    if len(os.fsencode(path)) >= _CONTROL_PATH_LIMIT:
        raise RuntimeError(f'control socket directory is too long: {ctl_dir!r}')
    return path


# Record the directory identity selected by this process.  If systemd removes
# and recreates XDG_RUNTIME_DIR beneath a detached daemon, the old log/lock fds
# refer to unlinked inodes.  The daemon watchdog uses this token to exit and let
# the frontend start a clean instance in its newly-selected runtime directory.
_secure_dir(CTL_DIR)
_secure_dir(WARM_DIR)
_secure_dir(INTERACTIVE_CTL_DIR)
_secure_dir(GATEWAY_CTL_DIR)
_base_stat = os.lstat(BASE)
_BASE_ID = (_base_stat.st_dev, _base_stat.st_ino)


def ensure_runtime_dirs() -> None:
    """Validate the original runtime base and recreate private children."""
    _secure_dir(BASE)
    st = os.lstat(BASE)
    if (st.st_dev, st.st_ino) != _BASE_ID:
        raise RuntimeError(f'runtime dir {BASE!r} was replaced')
    _secure_dir(CTL_DIR)
    _secure_dir(WARM_DIR)
    _secure_dir(INTERACTIVE_CTL_DIR)
    _secure_dir(GATEWAY_CTL_DIR)
