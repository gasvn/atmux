"""Serve the AutoTmux TUI itself to a browser.

This is not a second dashboard. It runs the real ``atmux`` in a pseudo-terminal
and streams that terminal to xterm.js, so every key, every layout mode and
every future feature arrives in the browser for free -- including ``attach``,
which works precisely because the app really does own a terminal here. A
browser-native reimplementation would have to reproduce all of that and would
then have to keep reproducing it.

Deliberately dependency-free. A single user with a phone and a tablet does not
need an ASGI stack, and the stdlib already has everything: ``pty`` for the
terminal, ``http.server`` for the page, and a WebSocket that fits in a hundred
lines because only two frame types matter here.

Bind to loopback and publish with ``tailscale serve``. Nothing here
authenticates a caller, so anything that can open the socket owns a shell.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import http.server
import ipaddress
import json
import os
import pty
import re
import select
import signal
import shutil
import socket
import struct
import sys
import termios
import threading
import urllib.parse

from . import config
from . import keypad
from . import paths
from . import statesource

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webassets')

# RFC 6455: the server proves it understood the handshake by hashing the
# client's key with this constant. It is not a secret and not negotiable.
_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

_OP_CONT, _OP_TEXT, _OP_BINARY, _OP_CLOSE, _OP_PING, _OP_PONG = (
    0x0, 0x1, 0x2, 0x8, 0x9, 0xA)

# One screen of a very large terminal is far below this; the cap only stops a
# malformed or hostile frame from asking for an unbounded allocation.
MAX_FRAME_BYTES = 4 * 1024 * 1024
# Bigger than any single PTY read, so output is forwarded whole rather than in
# fragments the terminal has to reassemble mid-escape-sequence.
PTY_READ_BYTES = 65536

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 7681

# Tailscale hands every node an address in the CGNAT range, and nothing
# outside the tailnet can route to it. Binding there is the same security
# posture as loopback behind `tailscale serve` -- the same peers reach it
# either way -- and it is the answer when serve itself is unavailable, which
# it is after a tailnet rename until tailscaled is restarted.
_TAILNET_V4 = ipaddress.ip_network('100.64.0.0/10')


def is_private_bind(host: str) -> bool:
    """Whether binding here keeps the terminal off the public internet."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in ('localhost',)
    if address.is_loopback:
        return True
    if address.version == 4 and address in _TAILNET_V4:
        return True
    # Tailscale's IPv6 range, and ordinary link-local.
    return address.is_link_local or (
        address.version == 6 and address in ipaddress.ip_network('fd7a:115c:a1e0::/48'))


# ── WebSocket framing ─────────────────────────────────────────────────────

