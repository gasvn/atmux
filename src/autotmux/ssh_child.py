"""Exec a warm SSH client that cannot outlive its owning atmux process."""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import sys

from autotmux import lifecycle, warm_registry


def _set_parent_death_signal(sig: int = signal.SIGTERM) -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(1, int(sig), 0, 0, 0)  # PR_SET_PDEATHSIG
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--parent-token", required=True)
    parser.add_argument("--registry-dir", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--control-path", required=True)
    parser.add_argument("ssh_argv", nargs=argparse.REMAINDER)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    ssh_argv = list(args.ssh_argv)
    if ssh_argv[:1] == ["--"]:
        ssh_argv = ssh_argv[1:]
    if not ssh_argv or os.path.basename(ssh_argv[0]) != "ssh":
        return 126
    try:
        _set_parent_death_signal()
        # Close the race where the parent died before prctl() was installed.
        if (os.getppid() != args.parent_pid
                or not lifecycle.same_process(
                    args.parent_pid, args.parent_token)):
            return 125
        path = warm_registry.write_record(
            args.registry_dir,
            parent_pid=args.parent_pid,
            parent_token=args.parent_token,
            node=args.node,
            control_path=args.control_path,
            ssh_argv=ssh_argv,
        )
        if (os.getppid() != args.parent_pid
                or not lifecycle.same_process(
                    args.parent_pid, args.parent_token)):
            warm_registry.remove_record(path)
            return 125
        os.execvp(ssh_argv[0], ssh_argv)
    except OSError as error:
        try:
            warm_registry.remove_record(
                warm_registry.registry_path(args.registry_dir, os.getpid()))
        except Exception:
            pass
        print(f"autotmux warm SSH helper: {error}", file=sys.stderr)
        return 127
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
