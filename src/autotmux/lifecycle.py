"""Small, side-effect-free process/lifecycle helpers.

Both the daemon controller and the TUI need to answer the same questions about
the daemon process.  Keeping the Linux ``/proc`` details here avoids the two
callers slowly acquiring different (and unsafe) definitions of "running".
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import stat
import sys
import threading
import time


_deferred_reap_lock = threading.Lock()
_deferred_reaps: dict[int, object] = {}
_deferred_reaper_thread: threading.Thread | None = None


def monotonic_clock_id() -> str:
    """Identify the host boot epoch to which monotonic timestamps belong."""
    boot_id = ''
    try:
        with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as f:
            boot_id = f.read(128).strip()
    except OSError:
        pass
    return f'{socket.gethostname()}:{boot_id}'


def _deferred_reaper_loop() -> None:
    global _deferred_reaper_thread
    while True:
        with _deferred_reap_lock:
            pending = list(_deferred_reaps.items())
            if not pending:
                _deferred_reaper_thread = None
                return
        for key, proc in pending:
            try:
                finished = proc.poll() is not None
            except (OSError, ChildProcessError):
                finished = True
            if finished:
                with _deferred_reap_lock:
                    if _deferred_reaps.get(key) is proc:
                        _deferred_reaps.pop(key, None)
        time.sleep(0.1)


def defer_popen_reap(proc) -> None:
    """Keep polling one killed-but-unreapable ``Popen`` from one shared thread.

    A child in uninterruptible I/O can outlive both bounded ``wait`` calls.
    Dropping its Popen handle then leaves a zombie once the kernel finally lets
    it exit. Retaining the owner and polling from one lazily-created daemon
    thread preserves responsive shutdown without one waiter thread per child.
    """
    global _deferred_reaper_thread
    key = id(proc)
    with _deferred_reap_lock:
        _deferred_reaps[key] = proc
        if (_deferred_reaper_thread is not None
                and _deferred_reaper_thread.is_alive()):
            return
        thread = threading.Thread(
            target=_deferred_reaper_loop, daemon=True,
            name='autotmux-deferred-child-reaper')
        _deferred_reaper_thread = thread
        try:
            thread.start()
        except BaseException:
            _deferred_reaper_thread = None
            _deferred_reaps.pop(key, None)
            raise


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """Return ``(state, start_time)`` from ``/proc/<pid>/stat``.

    ``comm`` is parenthesised and may itself contain spaces or ``)``.  Splitting
    after the final right parenthesis is therefore safer than a plain
    whitespace split.  ``start_time`` (field 22) is stable for the life of a
    process and lets callers detect PID reuse before signalling.
    """
    try:
        with open(f'/proc/{int(pid)}/stat', encoding='ascii') as f:
            raw = f.read()
        tail = raw[raw.rindex(')') + 2:].split()
        return tail[0], tail[19]
    except (OSError, ValueError, IndexError):
        return None


def process_token(pid: int) -> str | None:
    """Return a PID-reuse token, or ``None`` where ``/proc`` is unavailable."""
    stat = _proc_stat(pid)
    return stat[1] if stat else None


def process_parent_pid(pid: int) -> int | None:
    """Return a Linux process's current parent PID."""
    try:
        with open(f'/proc/{int(pid)}/stat', encoding='ascii') as f:
            raw = f.read()
        tail = raw[raw.rindex(')') + 2:].split()
        return int(tail[1])
    except (OSError, ValueError, IndexError):
        return None


