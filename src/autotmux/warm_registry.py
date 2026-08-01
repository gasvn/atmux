"""Identity-safe registry and cleanup for pre-warmed SSH children."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import time

from autotmux import lifecycle


_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REGISTRY_RE = re.compile(r"^warm-([1-9][0-9]*)\.json$")
_MAX_RECORD_BYTES = 16 * 1024


def registry_path(directory: str, pid: int) -> str:
    return os.path.join(directory, f"warm-{int(pid)}.json")


def _secure_registry_dir(directory: str) -> None:
    st = os.lstat(directory)
    if (not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise OSError(f"unsafe warm registry directory {directory!r}")


def ensure_directory(directory: str) -> None:
    """Create and verify the private registry directory.

    This also lets a new frontend interoperate with an older daemon whose
    runtime tree predates the warm-child registry.
    """
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _secure_registry_dir(directory)


def write_record(directory: str, *, parent_pid: int, parent_token: str,
                 node: str, control_path: str, ssh_argv: list[str]) -> str:
    """Publish the current helper/ssh PID before exec, without following links."""
    _secure_registry_dir(directory)
    pid = os.getpid()
    token = lifecycle.process_token(pid)
    if not token:
        raise OSError("cannot identify warm SSH child")
    record = {
        "version": 1,
        "kind": "warm-ssh",
        "pid": pid,
        "token": token,
        "parent_pid": int(parent_pid),
        "parent_token": str(parent_token),
        "node": str(node),
        "control_path": str(control_path),
        "argv": [str(value) for value in ssh_argv],
        "created": time.time(),
    }
    raw = json.dumps(record, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_RECORD_BYTES:
        raise OSError("warm SSH registry record is too large")
    path = registry_path(directory, pid)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f"unsafe warm registry file {path!r}")
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short warm registry write")
            view = view[written:]
    finally:
        os.close(fd)
    return path


def remove_record(path: str, expected_token: str | None = None) -> bool:
    try:
        if expected_token is not None:
            record = read_record(path)
            if record.get("token") != expected_token:
                return False
        os.unlink(path)
        return True
    except OSError:
        return False


def read_record(path: str) -> dict:
    raw = lifecycle.read_owned_regular_file(path, _MAX_RECORD_BYTES)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("warm registry record must be an object")
    return value


def _warm_ssh_matches(pid: int, node: str, control_path: str,
                      ctl_dir: str) -> bool:
    argv = lifecycle.process_cmdline(pid)
    if not argv or os.path.basename(argv[0]) != "ssh":
        return False
    if len(argv) < 3 or argv[-2:] != ["-tt", node]:
        return False
    if "-N" in argv or not _NODE_RE.fullmatch(node):
        return False
    expected_prefix = os.path.realpath(ctl_dir) + os.sep
    try:
        if not os.path.realpath(control_path).startswith(expected_prefix):
            return False
    except (OSError, TypeError):
        return False
    return (
        f"ControlPath={control_path}" in argv
        and "BatchMode=yes" in argv
        and "ConnectionAttempts=1" in argv
    )


def _helper_matches(pid: int, parent_pid: int, directory: str) -> bool:
    argv = lifecycle.process_cmdline(pid) or []
    joined = "\0".join(argv)
    return (
        "autotmux.ssh_child" in argv
        and f"--parent-pid\0{parent_pid}" in joined
        and f"--registry-dir\0{directory}" in joined
    )


def _deleted_warm_pty(pid: int) -> bool:
    targets = []
    for fd in (0, 1, 2):
        try:
            targets.append(os.readlink(f"/proc/{pid}/fd/{fd}"))
        except OSError:
            return False
    return (
        len(set(targets)) == 1
        and targets[0].startswith("/dev/pts/")
        and targets[0].endswith(" (deleted)")
    )


def _terminate(pid: int, token: str | None, grace: float = 0.5) -> bool:
    lifecycle.signal_same_process(pid, token, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while lifecycle.same_process(pid, token) and time.monotonic() < deadline:
        time.sleep(0.025)
    if lifecycle.same_process(pid, token):
        lifecycle.signal_same_process(pid, token, signal.SIGKILL)
    return not lifecycle.same_process(pid, token)


def sweep(directory: str, ctl_dir: str, *, include_legacy: bool = True) -> dict:
    """Reap registered children whose exact parent identity has disappeared.

    Legacy matching is intentionally much stricter: only an SSH with the exact
    warm-slave argument shape, PPID 1 and three references to the same deleted
    PTY is eligible.  Masters and usable interactive SSH clients cannot match.
    """
    stats = {"registered_killed": 0, "legacy_killed": 0,
             "stale_records": 0, "live_records": 0, "errors": 0}
    try:
        _secure_registry_dir(directory)
        names = os.listdir(directory)[:4096]
    except OSError:
        stats["errors"] += 1
        return stats
    registered_pids = set()
    for name in names:
        match = _REGISTRY_RE.fullmatch(name)
        if not match:
            continue
        path = os.path.join(directory, name)
        try:
            record = read_record(path)
            pid = int(record["pid"])
            token = str(record["token"])
            parent_pid = int(record["parent_pid"])
            parent_token = str(record["parent_token"])
            node = str(record["node"])
            control_path = str(record["control_path"])
            if pid != int(match.group(1)) or record.get("kind") != "warm-ssh":
                raise ValueError("registry identity mismatch")
        except (KeyError, OSError, TypeError, ValueError, UnicodeError):
            stats["errors"] += 1
            continue
        registered_pids.add(pid)
        if not lifecycle.same_process(pid, token):
            remove_record(path, token)
            stats["stale_records"] += 1
            continue
        if lifecycle.same_process(parent_pid, parent_token):
            stats["live_records"] += 1
            continue
        if (_warm_ssh_matches(pid, node, control_path, ctl_dir)
                or _helper_matches(pid, parent_pid, directory)):
            if _terminate(pid, token):
                stats["registered_killed"] += 1
                remove_record(path, token)
        else:
            # The PID token still matches but the process is no longer the
            # registered helper/ssh. Never redirect a cleanup signal.
            stats["errors"] += 1

    if not include_legacy:
        return stats
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        stats["errors"] += 1
        return stats
    for pid in pids:
        if pid in registered_pids or lifecycle.process_parent_pid(pid) != 1:
            continue
        argv = lifecycle.process_cmdline(pid) or []
        if len(argv) < 3 or os.path.basename(argv[0]) != "ssh":
            continue
        node = argv[-1]
        control_values = [
            value.removeprefix("ControlPath=") for value in argv
            if value.startswith("ControlPath=")
        ]
        if len(control_values) != 1:
            continue
        control_path = control_values[0]
        if (not _warm_ssh_matches(pid, node, control_path, ctl_dir)
                or not _deleted_warm_pty(pid)):
            continue
        token = lifecycle.process_token(pid)
        if token and _terminate(pid, token):
            stats["legacy_killed"] += 1
    return stats