def accept_key(client_key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value for a client's key."""
    digest = hashlib.sha1(client_key.strip().encode('ascii') + _WS_GUID)
    return base64.b64encode(digest.digest()).decode('ascii')


def encode_frame(payload: bytes, opcode: int = _OP_BINARY) -> bytes:
    """One unmasked server frame. Server frames must never be masked."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack('!H', length)
    else:
        header.append(127)
        header += struct.pack('!Q', length)
    return bytes(header) + payload


class FrameReader:
    """Decodes client frames from a stream of bytes.

    Fragmented messages are joined, control frames are surfaced separately, and
    a frame larger than ``MAX_FRAME_BYTES`` closes the connection rather than
    being buffered.
    """

    def __init__(self, limit: int = MAX_FRAME_BYTES) -> None:
        self._buffer = bytearray()
        self._message = bytearray()
        self._limit = limit

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Return the ``(opcode, payload)`` messages completed by ``data``."""
        self._buffer += data
        out: list[tuple[int, bytes]] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                return out
            fin, opcode, payload = frame
            if opcode in (_OP_CLOSE, _OP_PING, _OP_PONG):
                out.append((opcode, payload))
                continue
            if opcode == _OP_CONT:
                self._message += payload
            else:
                self._message = bytearray(payload)
                self._opcode = opcode
            if len(self._message) > self._limit:
                raise ValueError('websocket message too large')
            if fin:
                out.append((getattr(self, '_opcode', _OP_BINARY),
                            bytes(self._message)))
                self._message = bytearray()

    def _take_frame(self):
        buf = self._buffer
        if len(buf) < 2:
            return None
        fin = bool(buf[0] & 0x80)
        opcode = buf[0] & 0x0F
        masked = bool(buf[1] & 0x80)
        length = buf[1] & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < offset + 2:
                return None
            length = struct.unpack('!H', buf[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return None
            length = struct.unpack('!Q', buf[offset:offset + 8])[0]
            offset += 8
        if length > self._limit:
            raise ValueError('websocket frame too large')
        mask = b''
        if masked:
            if len(buf) < offset + 4:
                return None
            mask = bytes(buf[offset:offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = bytes(buf[offset:offset + length])
        del self._buffer[:offset + length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload


# ── the pseudo-terminal ───────────────────────────────────────────────────

class Terminal:
    """A command running on its own pty, resizable and readable."""

    def __init__(self, argv: list[str], env: dict | None = None,
                 cols: int = 80, rows: int = 24) -> None:
        self.argv = list(argv)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                                   # child
            try:
                os.environ.update(env or {})
                os.environ.setdefault('TERM', 'xterm-256color')
                # The browser is the only terminal here; a nested tmux would
                # try to take over the same pty and fight the app for it.
                os.environ.pop('TMUX', None)
                os.execvp(self.argv[0], self.argv)
            except BaseException:
                os._exit(127)
        self.resize(cols, rows)
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def resize(self, cols, rows) -> None:
        """Tell the pty its size, which is how tmux learns the browser's.

        Ignores anything it cannot make sense of. The values arrive from the
        page as JSON, so a malformed one is a message to drop -- not a reason
        to tear down a session someone is working in.
        """
        try:
            cols = max(2, min(int(cols), 1000))
            rows = max(2, min(int(rows), 1000))
        except (TypeError, ValueError):
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', rows, cols, 0, 0))
        except OSError:
            pass

    def read(self) -> bytes:
        try:
            return os.read(self.fd, PTY_READ_BYTES)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b''
            return b''                                       # EIO == exited

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def close(self) -> None:
        for action in (
                lambda: os.kill(self.pid, signal.SIGHUP),
                lambda: os.close(self.fd),
                lambda: os.waitpid(self.pid, os.WNOHANG)):
            try:
                action()
            except OSError:
                pass


# ── HTTP + WebSocket ──────────────────────────────────────────────────────

_ASSET_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
}
_ASSET_NAME = re.compile(r'^[A-Za-z0-9._-]+$')

# `tailscale serve --set-path /term` strips the prefix before proxying and
# forwards no header saying what it was, so the server cannot know where it is
# mounted and cannot issue the redirect itself. The browser can: it is the one
# that knows the address it asked for.
#
# Without this, /term (which is the address tailscale's own output prints)
# loads the page and then resolves every relative asset against the host root,
# where nothing is mounted -- so the page comes up and stays empty.
#
# Inline because it has to run before the assets that would 404, and any file
# it lived in would 404 for the same reason. The CSP admits exactly this text
# by hash, so it is not a hole: nothing else inline can run.
BOOTSTRAP_JS = (
    "if(!location.pathname.endsWith('/')){"
    "location.replace(location.pathname+'/'+location.search+location.hash)}"
)
BOOTSTRAP_HASH = 'sha256-' + base64.b64encode(
    hashlib.sha256(BOOTSTRAP_JS.encode('utf-8')).digest()).decode('ascii')
_BOOTSTRAP_SLOT = '<!--bootstrap-->'

# The layout contract, handed to the page rather than duplicated in it. The
# client cannot know which widths the dashboard has layouts for, and guessing
# is how it settled on a font size that produced a column count fitting
# neither. A meta tag, not a script: nothing to admit through the CSP.
_LAYOUT_SLOT = '<!--layout-->'


def _layout_meta() -> str:
    widths = ','.join(str(int(width)) for width in config.LAYOUT_WIDTHS)
    return f'<meta name="atmux-layout" content="{widths}">'
# Ours, and therefore worth re-fetching. Everything else is vendored and only
# changes when the package does.
_VOLATILE_ASSETS = frozenset({'index.html', 'app.js', 'manifest.json',
                              'dash.html', 'dash.js'})

# Where the terminal lives now that the root is the dashboard. Trailing slash
# so every asset the page asks for resolves under it.
CONSOLE = '/console/'


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'atmux-web'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):                       # quieter
        if self.server.verbose:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        path = self.path.split('?', 1)[0]
        # The socket is addressed relative to whichever page opened it, so
        # the console at /console/ asks for /console/ws. One rule rather than
        # a list of mount points: the page must keep working wherever it is
        # mounted, which is the whole reason it uses a relative URL.
        if path == '/ws' or path.endswith('/ws'):
            self._websocket()
        elif path == '/api/state':
            self._state()
        elif path in ('/', '/index.html'):
            # The dashboard, not the terminal. Reading a list is what a phone
            # is here for; the terminal is for the one thing that genuinely
            # needs a pty, and lives a click away.
            self._asset('dash.html')
        elif path == CONSOLE:
            self._asset('index.html')
        elif path == CONSOLE.rstrip('/'):
            self._redirect(CONSOLE)
        elif path == '/healthz':
            self._bytes(b'{"ok":true}\n', _ASSET_TYPES['.json'])
        elif path.startswith(CONSOLE):
            self._asset(path[len(CONSOLE):])
        else:
            self._asset(path.lstrip('/'))

    def _redirect(self, where: str) -> None:
        self.send_response(302)
        self.send_header('Location', where)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _state(self) -> None:
        """The dashboard's model, as JSON.

        Served from a cached background refresh rather than fetched here: a
        fetch is an SSH round trip to every gateway, and a phone polling on a
        timer would otherwise open more connections than a person ever did.
        """
        source = self.server.state
        if source is None:
            self._bytes(b'{"error":"no state source"}\n',
                        _ASSET_TYPES['.json'])
            return
        body = json.dumps(source.snapshot(), ensure_ascii=False)
        self._bytes(body.encode('utf-8'), _ASSET_TYPES['.json'])

    def _asset(self, name: str) -> None:
        # Names come off the wire; only ever serve a plain file from one
        # directory, never a path the caller composed.
        if not _ASSET_NAME.fullmatch(name):
            self.send_error(404)
            return
        full = os.path.join(ASSETS, name)
        try:
            with open(full, 'rb') as handle:
                body = handle.read()
        except OSError:
            self.send_error(404)
            return
        if name == 'index.html':
            body = body.replace(
                _BOOTSTRAP_SLOT.encode('ascii'),
                f'<script>{BOOTSTRAP_JS}</script>'.encode('utf-8'))
            body = body.replace(_LAYOUT_SLOT.encode('ascii'),
                                _layout_meta().encode('utf-8'))
        ctype = _ASSET_TYPES.get(os.path.splitext(name)[1],
                                 'application/octet-stream')
        # The vendored terminal never changes without a reinstall, and it is
        # half a megabyte over a phone connection. Our own page does change,
        # and a browser holding a stale copy of it is indistinguishable from
        # the server being broken -- which is exactly how one blocked script
        # presented itself.
        self._bytes(body, ctype, cache=name not in _VOLATILE_ASSETS)

    def _bytes(self, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control',
                         'public, max-age=604800, immutable' if cache
                         else 'no-store')
        # The page is self-contained: everything it loads comes from here, and
        # nothing it holds should ever leave. This is a terminal.
        #
        # script-src is 'self' with no 'unsafe-inline', which is why the page's
        # JavaScript lives in app.js: an inline <script> was silently blocked
        # and the whole page came up blank. ws:/wss: are named explicitly --
        # same-origin sockets ought to fall under 'self', and Safari has a long
        # history of disagreeing.
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; "
                         f"script-src 'self' '{BOOTSTRAP_HASH}'; "
                         "style-src 'self' 'unsafe-inline'; "
                         "connect-src 'self' ws: wss:; img-src 'self' data:; "
                         "frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    # ── the bridge ────────────────────────────────────────────────────
    def _websocket(self) -> None:
        key = self.headers.get('Sec-WebSocket-Key')
        upgrade = (self.headers.get('Upgrade') or '').lower()
        if not key or upgrade != 'websocket':
            self.send_error(400, 'expected a websocket upgrade')
            return
        self.send_response(101, 'Switching Protocols')
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept_key(key))
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return
        self._pump()

    # NODE:SESSION, strictly. Anchored, bounded, and refusing a leading dash
    # so the value can never be read as an option however it is passed on.
    # This arrives in a URL, and a URL is untrusted: anyone who can reach
    # this socket can craft one.
    _ATTACH = re.compile(r'^(?![-])[A-Za-z0-9._@-]{1,120}'
                         r':(?![-])[A-Za-z0-9._@-]{1,120}$')

    def _query(self) -> dict:
        raw = self.path.split('?', 1)[1] if '?' in self.path else ''
        out = {}
        for part in raw.split('&'):
            if not part:
                continue
            key, _, value = part.partition('=')
            out[urllib.parse.unquote_plus(key)] = urllib.parse.unquote_plus(
                value)
        return out

    # What a link may ask the program to do, and the flag it becomes. Two
    # entries rather than one per action on purpose: `attach` is the verb you
    # want nine times in ten, and `select` covers every other one at once --
    # every action in the dashboard acts on the highlighted row, so landing
    # on the right row makes all of them reachable without a keyboard and
    # without a flag each.
    #
    # A whitelist, not a passthrough: this arrives in a URL.
    _VERBS = {'attach': '--attach', 'select': '--select'}

    def _client_argv(self) -> list:
        """What to run for this client: the dashboard, or one session.

        Tapping a row on the list has to land on that session. Opening the
        dashboard instead is a screen that costs a tap and answers nothing --
        it is the same list you just tapped.

        The target reaches the program as its own argv element through
        execvp, never through a shell, and never concatenated into anything.
        """
        query = self._query()
        for verb, flag in self._VERBS.items():
            target = query.get(verb, '')
            if target and self._ATTACH.match(target):
                return list(self.server.argv) + [f'{flag}={target}']
        return list(self.server.argv)
    def _client_env(self) -> dict:
        """The environment this particular client's dashboard should see.

        Who draws the controls is a property of the client, not of the
        server: a phone and a laptop reach the same process, and a laptop
        that had its footer hidden because a phone might connect would be
        left with no controls at all. The page says which it is in the
        socket URL, which is the only thing available before the pty exists.
        """
        env = dict(self.server.env)
        query = self.path.split('?', 1)[1] if '?' in self.path else ''
        if 'touch=1' in query.split('&'):
            env[keypad.TOUCH_ENV] = 'web'
        return env

    def _pump(self) -> None:
        ended = False
        conn = self.connection
        terminal = Terminal(self._client_argv(), env=self._client_env())
        reader = FrameReader()
        conn.setblocking(False)
        try:
            while True:
                ready, _, _ = select.select([conn, terminal.fd], [], [], 0.5)
                if terminal.fd in ready:
                    data = terminal.read()
                    if not data:
                        ended = True                         # the app exited
                        break
                    self._send(encode_frame(data))
                if conn in ready:
                    try:
                        chunk = conn.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    if not self._dispatch(reader.feed(chunk), terminal):
                        break
        except (OSError, ValueError):
            pass
        finally:
            terminal.close()
            try:
                # A phone drops this socket every time it locks, so the page
                # reconnects by default. It must not do that when the program
                # is simply finished -- detaching from a session would leave
                # you staring at a terminal reconnecting to nothing.
                payload = (struct.pack('!H', 1000) + b'exit') if ended else b''
                self._send(encode_frame(payload, _OP_CLOSE))
            except OSError:
                pass
            self.close_connection = True

    def _dispatch(self, messages, terminal: Terminal) -> bool:
        """Apply client messages; False means the connection should close."""
        for opcode, payload in messages:
            if opcode == _OP_CLOSE:
                return False
            if opcode == _OP_PING:
                self._send(encode_frame(payload, _OP_PONG))
            elif opcode == _OP_TEXT:
                # Text carries control messages only -- keystrokes are binary,
                # so a resize can never be confused with something typed.
                self._control(payload, terminal)
            elif opcode == _OP_BINARY:
                terminal.write(payload)
        return True

    @staticmethod
    def _control(payload: bytes, terminal: Terminal) -> None:
        try:
            message = json.loads(payload.decode('utf-8', 'replace'))
        except ValueError:
            return
        if isinstance(message, dict) and message.get('t') == 'resize':
            terminal.resize(message.get('cols', 80), message.get('rows', 24))

    def _send(self, frame: bytes) -> None:
        conn = self.connection
        sent = 0
        while sent < len(frame):
            try:
                sent += conn.send(frame[sent:])
            except BlockingIOError:
                select.select([], [conn], [], 1.0)
            except InterruptedError:
                continue


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, argv, env=None, verbose=False) -> None:
        super().__init__(address, Handler)
        self.argv = list(argv)
        self.env = dict(env or {})
        self.verbose = bool(verbose)
        # One refresh loop for every client. Started by serve(); None in the
        # tests that only exercise the transport.
        self.state = None


# ── entry point ───────────────────────────────────────────────────────────

def default_argv() -> list[str]:
    """The atmux to serve: this installation, not whatever is on PATH."""
    entry = shutil.which('atmux')
    if entry:
        return [entry]
    return [sys.executable, '-m', 'autotmux.cli']


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          argv: list[str] | None = None, verbose: bool = False) -> None:
    server = Server((host, port), argv or default_argv(), verbose=verbose)
    # One background refresh shared by every client. Started before the first
    # request so the dashboard has something to draw rather than an empty
    # list that looks like "no sessions".
    server.state = statesource.StateSource()
    server.state.start()
    where = f'http://{host}:{port}/'
    print(f'[atmux-web] serving {" ".join(server.argv)} on {where}')
    if host in ('127.0.0.1', 'localhost', '::1'):
        print('[atmux-web] loopback only — publish it with: '
              f'tailscale serve --bg {port}')
    elif is_private_bind(host):
        print(f'[atmux-web] reachable from your tailnet at {where}')
    else:
        print(f'[atmux-web] ⚠ bound to {host}: anything that can reach this '
              'port gets a shell. Prefer 127.0.0.1 + `tailscale serve`.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog='atmux-web',
        description='Serve the AutoTmux TUI to a browser over a private '
                    'network. Binds to loopback; publish with `tailscale '
                    'serve`.')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument(
        'command', nargs=argparse.REMAINDER,
        help='command to run instead of atmux (advanced)')
    args = parser.parse_args(argv)
    command = [value for value in args.command if value != '--'] or None
    serve(args.host, args.port, command, args.verbose)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
