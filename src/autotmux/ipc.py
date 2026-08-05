"""Small bounded JSON protocol over the daemon's private Unix socket."""

from __future__ import annotations

import json
import os
import socket
import stat


MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _encoded(value, limit: int) -> bytes:
    raw = json.dumps(value, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > limit:
        raise ValueError(f"IPC message exceeds {limit} bytes")
    return raw


def send_json(sock: socket.socket, value, limit: int) -> None:
    sock.sendall(_encoded(value, limit))


def recv_json(sock: socket.socket, limit: int):
    chunks = []
    total = 0
    while True:
        chunk = sock.recv(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ValueError(f"IPC message exceeds {limit} bytes")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks)
    line, separator, trailing = raw.partition(b"\n")
    if not separator or trailing:
        raise ValueError("malformed IPC frame")
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("IPC payload must be an object")
    return value


def request(path: str, payload: dict, timeout: float = 10.0) -> dict:
    """Send one request after verifying the private daemon socket."""
    st = os.lstat(path)
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"unsafe preview socket {path!r}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.1, float(timeout)))
        client.connect(path)
        send_json(client, payload, MAX_REQUEST_BYTES)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            # Half-closing is a courtesy to the peer, not part of the protocol:
            # it says "no more request bytes are coming". A daemon that has
            # already answered and closed makes this fail with ENOTCONN, and
            # failing the whole request there would throw away a reply that is
            # sitting in the receive buffer, readable. Whether it happens comes
            # down to scheduling, so it shows up as a rare, load-dependent
            # error rather than an obvious one.
            pass
        return recv_json(client, MAX_RESPONSE_BYTES)
