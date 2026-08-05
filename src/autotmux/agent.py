"""SSH-stdio agent used by AutoTmux's optional local gateway mode.

The agent deliberately exposes no listening network socket.  A local client
executes it as an authenticated SSH command, sends one bounded JSON request on
stdin, and receives one framed JSON response on stdout.  Interactive requests
reuse the login host's existing compute-node ControlMaster when available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
import queue

from autotmux import __version__, config, gateway, ipc, keepalive, lifecycle, paths


_STATE_LIMIT = 8 * 1024 * 1024
_REQUEST_LIMIT = 64 * 1024
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_JOB_RE = re.compile(r"^\d+(?:_\d+|_\[[0-9,%-]+\])?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe_error(value, limit: int = 240) -> str:
    cleaned = _CONTROL_RE.sub(" ", str(value or ""))
    return " ".join(cleaned.split())[:limit]


def _active_base() -> str:
    return lifecycle.active_runtime_base(paths.GUARD_FILE) or paths.BASE


def _state_path() -> str:
    return os.path.join(_active_base(), "daemon.json")


def _preview_path() -> str:
    return os.path.join(_active_base(), "preview.sock")


def _daemon_running() -> bool:
    base = lifecycle.active_runtime_base(paths.GUARD_FILE)
    if base is not None:
        return True
    return lifecycle.lock_is_held(paths.PID_FILE + ".lock")


def _read_state() -> dict | None:
    try:
        raw = lifecycle.read_owned_regular_file(_state_path(), _STATE_LIMIT)
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _request_daemon_start() -> bool:
    """Start the native login-node daemon without tying it to this SSH RPC."""
    if _daemon_running():
        return False
    try:
        subprocess.Popen(
            [sys.executable, "-m", "autotmux.daemon", "start"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            close_fds=True)
        return True
    except OSError:
        return False


def _bounded_registry_read(timeout: float = 0.5) -> tuple[bool, list]:
    """Keep a slow shared home filesystem off the state RPC critical path."""
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put(keepalive._load_registry_checked(config.KEEPALIVE_PATH))
        except Exception:
            try:
                result.put((False, []))
            except queue.Full:
                pass

    threading.Thread(target=worker, daemon=True,
                     name="agent-registry-read").start()
    try:
        value = result.get(timeout=max(0.01, float(timeout)))
    except queue.Empty:
        return False, []
    return value if (isinstance(value, tuple) and len(value) == 2) else (False, [])


def _state_response() -> dict:
    state = _read_state()
    running = _daemon_running()
    starting = False
    if not running:
        starting = _request_daemon_start()
        # Most healthy daemon starts publish readiness in well under a second.
        # A short wait avoids a blank first local frame without allowing a busy
        # login node to hold the gateway race hostage.
        deadline = time.monotonic() + 1.25
        while state is None and time.monotonic() < deadline:
            time.sleep(0.05)
            state = _read_state()
            if state is not None:
                break
    if state is None:
        return {
            "ok": False,
            "kind": "starting" if starting else "unavailable",
            "reason": ("login-node daemon is starting" if starting
                       else "login-node daemon state is unavailable"),
            "host": socket.gethostname(),
        }
    registry_ok, registry_entries = _bounded_registry_read()
    return {
        "ok": True,
        "state": state,
        "host": socket.gethostname(),
        "daemon_running": running,
        "daemon_starting": starting,
        "keepalive_entries": (
            [entry for entry in registry_entries if isinstance(entry, dict)]
            if registry_ok else None),
    }


def _forward_daemon_request(request: dict, timeout: float = 12.0) -> dict:
    if not _daemon_running():
        _request_daemon_start()
        return {"ok": False, "kind": "starting",
                "reason": "login-node daemon is starting", "retry_after": 1.0}
    try:
        return ipc.request(_preview_path(), request, timeout)
    except FileNotFoundError:
        return {"ok": False, "kind": "starting",
                "reason": "login-node daemon service is starting",
                "retry_after": 1.0}
    except Exception as error:
        return {"ok": False, "kind": "unavailable",
                "reason": _safe_error(error), "retry_after": 2.0}


def _scontrol_job(job_id: str) -> dict | None:
    if not isinstance(job_id, str) or _JOB_RE.fullmatch(job_id) is None:
        return None
    try:
        result = subprocess.run(
            ["scontrol", "show", "job", job_id],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return keepalive.parse_scontrol(result.stdout or "")


def _bounded_text(value, *, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError("expected a string")
        return ""
    if required and not value:
        raise ValueError("expected a non-empty string")
    if len(value.encode("utf-8", "surrogatepass")) > maximum:
        raise ValueError("string value is too large")
    return value


def handle_rpc(request: dict) -> dict:
    action = request.get("action")
    if action == "ping":
        running = _daemon_running()
        starting = False
        if request.get("ensure_daemon") is True and not running:
            starting = _request_daemon_start()
        return {"ok": True, "host": socket.gethostname(),
                "user": os.environ.get("USER", ""),
                "version": __version__, "daemon_running": running,
                "daemon_starting": starting}
    if action == "state":
        return _state_response()
    if action in {"preview", "report", "status", "session"}:
        node = request.get("node")
        if not isinstance(node, str) or _NODE_RE.fullmatch(node) is None:
            return {"ok": False, "kind": "invalid", "reason": "invalid node"}
        forwarded = {"action": action, "node": node}
        if action == "session":
            # The daemon validates the verb and the name; bound them here too
            # so a malformed request never reaches it as an oversized frame.
            verb = request.get("verb")
            if verb not in config.SESSION_VERBS:
                return {"ok": False, "kind": "invalid",
                        "reason": "invalid session verb"}
            try:
                forwarded["session"] = _bounded_text(
                    request.get("session"), maximum=4096, required=True)
            except ValueError as error:
                return {"ok": False, "kind": "invalid", "reason": str(error)}
            forwarded["verb"] = verb
        elif action == "preview":
            try:
                forwarded["session"] = _bounded_text(
                    request.get("session"), maximum=4096, required=True)
            except ValueError as error:
                return {"ok": False, "kind": "invalid", "reason": str(error)}
        elif action == "report":
            forwarded.update({
                "outcome": request.get("outcome"),
                "reason": _safe_error(request.get("reason"), 200),
                "source": _safe_error(request.get("source"), 40),
            })
        return _forward_daemon_request(forwarded)
    if action == "keepalive-list":
        ok, entries = keepalive._load_registry_checked(config.KEEPALIVE_PATH)
        if not ok:
            return {"ok": False, "kind": "unavailable",
                    "reason": "could not safely read keep-alive registry"}
        return {"ok": True, "entries": [entry for entry in entries
                                          if isinstance(entry, dict)]}
    if action == "scontrol":
        job_id = request.get("job_id")
        info = _scontrol_job(job_id) if isinstance(job_id, str) else None
        if info is None:
            return {"ok": False, "kind": "not-found",
                    "reason": "could not read that Slurm job"}
        return {"ok": True, "info": info}
    if action == "keepalive-set":
        try:
            job_name = _bounded_text(
                request.get("job_name"), maximum=4096, required=True)
            command = _bounded_text(request.get("command"), maximum=64 * 1024)
            workdir = _bounded_text(request.get("workdir"), maximum=16 * 1024)
            enabled = request.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")
            job_id = request.get("job_id")
            entry_id = request.get("entry_id")
            if job_id is not None:
                if keepalive.job_family_id(job_id) is None:
                    raise ValueError("invalid Slurm job id")
                job_id = str(job_id)
            if entry_id is not None:
                entry_id = _bounded_text(entry_id, maximum=256, required=True)
            result = keepalive.set_entry_enabled(
                config.KEEPALIVE_PATH, job_name, enabled, command, workdir,
                job_id=job_id, entry_id=entry_id)
            return {"ok": True, "enabled": result}
        except Exception as error:
            return {"ok": False, "kind": "invalid",
                    "reason": _safe_error(error)}
    return {"ok": False, "kind": "invalid", "reason": "invalid action"}


def _emit(value: dict) -> None:
    response = dict(value)
    response["protocol"] = gateway.PROTOCOL_VERSION
    response.setdefault("version", __version__)
    raw = json.dumps(response, ensure_ascii=False,
                     separators=(",", ":"))
    sys.stdout.write(gateway.PROTOCOL_PREFIX + raw + "\n")
    sys.stdout.flush()


def rpc_main() -> int:
    try:
        raw = sys.stdin.buffer.readline(_REQUEST_LIMIT + 1)
        if len(raw) > _REQUEST_LIMIT:
            raise ValueError("request is too large")
        if not raw.endswith(b"\n"):
            raise ValueError("request frame is incomplete")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        _emit(handle_rpc(request))
        return 0
    except Exception as error:
        _emit({"ok": False, "kind": "invalid", "reason": _safe_error(error)})
        return 2


def _control_path(node: str) -> str | None:
    base = _active_base()
    ctl = paths.control_path(node, os.path.join(base, "ctl"))
    try:
        info = os.lstat(ctl)
    except OSError:
        return None
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()):
        return None
    return ctl


def _report_interactive(node: str, returncode: int, source: str) -> None:
    if node == "localhost":
        return
    outcome = "failure" if returncode == 255 else "success"
    try:
        _forward_daemon_request({
            "action": "report", "node": node, "outcome": outcome,
            "reason": ("interactive SSH failed" if outcome == "failure" else ""),
            "source": source,
        }, 0.75)
    except Exception:
        pass


def _compute_ssh_argv(node: str, session: str | None, *,
                      direct: bool = False) -> list[str]:
    settings = {
        "connect_timeout": int(config.DEFAULTS["connect_timeout"]),
        "server_alive_int": int(config.DEFAULTS["server_alive_int"]),
        "server_alive_max": int(config.DEFAULTS["server_alive_max"]),
    }
    state = _read_state()
    published = state.get("ssh_config") if isinstance(state, dict) else None
    bounds = {
        "connect_timeout": (1, 600),
        "server_alive_int": (1, 3_600),
        "server_alive_max": (1, 100),
    }
    if isinstance(published, dict):
        for key, (minimum, maximum) in bounds.items():
            value = published.get(key)
            if (isinstance(value, int) and not isinstance(value, bool)
                    and minimum <= value <= maximum):
                settings[key] = value
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(settings['connect_timeout'])}",
        "-o", "ConnectionAttempts=1",
        "-o", f"ServerAliveInterval={int(settings['server_alive_int'])}",
        "-o", f"ServerAliveCountMax={int(settings['server_alive_max'])}",
        "-o", "StrictHostKeyChecking=accept-new",
        *gateway.interactive_ssh_options(),
    ]
    control_path = (None if direct else paths.control_path(
        f"agent-interactive-{node}", paths.INTERACTIVE_CTL_DIR))
    if control_path:
        args += [
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=300",
            "-o", f"ControlPath={control_path}",
        ]
    elif direct:
        args += ["-o", "ControlPath=none", "-o", "ControlMaster=no"]
    args += ["-t", node]
    if session is not None:
        args.append(
            f"exec tmux attach-session -d -t {shlex.quote(session)}")
    return args


def interactive_main(token: str) -> int:
    try:
        request = gateway.decode_interactive_token(token)
    except ValueError as error:
        sys.stderr.write(f"atmux-agent: {error}\n")
        return 2
    node = request["node"]
    kind = request["kind"]
    session = request.get("session") if kind == "attach" else None
    if node == "localhost":
        if kind == "shell":
            shell = os.environ.get("SHELL") or "/bin/bash"
            argv = [shell, "-l"]
        else:
            argv = ["tmux", "attach-session", "-d", "-t", session]
        try:
            return subprocess.call(argv)
        except OSError as error:
            sys.stderr.write(f"atmux-agent: {error.strerror or error}\n")
            return 127

    used_master = True
    try:
        returncode = subprocess.call(_compute_ssh_argv(node, session))
        if returncode == 255 and used_master:
            sys.stderr.write(
                "atmux-agent: compute ControlMaster failed; retrying direct SSH\n")
            returncode = subprocess.call(
                _compute_ssh_argv(node, session, direct=True))
    except OSError as error:
        sys.stderr.write(f"atmux-agent: {error.strerror or error}\n")
        returncode = 127
    _report_interactive(node, returncode, f"gateway-{kind}")
    return returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atmux-agent",
        description="Private SSH-stdio bridge for AutoTmux local mode.")
    parser.add_argument("--version", action="version",
                        version=f"AutoTmux agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rpc", help="serve one framed JSON request on stdin")
    interactive = sub.add_parser(
        "interactive", help="run one encoded interactive attach request")
    interactive.add_argument("token")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "rpc":
        raise SystemExit(rpc_main())
    raise SystemExit(interactive_main(args.token))


if __name__ == "__main__":
    main()