def pid_running(pid: int) -> bool:
    """True for a live process, but false for a zombie waiting to be reaped."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    stat = _proc_stat(pid)
    return stat is None or stat[0] != 'Z'


def _cmdline(pid: int) -> list[bytes] | None:
    # Linux may expose an empty cmdline for a very short window while a freshly
    # spawned shebang script transitions through exec.  Controllers commonly
    # inspect a just-started daemon, so retry that transient without turning a
    # genuinely missing process into a long wait.
    for attempt in range(5):
        try:
            with open(f'/proc/{int(pid)}/cmdline', 'rb') as f:
                raw = f.read()
        except OSError:
            return None
        if raw or attempt == 4:
            return [part for part in raw.split(b'\x00') if part]
        time.sleep(0.005)
    return []


def process_cmdline(pid: int) -> list[str] | None:
    """Return a decoded argv snapshot suitable for exact identity checks."""
    raw = _cmdline(pid)
    if raw is None:
        return None
    return [os.fsdecode(value) for value in raw]


def _python_script_is_autotmux(raw_path: bytes,
                               raw_interpreter: bytes) -> bool:
    """Verify a Python launcher path without reading its potentially-NFS file."""
    path = os.path.abspath(os.fsdecode(raw_path))
    basename = os.path.basename(path)
    if basename == 'daemon.py':
        package_daemon = os.path.abspath(
            os.path.join(os.path.dirname(__file__), 'daemon.py'))
        return path == package_daemon
    if basename in {'atd', 'atmux-daemon'}:
        # setuptools installs the wrapper beside its shebang interpreter. This
        # distinguishes our console launcher from /usr/sbin/atd and arbitrary
        # same-named scripts without opening a home-directory file that could
        # itself hang while `atd stop` is trying to recover from NFS trouble.
        interpreter = os.path.abspath(os.fsdecode(raw_interpreter))
        trusted = {
            os.path.join(os.path.dirname(interpreter), name)
            for name in ('atd', 'atmux-daemon')
        }
        launcher = os.path.abspath(sys.argv[0])
        if os.path.basename(launcher).removesuffix('.exe') in {
                'atmux', 'atd', 'atmux-daemon'}:
            # `pip install --user` puts both wrappers in ~/.local/bin even when
            # their shebang interpreter lives in /usr/bin.
            trusted.update(
                os.path.join(os.path.dirname(launcher), name)
                for name in ('atd', 'atmux-daemon')
            )
        return path in trusted
    # Pre-package releases used this deliberately project-specific basename.
    return basename == 'autotmux_daemon.py'


_DAEMON_ACTIONS = {b'start', b'restart', b'run'}


def _has_daemon_action(args: list[bytes], index: int) -> bool:
    """Whether the script/module argv denotes a long-lived daemon command."""
    return index < len(args) and args[index].lower() in _DAEMON_ACTIONS


def is_autotmux_daemon(pid: int) -> bool:
    """Check daemon identity without substring false positives.

    A stale PID file must never make ``atd stop`` kill an unrelated process,
    and it must not make the frontend believe a dead daemon is healthy.  On a
    platform without ``/proc`` we retain the old liveness-only fallback.
    """
    args = _cmdline(pid)
    if args is None:
        return pid_running(pid)
    if not args:
        return False
    argv0 = os.path.basename(os.fsdecode(args[0]))

    is_python = argv0.startswith(('python', 'pypy'))
    if not is_python:
        return False

    # Parse only Python's interpreter portion. Tokens after ``-c`` or the first
    # script belong to user code; accepting a matching basename there lets an
    # unrelated process spoof a stale PID file with an ordinary "atd" arg.
    i = 1
    options_with_value = {b'-W', b'-X', b'--check-hash-based-pycs'}
    while i < len(args):
        arg = args[i]
        if arg == b'-c':
            return False
        if arg == b'-m':
            return (i + 1 < len(args)
                    and args[i + 1] == b'autotmux.daemon'
                    and _has_daemon_action(args, i + 2))
        if arg in options_with_value:
            i += 2
            continue
        if arg == b'--':
            i += 1
            if i >= len(args):
                return False
            arg = args[i]
        elif arg.startswith(b'-'):
            i += 1
            continue
        if not arg.startswith(b'-'):
            return (_python_script_is_autotmux(arg, args[0])
                    and _has_daemon_action(args, i + 1))
        i += 1
    return False


def same_process(pid: int, token: str | None) -> bool:
    """Whether ``pid`` is live and still denotes the process captured earlier."""
    if not pid_running(pid):
        return False
    current = process_token(pid)
    # No /proc: identity tokens are unavailable, so liveness is the best POSIX
    # fallback.  On Linux, a changed/missing token means the PID was reused.
    return token is None or (current is not None and current == token)


def signal_same_process(pid: int, token: str | None, sig: int) -> bool:
    """Signal only if ``pid`` still matches ``token``.  Returns whether sent."""
    if not same_process(pid, token):
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def open_lock_file(path: str, create: bool = False) -> int:
    """Open a user-owned regular lock file without following symlinks."""
    flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f'unsafe lock file {path!r}')
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_owned_regular_file(path: str) -> int:
    """Open a current-user regular file without following links or FIFOs.

    Runtime files are advisory inputs: a stale symlink must not redirect a
    controller into an unrelated file, and opening a FIFO must not make a
    status/stop command wait forever. ``O_NONBLOCK`` is harmless for regular
    files and makes the special-file rejection safe on local filesystems.
    """
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
             | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f'unsafe runtime file {path!r}')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_owned_regular_file(path: str, max_bytes: int) -> bytes:
    """Read a verified runtime file with a strict byte bound."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError('max_bytes must be a non-negative integer')
    fd = open_owned_regular_file(path)
    try:
        st = os.fstat(fd)
        if st.st_size > max_bytes:
            raise OSError(f'runtime file exceeds {max_bytes} bytes: {path!r}')
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise OSError(f'runtime file exceeds {max_bytes} bytes: {path!r}')
        return raw
    finally:
        os.close(fd)


