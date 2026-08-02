"""Local multi-login gateway client for AutoTmux.

The ordinary login-node deployment remains the default.  When a local client
configures two or more login hosts, :class:`GatewayPool` races bounded RPCs over
SSH, keeps a sticky healthy route, and falls back to another login host after a
transport failure.  The remote side is ``autotmux.agent``; no TCP listener or
new credential is introduced.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from autotmux import __version__, config, lifecycle, paths


PROTOCOL_VERSION = 1
PROTOCOL_PREFIX = "AUTOTMUX/1 "
_RPC_REQUEST_LIMIT = 64 * 1024
_RPC_RESPONSE_LIMIT = 10 * 1024 * 1024
_CACHE_LIMIT = 10 * 1024 * 1024
_SNAPSHOT_CACHE_LIMIT = 16 * 1024 * 1024
_SNAPSHOT_ENTRY_LIMIT = 1024 * 1024
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class GatewayError(RuntimeError):
    """A gateway operation failed without a usable response."""


class GatewayBusy(GatewayError):
    """The bounded per-gateway RPC budget is currently full."""


class GatewayTransportError(GatewayError):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True)
class Route:
    gateway: str | None
    target: str
    fixed: bool = False


def encode_interactive_token(node: str, kind: str,
                             session: str | None = None) -> str:
    if not isinstance(node, str) or _NODE_RE.fullmatch(node) is None:
        raise ValueError("invalid interactive node")
    if kind not in {"attach", "shell"}:
        raise ValueError("invalid interactive action")
    if kind == "attach":
        if (not isinstance(session, str) or not session
                or len(session.encode("utf-8", "surrogatepass")) > 4096):
            raise ValueError("invalid tmux session")
    elif session is not None:
        raise ValueError("shell request cannot carry a session")
    payload = {"v": PROTOCOL_VERSION, "node": node, "kind": kind}
    if session is not None:
        payload["session"] = session
    raw = json.dumps(payload, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > 16 * 1024:
        raise ValueError("interactive request is too large")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_interactive_token(token: str) -> dict:
    if not isinstance(token, str) or not token or len(token) > 32 * 1024:
        raise ValueError("invalid interactive token")
    if re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise ValueError("invalid interactive token")
    padding = "=" * (-len(token) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(token + padding))
    except Exception as error:
        raise ValueError("invalid interactive token") from error
    if not isinstance(value, dict) or value.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported interactive token")
    node = value.get("node")
    kind = value.get("kind")
    if not isinstance(node, str) or _NODE_RE.fullmatch(node) is None:
        raise ValueError("invalid interactive node")
    if kind not in {"attach", "shell"}:
        raise ValueError("invalid interactive action")
    session = value.get("session")
    if kind == "attach":
        if (not isinstance(session, str) or not session
                or len(session.encode("utf-8", "surrogatepass")) > 4096):
            raise ValueError("invalid tmux session")
    elif session is not None:
        raise ValueError("shell request cannot carry a session")
    return value


def _safe_error(value, limit: int = 240) -> str:
    cleaned = _CONTROL_RE.sub(" ", str(value or ""))
    return " ".join(cleaned.split())[:limit]


def _atomic_write_json(path: str, value, limit: int) -> None:
    raw = json.dumps(value, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        raise ValueError(f"cache exceeds {limit} bytes")
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_owned_json(path: str, limit: int) -> dict:
    raw = lifecycle.read_owned_regular_file(path, limit)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _gateway_jitter(gateway: str, failures: int) -> float:
    digest = hashlib.sha256(
        f"{gateway}\0{failures}".encode("utf-8", "surrogatepass")
    ).digest()
    return 0.90 + int.from_bytes(digest[:2], "big") / 65535.0 * 0.20


def _login_key(gateway: str, remote_host: str, existing: set[str]) -> str:
    label = remote_host or gateway.rsplit("@", 1)[-1]
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.") or "gateway"
    base = f"login--{label}"[:220]
    candidate = base
    if candidate in existing or not _NODE_RE.fullmatch(candidate):
        suffix = hashlib.sha256(gateway.encode("utf-8")).hexdigest()[:8]
        candidate = f"login--{label[:200]}-{suffix}"
    return candidate


class GatewayPool:
    """Bounded, thread-safe gateway selection and SSH transport."""

    def __init__(self, settings: dict, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 popen=subprocess.Popen,
                 local_state_loader: Callable[[], dict] | None = None) -> None:
        self.settings = dict(settings)
        self._gateways = tuple(settings.get("gateways") or ())
        if not self._gateways or any(not config.valid_gateway(g) for g in self._gateways):
            raise ValueError("GatewayPool needs at least one valid gateway")
        self._clock = clock
        self._wall_clock = wall_clock
        self._pool_id = hashlib.sha256(
            "\0".join(sorted(self._gateways)).encode(
                "utf-8", "surrogatepass")).hexdigest()[:16]
        self._popen = popen
        self._local_state_loader = local_state_loader or self._local_node_state
        self._lock = threading.RLock()
        self._cache_lock = threading.Lock()
        self._rpc_slots = {gateway: threading.BoundedSemaphore(2)
                           for gateway in self._gateways}
        self._health = {
            gateway: {
                "failures": 0,
                "retry_at": 0.0,
                "ewma_ms": None,
                "last_success": None,
                "last_failure": None,
                "last_error": "",
            }
            for gateway in self._gateways
        }
        self._route_health: dict[tuple[str, str], dict] = {}
        self._last_probe: dict[str, float | None] = {
            gateway: None for gateway in self._gateways}
        self._active: str | None = None
        self._sticky_until = 0.0
        self._routes: dict[str, Route] = {}
        self._last_error = ""
        self._sequence = 0
        self._cache_sequence = 0
        self._last_state: dict = {}
        self._snapshots: dict = {}
        self._keepalive_entries: list[dict] = []
        self._keepalive_known = False
        self._load_caches()

    @property
    def enabled(self) -> bool:
        return True

    @property
    def gateways(self) -> tuple[str, ...]:
        return self._gateways

    @property
    def active_gateway(self) -> str | None:
        with self._lock:
            return self._active

    def _load_caches(self) -> None:
        state_cache_valid = False
        try:
            state = _read_owned_json(paths.GATEWAY_STATE_CACHE, _CACHE_LIMIT)
            gateway_info = state.get("gateway")
            if (isinstance(state.get("nodes"), dict)
                    and isinstance(gateway_info, dict)
                    and gateway_info.get("pool_id") == self._pool_id):
                self._last_state = state
                sequence = state.get("gateway_sequence")
                if isinstance(sequence, int) and not isinstance(sequence, bool):
                    self._sequence = max(0, sequence)
                    self._cache_sequence = self._sequence
                self._restore_routes(state)
                state_cache_valid = True
        except Exception:
            pass
        if not state_cache_valid:
            return
        try:
            snapshots = _read_owned_json(
                paths.GATEWAY_SNAPSHOT_CACHE, _SNAPSHOT_CACHE_LIMIT)
            self._snapshots = {
                key: value for key, value in snapshots.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
        except Exception:
            pass

    def _restore_routes(self, state: dict) -> None:
        routes = {}
        nodes = state.get("nodes")
        if isinstance(nodes, dict):
            for display, item in nodes.items():
                route = item.get("gateway_route") if isinstance(item, dict) else None
                if not isinstance(route, dict):
                    continue
                target = route.get("target")
                gateway = route.get("gateway")
                fixed = bool(route.get("fixed"))
                if (isinstance(display, str) and isinstance(target, str)
                        and _NODE_RE.fullmatch(target)
                        and (gateway is None or gateway in self._gateways)):
                    routes[display] = Route(gateway, target, fixed)
        self._routes = routes

    def _control_path(self, gateway: str) -> str:
        return paths.control_path(f"gateway-{gateway}", paths.GATEWAY_CTL_DIR)

    def _ssh_argv(self, gateway: str, *, tty: bool = False,
                  direct: bool = False) -> list[str]:
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(self.settings['connect_timeout'])}",
            "-o", "ConnectionAttempts=1",
            "-o", f"ServerAliveInterval={int(self.settings['server_alive_int'])}",
            "-o", f"ServerAliveCountMax={int(self.settings['server_alive_max'])}",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if direct:
            args += ["-o", "ControlPath=none", "-o", "ControlMaster=no"]
        else:
            args += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPersist={int(self.settings['control_persist'])}",
                "-o", f"ControlPath={self._control_path(gateway)}",
            ]
        args.append("-tt" if tty else "-T")
        args.append(gateway)
        return args

    def _agent_command(self, *args: str) -> str:
        command = list(self.settings.get("agent_command") or ["atmux-agent"])
        return shlex.join([*command, *args])

    @staticmethod
    def _parse_response(stdout: bytes) -> dict:
        if len(stdout) > _RPC_RESPONSE_LIMIT:
            raise GatewayError("gateway response is too large")
        try:
            text = stdout.decode("utf-8")
        except UnicodeError as error:
            raise GatewayError("gateway returned invalid UTF-8") from error
        for line in reversed(text.splitlines()):
            if not line.startswith(PROTOCOL_PREFIX):
                continue
            try:
                value = json.loads(line[len(PROTOCOL_PREFIX):])
            except (TypeError, ValueError) as error:
                raise GatewayError("gateway returned malformed JSON") from error
            if not isinstance(value, dict):
                raise GatewayError("gateway response is not an object")
            if value.get("protocol") != PROTOCOL_VERSION:
                raise GatewayError("gateway protocol version mismatch")
            return value
        raise GatewayError("gateway agent response marker is missing")

    @staticmethod
    def _kill_process(proc) -> None:
        try:
            if getattr(proc, "pid", None):
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                proc.kill()
            except (OSError, AttributeError):
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def _rpc_once(self, gateway: str, payload: dict, timeout: float,
                  *, direct: bool = False) -> dict:
        raw = json.dumps(payload, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > _RPC_REQUEST_LIMIT:
            raise ValueError("gateway request is too large")
        argv = self._ssh_argv(gateway, direct=direct)
        argv.append(self._agent_command("rpc"))
        try:
            proc = self._popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            raise GatewayTransportError(
                f"could not start ssh: {error.strerror or error}", 127) from error
        try:
            stdout, stderr = proc.communicate(raw, timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired as error:
            self._kill_process(proc)
            raise GatewayTransportError(
                f"gateway RPC timed out after {timeout:.1f}s") from error
        if proc.returncode != 0:
            detail = _safe_error(stderr.decode("utf-8", "replace"))
            raise GatewayTransportError(
                detail or f"ssh exited with status {proc.returncode}",
                proc.returncode)
        return self._parse_response(stdout)

    def _record_success(self, gateway: str, elapsed: float) -> None:
        elapsed_ms = max(0.0, elapsed * 1000.0)
        with self._lock:
            entry = self._health[gateway]
            previous = entry.get("ewma_ms")
            entry["ewma_ms"] = (elapsed_ms if previous is None
                                else float(previous) * 0.75 + elapsed_ms * 0.25)
            entry.update({
                "failures": 0,
                "retry_at": 0.0,
                "last_success": self._clock(),
                "last_error": "",
            })

    def _record_transport_alive(self, gateway: str) -> None:
        """Clear failures without treating an interactive lifetime as RTT."""
        with self._lock:
            entry = self._health[gateway]
            entry.update({
                "failures": 0,
                "retry_at": 0.0,
                "last_success": self._clock(),
                "last_error": "",
            })

    def _record_failure(self, gateway: str, error: str) -> None:
        now = self._clock()
        with self._lock:
            entry = self._health[gateway]
            failures = int(entry.get("failures", 0)) + 1
            delay = min(
                float(self.settings["backoff_cap"]),
                float(self.settings["backoff_base"]) * 2 ** min(failures - 1, 20),
            ) * _gateway_jitter(gateway, failures)
            entry.update({
                "failures": failures,
                "retry_at": now + delay,
                "last_failure": now,
                "last_error": _safe_error(error),
            })
            if self._active == gateway:
                self._sticky_until = 0.0

    def _rpc_gateway(self, gateway: str, payload: dict,
                     timeout: float | None = None) -> dict:
        if gateway not in self._rpc_slots:
            raise GatewayError(f"unknown gateway {gateway!r}")
        timeout = (float(self.settings["state_timeout"])
                   if timeout is None else max(0.1, float(timeout)))
        slot = self._rpc_slots[gateway]
        if not slot.acquire(timeout=min(0.25, timeout)):
            raise GatewayBusy(f"gateway {gateway} is busy")
        started = self._clock()
        try:
            try:
                response = self._rpc_once(gateway, payload, timeout)
            except GatewayTransportError as first:
                if first.returncode != 255:
                    raise
                # A stale local ControlMaster must not make a healthy login host
                # look dead.  Retry once without multiplexing before opening the
                # circuit or moving to another gateway.
                remaining = timeout - (self._clock() - started)
                if remaining <= 0.1:
                    raise
                response = self._rpc_once(
                    gateway, payload, remaining, direct=True)
            self._record_success(gateway, self._clock() - started)
            return response
        except GatewayError as error:
            self._record_failure(gateway, str(error))
            raise
        finally:
            slot.release()

    def _candidate_gateways(self, fixed: str | None = None) -> list[str]:
        if fixed is not None:
            return [fixed] if fixed in self._gateways else []
        now = self._clock()
        with self._lock:
            active = self._active
            sticky = active is not None and now < self._sticky_until
            health = copy.deepcopy(self._health)
        eligible = [gateway for gateway in self._gateways
                    if float(health[gateway].get("retry_at") or 0) <= now]
        if not eligible:
            eligible = [min(self._gateways,
                            key=lambda g: float(health[g].get("retry_at") or 0))]

        def score(gateway: str) -> tuple[float, int]:
            latency = health[gateway].get("ewma_ms")
            value = float(latency) if isinstance(latency, (int, float)) else math.inf
            return value, self._gateways.index(gateway)

        ordered = sorted(eligible, key=score)
        if sticky and active in ordered:
            ordered.remove(active)
            ordered.insert(0, active)
        elif active in ordered and health[active].get("ewma_ms") is None:
            ordered.remove(active)
            ordered.insert(0, active)
        return ordered

    def _route_for(self, node: str) -> Route:
        if node == "localhost":
            return Route(None, "localhost", True)
        with self._lock:
            route = self._routes.get(node)
        return route or Route(None, node, False)

    def _interactive_candidates(self, target: str,
                                fixed: str | None = None) -> list[str]:
        candidates = self._candidate_gateways(fixed)
        if fixed is not None:
            return candidates
        now = self._clock()
        with self._lock:
            retry = {
                gateway: float(self._route_health.get(
                    (gateway, target), {}).get("retry_at") or 0)
                for gateway in candidates
            }
        eligible = [gateway for gateway in candidates if retry[gateway] <= now]
        if eligible:
            return eligible
        return ([min(candidates, key=lambda gateway: retry[gateway])]
                if candidates else [])

    def _record_route_failure(self, gateway: str, target: str,
                              error: str) -> None:
        now = self._clock()
        with self._lock:
            entry = self._route_health.setdefault(
                (gateway, target), {"failures": 0, "retry_at": 0.0,
                                    "last_error": ""})
            failures = int(entry.get("failures") or 0) + 1
            delay = min(
                float(self.settings["backoff_cap"]),
                float(self.settings["backoff_base"])
                * 2 ** min(failures - 1, 20),
            ) * _gateway_jitter(f"{gateway}:{target}", failures)
            entry.update({"failures": failures, "retry_at": now + delay,
                          "last_error": _safe_error(error)})

    def _record_route_success(self, gateway: str, target: str) -> None:
        with self._lock:
            self._route_health.pop((gateway, target), None)

    def _set_active(self, gateway: str) -> None:
        with self._lock:
            if self._active != gateway:
                self._active = gateway
            self._sticky_until = self._clock() + float(self.settings["sticky_ttl"])

    def _schedule_backup_probes(self, active: str) -> None:
        """Keep standby login masters authenticated and their scores current."""
        now = self._clock()
        interval = float(self.settings["probe_interval"])
        to_probe = []
        with self._lock:
            for gateway in self._gateways:
                if gateway == active:
                    continue
                last = self._last_probe.get(gateway)
                retry_at = float(self._health[gateway].get("retry_at") or 0)
                if retry_at > now or (last is not None and now - last < interval):
                    continue
                self._last_probe[gateway] = now
                to_probe.append(gateway)

        def worker(name: str) -> None:
            try:
                self._rpc_gateway(
                    name, {"action": "ping", "ensure_daemon": True},
                    min(float(self.settings["state_timeout"]),
                        float(self.settings["connect_timeout"]) + 2.0))
            except Exception:
                pass

        for gateway in to_probe:
            threading.Thread(target=worker, args=(gateway,), daemon=True,
                             name=f"gateway-standby-{gateway}").start()

    def _health_payload(self) -> dict:
        now = self._clock()
        with self._lock:
            active = self._active
            items = []
            for gateway in self._gateways:
                entry = dict(self._health[gateway])
                retry_in = max(0.0, float(entry.get("retry_at") or 0) - now)
                items.append({
                    "name": gateway,
                    "state": (
                        "backoff" if retry_in > 0 else
                        "healthy" if entry.get("last_success") is not None else
                        "probing" if entry.get("failures") else "unknown"),
                    "latency_ms": entry.get("ewma_ms"),
                    "failures": int(entry.get("failures") or 0),
                    "retry_in": retry_in,
                    "last_error": entry.get("last_error") or "",
                })
        return {
            "mode": "gateway",
            "active": active or "",
            "healthy": sum(1 for item in items if item["state"] == "healthy"),
            "total": len(items),
            "items": items,
        }

    @staticmethod
    def _local_node_state() -> dict:
        sessions = []
        error = ""
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F",
                 "#{session_name}:#{session_windows}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=1.5)
            if result.returncode == 0:
                for line in (result.stdout or "").splitlines():
                    name, separator, windows = line.partition(":")
                    if name:
                        sessions.append([name, windows if separator else "?"])
        except FileNotFoundError:
            error = "local tmux is not installed"
        except subprocess.TimeoutExpired:
            error = "local tmux listing timed out"
        except OSError as exc:
            error = _safe_error(exc)
        try:
            load = f"{os.getloadavg()[0]:.2f}"
        except (AttributeError, OSError):
            load = ""
        return {
            "alive": True,
            "socket": "",
            "network": {
                "state": "healthy", "failures": 0, "retry_in": 0.0,
                "reason": "", "source": "local", "busy": False,
            },
            "info": {
                "job_id": "-", "job_name": "local",
                "time": "", "nproc": str(os.cpu_count() or ""),
                "load": load,
            },
            "sessions": sessions,
            "last_error": error,
            "gateway_route": {
                "gateway": None, "target": "localhost", "fixed": True,
            },
        }

    def _decorate_state(self, state: dict, gateway: str,
                        remote_host: str, agent_version: str = "",
                        keepalive_entries=None) -> dict:
        decorated = copy.deepcopy(state)
        source_nodes = decorated.get("nodes")
        if not isinstance(source_nodes, dict):
            raise GatewayError("gateway state has an invalid nodes object")
        nodes = {}
        routes = {}
        existing = {str(name) for name in source_nodes}
        login_display = _login_key(gateway, remote_host, existing)
        for source_name, raw_item in source_nodes.items():
            if (not isinstance(source_name, str)
                    or _NODE_RE.fullmatch(source_name) is None
                    or not isinstance(raw_item, dict)):
                continue
            display = login_display if source_name == "localhost" else source_name
            item = copy.deepcopy(raw_item)
            item["socket"] = ""
            fixed = source_name == "localhost"
            route_gateway = gateway if fixed else None
            item["gateway_route"] = {
                "gateway": route_gateway,
                "target": source_name,
                "fixed": fixed,
            }
            nodes[display] = item
            routes[display] = Route(route_gateway, source_name, fixed)
        nodes["localhost"] = self._local_state_loader()
        routes["localhost"] = Route(None, "localhost", True)
        decorated["nodes"] = nodes
        gateway_info = self._health_payload()
        gateway_info.update({
            "active": gateway,
            "source_host": remote_host,
            "cached": False,
            "received_epoch": self._wall_clock(),
            "received_monotonic": self._clock(),
            "last_error": "",
            "client_version": __version__,
            "agent_version": agent_version,
            "pool_id": self._pool_id,
        })
        decorated["gateway"] = gateway_info
        with self._lock:
            self._sequence += 1
            decorated["gateway_sequence"] = self._sequence
            self._routes = routes
            if isinstance(keepalive_entries, list):
                self._keepalive_entries = [
                    dict(entry) for entry in keepalive_entries
                    if isinstance(entry, dict) and entry.get("enabled")]
                self._keepalive_known = True
        return decorated

    def _cache_state(self, state: dict) -> None:
        with self._cache_lock:
            sequence = state.get("gateway_sequence")
            if (isinstance(sequence, int) and not isinstance(sequence, bool)
                    and sequence < self._cache_sequence):
                return
            try:
                _atomic_write_json(paths.GATEWAY_STATE_CACHE, state, _CACHE_LIMIT)
            except Exception:
                return
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                self._cache_sequence = sequence

    def _cached_or_empty_state(self, error: str) -> dict:
        with self._lock:
            cached = copy.deepcopy(self._last_state)
        now_epoch = self._wall_clock()
        if not isinstance(cached.get("nodes"), dict):
            cached = {
                "pid": None,
                "user": os.environ.get("USER", ""),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_monotonic": self._clock(),
                "monotonic_clock_id": lifecycle.monotonic_clock_id(),
                "squeue_long": "",
                "squeue_pending": "",
                "squeue_updated": "?",
                "nodes": {},
                "keepalive": {},
                "keepalive_health": {},
            }
        cached["nodes"]["localhost"] = self._local_state_loader()
        gateway_info = self._health_payload()
        previous = cached.get("gateway")
        received = (previous.get("received_epoch")
                    if isinstance(previous, dict) else None)
        cache_age = (max(0.0, now_epoch - float(received))
                     if isinstance(received, (int, float)) else None)
        gateway_info.update({
            "cached": True,
            "cache_age": cache_age,
            "last_error": _safe_error(error),
            "client_version": __version__,
            "pool_id": self._pool_id,
        })
        cached["gateway"] = gateway_info
        with self._lock:
            self._sequence += 1
            cached["gateway_sequence"] = self._sequence
        self._restore_routes(cached)
        return cached

    def fetch_state(self) -> tuple[bool, dict]:
        """Return the first valid gateway state, racing backups when needed."""
        candidates = self._candidate_gateways()
        results: queue.Queue = queue.Queue()
        started: set[str] = set()
        completed = 0
        timeout = float(self.settings["state_timeout"])
        deadline = self._clock() + timeout + 0.5

        def launch(gateway: str) -> None:
            if gateway in started:
                return
            started.add(gateway)
            with self._lock:
                self._last_probe[gateway] = self._clock()

            def worker() -> None:
                try:
                    response = self._rpc_gateway(
                        gateway, {"action": "state"}, timeout)
                    state = response.get("state")
                    host = response.get("host")
                    if not response.get("ok") or not isinstance(state, dict):
                        raise GatewayError(
                            _safe_error(response.get("reason"))
                            or "gateway has no daemon state")
                    if not isinstance(state.get("nodes"), dict):
                        raise GatewayError("gateway returned invalid daemon state")
                    results.put((gateway, state,
                                 host if isinstance(host, str) else gateway,
                                 (response.get("version")
                                  if (isinstance(response.get("version"), str)
                                      and re.fullmatch(
                                          r"[A-Za-z0-9._+-]{1,64}",
                                          response.get("version")))
                                  else ""),
                                 response.get("keepalive_entries"), None))
                except Exception as error:
                    results.put((gateway, None, "", "", None, error))

            threading.Thread(target=worker, daemon=True,
                             name=f"gateway-state-{gateway}").start()

        if not candidates:
            return True, self._cached_or_empty_state("no configured gateway is eligible")
        launch(candidates[0])
        remaining = list(candidates[1:])
        first_wait = min(float(self.settings["hedge_delay"]), timeout)
        last_error = ""
        while self._clock() < deadline:
            wait_for = max(0.01, min(
                deadline - self._clock(),
                first_wait if remaining else deadline - self._clock()))
            try:
                (gateway, remote_state, remote_host, agent_version,
                 entries, error) = results.get(
                    timeout=wait_for)
            except queue.Empty:
                if remaining:
                    for backup in remaining:
                        launch(backup)
                    remaining.clear()
                    first_wait = timeout
                    continue
                break
            completed += 1
            if error is None:
                try:
                    decorated = self._decorate_state(
                        remote_state, gateway, remote_host,
                        agent_version, entries)
                except Exception as decorate_error:
                    last_error = _safe_error(decorate_error)
                else:
                    self._set_active(gateway)
                    with self._lock:
                        current_sequence = self._last_state.get(
                            "gateway_sequence", -1)
                        if (not isinstance(current_sequence, int)
                                or decorated["gateway_sequence"]
                                >= current_sequence):
                            self._last_state = copy.deepcopy(decorated)
                        self._last_error = ""
                    self._cache_state(decorated)
                    self._schedule_backup_probes(gateway)
                    return True, decorated
            else:
                last_error = _safe_error(error)
            if remaining:
                for backup in remaining:
                    launch(backup)
                remaining.clear()
            if (not remaining and len(started) == len(candidates)
                    and completed >= len(started)):
                break
        if not last_error:
            last_error = "all gateway state requests timed out"
        with self._lock:
            self._last_error = last_error
        return True, self._cached_or_empty_state(last_error)

    def read_snapshots(self) -> tuple[bool, dict]:
        with self._lock:
            return True, copy.deepcopy(self._snapshots)

    def _store_preview(self, node: str, session: str, content: str,
                       captured_epoch=None) -> None:
        encoded = content.encode("utf-8", "surrogatepass")
        if len(encoded) > _SNAPSHOT_ENTRY_LIMIT:
            content = encoded[-_SNAPSHOT_ENTRY_LIMIT:].decode(
                "utf-8", "replace")
        epoch = (float(captured_epoch)
                 if isinstance(captured_epoch, (int, float)) else self._wall_clock())
        if not math.isfinite(epoch):
            epoch = self._wall_clock()
        entry = {
            "lines": content,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "captured_epoch": epoch,
            "captured_monotonic": self._clock(),
            "monotonic_clock_id": lifecycle.monotonic_clock_id(),
        }
        with self._lock:
            self._snapshots[f"{node}:{session}"] = entry
            ordered = sorted(
                self._snapshots,
                key=lambda key: float(
                    self._snapshots[key].get("captured_epoch") or 0))
            approximate_bytes = sum(
                len(str(value.get("lines") or "").encode(
                    "utf-8", "surrogatepass")) + 512
                for value in self._snapshots.values())
            while (ordered and (len(self._snapshots) > 256
                                or approximate_bytes
                                > _SNAPSHOT_CACHE_LIMIT - 64 * 1024)):
                oldest = ordered.pop(0)
                removed = self._snapshots.pop(oldest, {})
                approximate_bytes -= (
                    len(str(removed.get("lines") or "").encode(
                        "utf-8", "surrogatepass")) + 512)
            snapshot = copy.deepcopy(self._snapshots)
        with self._cache_lock:
            try:
                _atomic_write_json(
                    paths.GATEWAY_SNAPSHOT_CACHE, snapshot,
                    _SNAPSHOT_CACHE_LIMIT)
            except Exception:
                pass

    def _rpc_failover(self, payload: dict, *, node: str | None = None,
                      retry_unavailable: bool = False) -> dict:
        route = self._route_for(node) if node else Route(None, "", False)
        candidates = self._candidate_gateways(
            route.gateway if route.fixed else None)
        last_error = "no gateway is available"
        deadline = self._clock() + float(self.settings["state_timeout"])
        for index, gateway in enumerate(candidates):
            remaining = deadline - self._clock()
            if remaining <= 0.1:
                last_error = "gateway operation timed out"
                break
            attempts_left = max(1, len(candidates) - index)
            attempt_timeout = max(0.1, remaining / attempts_left)
            request = dict(payload)
            if node is not None:
                request["node"] = route.target
            try:
                response = self._rpc_gateway(
                    gateway, request, attempt_timeout)
            except Exception as error:
                last_error = _safe_error(error)
                continue
            response = dict(response)
            response["gateway"] = gateway
            if response.get("ok"):
                self._set_active(gateway)
                return response
            last_error = _safe_error(response.get("reason")) or "gateway operation failed"
            if not retry_unavailable or response.get("kind") not in {
                    "busy", "backoff", "unavailable"}:
                return response
        return {"ok": False, "protocol": PROTOCOL_VERSION,
                "kind": "unavailable", "reason": last_error,
                "retry_after": float(self.settings["backoff_base"])}

    def preview(self, node: str, session: str) -> dict:
        if (not isinstance(session, str) or not session
                or len(session.encode("utf-8", "surrogatepass")) > 4096):
            return {"ok": False, "kind": "invalid",
                    "reason": "invalid tmux session"}
        if node == "localhost":
            try:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-e", "-t", session],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=min(
                        8.0, float(self.settings["state_timeout"])))
            except subprocess.TimeoutExpired:
                return {"ok": False, "kind": "unavailable",
                        "reason": "local tmux preview timed out",
                        "retry_after": 1.0}
            except OSError as error:
                return {"ok": False, "kind": "unavailable",
                        "reason": _safe_error(error), "retry_after": 2.0}
            if result.returncode != 0:
                return {"ok": False, "kind": "not-found",
                        "reason": _safe_error(result.stderr)
                                  or "local tmux session no longer exists"}
            response = {"ok": True, "content": result.stdout or "",
                        "captured_epoch": self._wall_clock(),
                        "gateway": "local"}
            self._store_preview(
                node, session, response["content"],
                response["captured_epoch"])
            return response
        response = self._rpc_failover(
            {"action": "preview", "session": session}, node=node,
            retry_unavailable=True)
        content = response.get("content")
        if response.get("ok") and isinstance(content, str):
            self._store_preview(
                node, session, content, response.get("captured_epoch"))
        return response

    def keepalive_entries(self, require_fresh: bool = False) -> list[dict]:
        with self._lock:
            cached = copy.deepcopy(self._keepalive_entries)
            known = self._keepalive_known
        if known and not require_fresh:
            return cached
        response = self._rpc_failover({"action": "keepalive-list"})
        entries = response.get("entries")
        if not response.get("ok") or not isinstance(entries, list):
            raise GatewayError(_safe_error(response.get("reason"))
                               or "could not read keep-alive registry")
        enabled = [dict(entry) for entry in entries
                   if isinstance(entry, dict) and entry.get("enabled")]
        with self._lock:
            self._keepalive_entries = copy.deepcopy(enabled)
            self._keepalive_known = True
        return enabled

    def scontrol_job(self, job_id: str) -> dict | None:
        response = self._rpc_failover(
            {"action": "scontrol", "job_id": str(job_id)})
        info = response.get("info")
        return dict(info) if response.get("ok") and isinstance(info, dict) else None

    def set_keepalive(self, job_name: str, enabled: bool,
                      command: str = "", workdir: str = "", *,
                      job_id=None, entry_id=None) -> bool:
        response = self._rpc_failover({
            "action": "keepalive-set",
            "job_name": job_name,
            "enabled": bool(enabled),
            "command": command,
            "workdir": workdir,
            "job_id": job_id,
            "entry_id": entry_id,
        })
        if not response.get("ok"):
            raise GatewayError(_safe_error(response.get("reason"))
                               or "could not update keep-alive registry")
        return bool(response.get("enabled"))

    def report_async(self, node: str, outcome: str,
                     reason: str, source: str) -> None:
        if node == "localhost":
            return

        def worker() -> None:
            try:
                self._rpc_failover({
                    "action": "report", "outcome": outcome,
                    "reason": _safe_error(reason, 200),
                    "source": _safe_error(source, 40),
                }, node=node)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="gateway-network-report").start()

    def _master_alive(self, gateway: str) -> bool:
        argv = [
            "ssh", "-o", f"ControlPath={self._control_path(gateway)}",
            "-O", "check", gateway,
        ]
        try:
            result = subprocess.run(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def authenticate(self) -> list[dict]:
        """Interactively establish one persistent SSH master per gateway.

        Background RPCs intentionally use BatchMode=yes so an MFA/password
        prompt can never corrupt the TUI.  This explicit bootstrap command is
        the one place where OpenSSH is allowed to prompt the user's terminal.
        """
        results = []
        for index, gateway in enumerate(self._gateways, 1):
            if self._master_alive(gateway):
                results.append({"gateway": gateway, "ok": True,
                                "existing": True})
                continue
            print(f"[atmux] authenticating gateway {index}/{len(self._gateways)}: "
                  f"{gateway}", flush=True)
            argv = [
                "ssh",
                "-o", "BatchMode=no",
                "-o", f"ConnectTimeout={int(self.settings['connect_timeout'])}",
                "-o", "ConnectionAttempts=1",
                "-o", f"ServerAliveInterval={int(self.settings['server_alive_int'])}",
                "-o", f"ServerAliveCountMax={int(self.settings['server_alive_max'])}",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ControlMaster=auto",
                "-o", f"ControlPersist={int(self.settings['control_persist'])}",
                "-o", f"ControlPath={self._control_path(gateway)}",
                "-fN", gateway,
            ]
            try:
                returncode = subprocess.call(argv)
                error = "" if returncode == 0 else f"ssh exited with status {returncode}"
            except OSError as exc:
                returncode = 127
                error = f"ssh: {exc.strerror or exc}"
            ok = returncode == 0 and self._master_alive(gateway)
            if returncode == 0 and not ok:
                deadline = self._clock() + 1.0
                while self._clock() < deadline and not ok:
                    time.sleep(0.05)
                    ok = self._master_alive(gateway)
            if ok:
                self._record_transport_alive(gateway)
            else:
                self._record_failure(gateway, error or "SSH master did not start")
            results.append({"gateway": gateway, "ok": ok,
                            "existing": False, "error": error})
        return results

    def check_all(self) -> list[dict]:
        """Probe all configured agents concurrently for diagnostics."""
        output: queue.Queue = queue.Queue()

        def worker(name: str) -> None:
            started = self._clock()
            try:
                response = self._rpc_gateway(
                    name, {"action": "ping"},
                    float(self.settings["state_timeout"]))
                if not response.get("ok"):
                    raise GatewayError(
                        _safe_error(response.get("reason")) or "agent rejected ping")
                output.put({
                    "gateway": name, "ok": True,
                    "latency_ms": max(0.0, (self._clock() - started) * 1000),
                    "host": response.get("host") or "",
                    "version": response.get("version") or "",
                })
            except Exception as error:
                output.put({"gateway": name, "ok": False,
                            "error": _safe_error(error)})

        for name in self._gateways:
            threading.Thread(target=worker, args=(name,), daemon=True,
                             name=f"gateway-check-{name}").start()
        results = []
        deadline = self._clock() + float(self.settings["state_timeout"]) + 0.5
        while len(results) < len(self._gateways) and self._clock() < deadline:
            try:
                results.append(output.get(timeout=max(
                    0.01, deadline - self._clock())))
            except queue.Empty:
                break
        seen = {item["gateway"] for item in results}
        for name in self._gateways:
            if name not in seen:
                results.append({"gateway": name, "ok": False,
                                "error": "probe timed out"})
        order = {name: index for index, name in enumerate(self._gateways)}
        return sorted(results, key=lambda item: order[item["gateway"]])

    @staticmethod
    def _session_from_remote_args(remote_args: list[str] | None) -> tuple[str, str | None]:
        if remote_args is None:
            return "shell", None
        if len(remote_args) == 4 and remote_args[:3] == ["tmux", "attach", "-t"]:
            try:
                parsed = shlex.split(remote_args[3])
            except ValueError as error:
                raise ValueError("invalid tmux attach arguments") from error
            if len(parsed) == 1 and parsed[0]:
                return "attach", parsed[0]
        raise ValueError("unsupported remote interactive command")

    def _interactive_once(self, gateway: str, token: str,
                          *, direct: bool = False) -> tuple[int, str]:
        argv = self._ssh_argv(gateway, tty=True, direct=direct)
        argv.append(self._agent_command("interactive", token))
        try:
            return subprocess.call(argv), ""
        except OSError as error:
            return 127, f"ssh: {error.strerror or error}"

    def run_interactive(self, node: str, remote_args: list[str] | None,
                        *, direct: bool = False) -> tuple[int, str, bool]:
        kind, session = self._session_from_remote_args(remote_args)
        route = self._route_for(node)
        if route.target == "localhost" and route.gateway is None:
            raise ValueError("local routes must not use gateway SSH")
        token = encode_interactive_token(route.target, kind, session)
        candidates = self._interactive_candidates(
            route.target, route.gateway if route.fixed else None)
        if not candidates:
            return 255, "no login gateway is available", direct
        last_error = "all login gateways failed"
        final_returncode = 255
        for index, gateway in enumerate(candidates, 1):
            mode = "direct" if direct else "multiplexed"
            print(
                f"\n[atmux] gateway {index}/{len(candidates)}: {gateway} "
                f"({mode}) → {route.target}…", flush=True)
            returncode, error = self._interactive_once(
                gateway, token, direct=direct)
            if returncode == 255 and not direct:
                print(
                    f"[atmux] {gateway} transport failed; retrying it once "
                    "without the local ControlMaster…", flush=True)
                returncode, error = self._interactive_once(
                    gateway, token, direct=True)
                direct = True
            if returncode not in {126, 127, 255}:
                self._record_transport_alive(gateway)
                self._record_route_success(gateway, route.target)
                self._set_active(gateway)
                return returncode, error, direct
            self._record_route_failure(
                gateway, route.target, error or "interactive route failed")
            final_returncode = returncode
            last_error = (error or
                          (f"gateway {gateway} disconnected" if returncode == 255
                           else f"atmux-agent unavailable on {gateway} "
                                f"(status {returncode})"))
            direct = False
            if index < len(candidates):
                print(
                    f"[atmux] {gateway} disconnected; failing over to the "
                    "next login node…", flush=True)
        return final_returncode, last_error, direct

    def status(self) -> dict:
        return self._health_payload()
