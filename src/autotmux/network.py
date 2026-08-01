"""Per-node network admission and circuit breaking.

The SSH ControlMaster has a small, shared channel budget.  Session polling,
pane snapshots and live previews must therefore coordinate by *node*, not by
call site.  This module is deliberately independent of the daemon so its state
machine can be fault-tested without starting threads or SSH processes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
import time


_DEFAULT_DELAYS = (2.0, 4.0, 8.0, 16.0, 30.0, 60.0)


@dataclass
class NodeNetworkLease:
    """One admitted background operation for a node."""

    coordinator: "NodeNetworkCoordinator"
    node: str
    source: str
    gate: threading.Lock | None
    _finished: bool = False

    def success(self) -> None:
        if not self._finished:
            self._finished = True
            self.coordinator._finish(self, True, "")

    def failure(self, reason: str) -> None:
        if not self._finished:
            self._finished = True
            self.coordinator._finish(self, False, reason)

    def neutral(self) -> None:
        """Release capacity without changing reachability state."""
        if not self._finished:
            self._finished = True
            self.coordinator._finish(self, None, "")

    def __enter__(self) -> "NodeNetworkLease":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        if not self._finished:
            if exc_type is None:
                self.success()
            else:
                self.neutral()


class NodeNetworkCoordinator:
    """Shared circuit breaker plus one background SSH channel per node.

    The first transport failure marks a node suspect and briefly pauses other
    background work.  Repeated failures open the circuit with exponential,
    deterministically-jittered backoff.  Once the delay expires exactly one
    half-open probe is admitted; a successful probe closes the circuit.

    Interactive attaches do not acquire these leases.  They inspect the
    published state and may bypass a suspect ControlMaster without being held
    behind snapshots or polling.
    """

    def __init__(self, delays=_DEFAULT_DELAYS, clock=time.monotonic) -> None:
        cleaned = tuple(float(value) for value in delays if float(value) >= 0)
        if not cleaned:
            raise ValueError("at least one non-negative circuit delay is required")
        self._delays = cleaned
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._gates: dict[str, threading.Lock] = {}

    @staticmethod
    def _jitter(node: str, failures: int) -> float:
        digest = hashlib.sha256(
            f"{node}\0{failures}".encode("utf-8", "surrogatepass")
        ).digest()
        # Stable 0.90..1.10 multiplier avoids a reconnect stampede while
        # keeping tests and status output reproducible.
        return 0.90 + int.from_bytes(digest[:2], "big") / 65535.0 * 0.20

    def _entry_locked(self, node: str) -> dict:
        return self._entries.setdefault(node, {
            "state": "healthy",
            "failures": 0,
            "retry_at": 0.0,
            "reason": "",
            "source": "",
            "last_success": None,
            "last_failure": None,
        })

    def acquire(self, node: str, source: str) -> NodeNetworkLease | None:
        if node == "localhost":
            return NodeNetworkLease(self, node, source, None)
        now = self._clock()
        with self._lock:
            entry = self._entry_locked(node)
            if entry["failures"] and now < entry["retry_at"]:
                return None
            gate = self._gates.setdefault(node, threading.Lock())
            if not gate.acquire(blocking=False):
                return None
            if entry["failures"]:
                entry["state"] = "half-open"
            return NodeNetworkLease(self, node, source, gate)

    def _finish(self, lease: NodeNetworkLease,
                success: bool | None, reason: str) -> None:
        now = self._clock()
        try:
            if lease.node != "localhost" and success is not None:
                with self._lock:
                    entry = self._entry_locked(lease.node)
                    if success:
                        entry.update({
                            "state": "healthy",
                            "failures": 0,
                            "retry_at": 0.0,
                            "reason": "",
                            "source": lease.source,
                            "last_success": now,
                        })
                    else:
                        failures = int(entry.get("failures", 0)) + 1
                        base = self._delays[min(failures - 1,
                                                len(self._delays) - 1)]
                        delay = base * self._jitter(lease.node, failures)
                        entry.update({
                            "state": "suspect" if failures == 1 else "offline",
                            "failures": failures,
                            "retry_at": now + delay,
                            "reason": " ".join(str(reason).split())[:200],
                            "source": lease.source,
                            "last_failure": now,
                        })
        finally:
            if lease.gate is not None:
                try:
                    lease.gate.release()
                except RuntimeError:
                    pass

    def report_success(self, node: str, source: str) -> None:
        lease = NodeNetworkLease(self, node, source, None)
        lease.success()

    def report_failure(self, node: str, source: str, reason: str) -> None:
        lease = NodeNetworkLease(self, node, source, None)
        lease.failure(reason)

    def drop(self, node: str) -> None:
        with self._lock:
            self._entries.pop(node, None)
            gate = self._gates.get(node)
            if gate is not None and not gate.locked():
                self._gates.pop(node, None)

    def snapshot(self, node: str) -> dict:
        if node == "localhost":
            return {
                "state": "healthy", "failures": 0, "retry_in": 0.0,
                "reason": "", "source": "local", "busy": False,
            }
        now = self._clock()
        with self._lock:
            entry = dict(self._entry_locked(node))
            gate = self._gates.get(node)
        return {
            "state": entry["state"],
            "failures": int(entry["failures"]),
            "retry_in": max(0.0, float(entry["retry_at"]) - now),
            "reason": entry["reason"],
            "source": entry["source"],
            "busy": bool(gate and gate.locked()),
            "last_success_age": (
                None if entry["last_success"] is None
                else max(0.0, now - float(entry["last_success"]))),
            "last_failure_age": (
                None if entry["last_failure"] is None
                else max(0.0, now - float(entry["last_failure"]))),
        }