def lock_is_held(path: str) -> bool:
    """True iff another process holds an exclusive ``flock`` on ``path``."""
    if not os.path.exists(path):
        return False
    try:
        fd = open_lock_file(path)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def lock_owner_pid(path: str) -> int | None:
    """Return the Linux PID holding ``path``'s flock, when discoverable.

    The lock file also stores the daemon PID in current releases, but
    ``/proc/locks`` is authoritative and recovers older daemons whose lock file
    was empty or whose advisory PID file disappeared.
    """
    fd = None
    try:
        fd = open_lock_file(path)
        st = os.fstat(fd)
        wanted = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        with open('/proc/locks', encoding='ascii') as f:
            lines = f.readlines()
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or parts[1] != 'FLOCK' or parts[3] != 'WRITE':
            continue
        try:
            dev_major, dev_minor, inode = parts[5].split(':')
            key = (int(dev_major, 16), int(dev_minor, 16), int(inode))
            pid = int(parts[4])
        except (ValueError, IndexError):
            continue
        if key == wanted and pid > 0:
            return pid
    return None


def read_lock_metadata(path: str) -> dict:
    """Read a small JSON payload from a verified lock inode, if present."""
    try:
        fd = open_lock_file(path)
    except OSError:
        return {}
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        value = json.loads(raw.decode('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    finally:
        os.close(fd)


def active_runtime_base(guard_path: str) -> str | None:
    """Return the secure runtime base advertised by a live stable guard.

    Different SSH/login environments do not always agree on
    ``XDG_RUNTIME_DIR``. The stable /tmp guard is shared between them, so a
    running daemon publishes its chosen base there and clients can follow its
    state and ControlMaster sockets instead of waiting on an unrelated path.
    """
    if not lock_is_held(guard_path):
        return None
    metadata = read_lock_metadata(guard_path)
    if metadata.get('ready') is False:
        return None
    base = metadata.get('base')
    if not isinstance(base, str) or not base or not os.path.isabs(base):
        return None
    try:
        st = os.lstat(base)
    except OSError:
        return None
    if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        return None
    return base


# Re-exporting the type most callers need avoids each module importing signal
# only for SIGTERM/SIGKILL constants in tests and small control paths.
SIGTERM = signal.SIGTERM
SIGKILL = signal.SIGKILL
