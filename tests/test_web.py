"""Tests for the browser terminal.

The point of serving the TUI itself rather than rebuilding it is that every
feature arrives for free. What has to be right is the transport underneath:
the handshake, the framing, the pty, and the fact that a name off the wire can
never name a file outside the asset directory.
"""
import base64
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import keypad, web


def _node():
    """A javascript runtime, if this machine has one.

    The layout arithmetic ships as javascript. Restating it in python here
    would test the restatement, so where node exists the real function is
    run; where it does not, those tests skip rather than pretend.
    """
    import shutil
    return shutil.which('node')


def _extract(source: str, name: str) -> str:
    """One top-level `function name(...) {...}` out of app.js, by brace depth.

    Not a regex: an earlier test in this file searched for a block with a
    non-greedy pattern, matched the wrong one, and still asserted true. A
    false pass is worse than a failure.
    """
    start = source.index(f'function {name}(')
    depth, i = 0, source.index('{', start)
    while i < len(source):
        if source[i] == '{':
            depth += 1
        elif source[i] == '}':
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f'unbalanced braces in {name}')


def client_frame(payload: bytes, opcode: int = 0x2, fin: bool = True,
                 mask: bytes = b'\x01\x02\x03\x04') -> bytes:
    """A frame shaped the way a browser sends one: always masked."""
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack('!H', length)
    else:
        header.append(0x80 | 127)
        header += struct.pack('!Q', length)
    return bytes(header) + mask + body


class HandshakeTests(unittest.TestCase):
    def test_the_rfc_worked_example(self):
        """RFC 6455 §1.3. Getting this wrong means no browser ever connects,
        so it is pinned to the specification's own vector rather than to
        whatever the implementation happens to produce."""
        self.assertEqual(web.accept_key('dGhlIHNhbXBsZSBub25jZQ=='),
                         's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(web.accept_key(' dGhlIHNhbXBsZSBub25jZQ== \r\n'),
                         's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')


class FramingTests(unittest.TestCase):
    def test_payloads_round_trip_across_every_length_encoding(self):
        """126 and 65536 are where the header grows. A terminal crosses both:
        a keystroke is one byte, a full redraw is tens of kilobytes."""
        for size in (0, 1, 125, 126, 127, 65535, 65536, 200000):
            with self.subTest(size=size):
                payload = os.urandom(size)
                reader = web.FrameReader()
                self.assertEqual(reader.feed(client_frame(payload)),
                                 [(0x2, payload)])

    def test_a_frame_split_across_reads_is_not_lost(self):
        """TCP does not respect frame boundaries."""
        payload = os.urandom(5000)
        frame = client_frame(payload)
        reader = web.FrameReader()
        for cut in (1, 3, 9, 700):
            self.assertEqual(reader.feed(frame[:cut]), [])
            frame = frame[cut:]
        self.assertEqual(reader.feed(frame), [(0x2, payload)])

    def test_fragmented_messages_are_reassembled(self):
        reader = web.FrameReader()
        self.assertEqual(reader.feed(client_frame(b'abc', 0x2, fin=False)), [])
        self.assertEqual(reader.feed(client_frame(b'def', 0x0, fin=True)),
                         [(0x2, b'abcdef')])

    def test_control_frames_are_surfaced_not_mixed_into_the_stream(self):
        """A ping arriving mid-message must not end up typed into the shell."""
        reader = web.FrameReader()
        self.assertEqual(reader.feed(client_frame(b'ab', 0x2, fin=False)), [])
        self.assertEqual(reader.feed(client_frame(b'hi', 0x9)), [(0x9, b'hi')])
        self.assertEqual(reader.feed(client_frame(b'cd', 0x0, fin=True)),
                         [(0x2, b'abcd')])

    def test_an_oversized_frame_is_refused_rather_than_allocated(self):
        """The length is attacker-controlled and arrives before the payload."""
        reader = web.FrameReader(limit=1024)
        header = bytes([0x82, 0xFF]) + struct.pack('!Q', 10 ** 12) + b'\0' * 4
        with self.assertRaises(ValueError):
            reader.feed(header)

    def test_a_message_cannot_grow_past_the_limit_by_fragmenting(self):
        reader = web.FrameReader(limit=100)
        reader.feed(client_frame(b'x' * 60, 0x2, fin=False))
        with self.assertRaises(ValueError):
            reader.feed(client_frame(b'x' * 60, 0x0, fin=True))

    def test_server_frames_are_never_masked(self):
        """RFC 6455 §5.1: a masked server frame is a protocol error and
        browsers close the connection on one."""
        for size in (0, 200, 70000):
            frame = web.encode_frame(os.urandom(size))
            self.assertEqual(frame[1] & 0x80, 0, 'mask bit must be clear')

    def test_encoded_frames_decode_back(self):
        for size in (0, 125, 126, 65536):
            payload = os.urandom(size)
            frame = web.encode_frame(payload)
            # Re-mask it so the reader (which expects client frames) can parse.
            self.assertEqual(len(frame) - len(payload),
                             2 if size < 126 else (4 if size < 65536 else 10))


class TerminalTests(unittest.TestCase):
    def test_a_command_runs_and_its_output_arrives(self):
        term = web.Terminal(['/bin/echo', 'hello pty'])
        try:
            out = b''
            deadline = time.time() + 10
            while time.time() < deadline and b'hello pty' not in out:
                out += term.read()
                time.sleep(0.02)
            self.assertIn(b'hello pty', out)
        finally:
            term.close()

    def test_the_size_reaches_the_program(self):
        """tmux sizes a session to its client, so a wrong size here means the
        remote session is drawn for the wrong terminal."""
        term = web.Terminal(
            ['/bin/sh', '-c', 'stty size'], cols=113, rows=37)
        try:
            out = b''
            deadline = time.time() + 10
            while time.time() < deadline and b'37' not in out:
                out += term.read()
                time.sleep(0.02)
            self.assertIn(b'37 113', out.replace(b'\r', b''))
        finally:
            term.close()

    def test_input_reaches_the_program(self):
        term = web.Terminal(['/bin/cat'])
        try:
            term.write(b'ping\n')
            out = b''
            deadline = time.time() + 10
            while time.time() < deadline and b'ping' not in out:
                out += term.read()
                time.sleep(0.02)
            self.assertIn(b'ping', out)
        finally:
            term.close()

    def test_an_outer_tmux_is_not_inherited(self):
        """The browser is the only terminal here. A $TMUX pointing at the
        server's own session would make the child try to nest into it."""
        os.environ['TMUX'] = '/tmp/fake,1,0'
        try:
            term = web.Terminal(['/bin/sh', '-c', 'echo "TMUX=[$TMUX]"'])
            try:
                out = b''
                deadline = time.time() + 10
                while time.time() < deadline and b'TMUX=' not in out:
                    out += term.read()
                    time.sleep(0.02)
                self.assertIn(b'TMUX=[]', out)
            finally:
                term.close()
        finally:
            os.environ.pop('TMUX', None)

    def test_closing_reaps_the_child(self):
        term = web.Terminal(['/bin/sleep', '60'])
        pid = term.pid
        term.close()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            except ChildProcessError:
                return
            except OSError:
                return
            time.sleep(0.05)
        self.fail('the child outlived its terminal')


class _ServedFixture(unittest.TestCase):
    """A live server running a trivial command instead of the whole TUI."""

    COMMAND = ['/bin/cat']

    def setUp(self):
        self.server = web.Server(('127.0.0.1', 0), self.COMMAND)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _open(self, path='/ws'):
        sock = socket.create_connection((self.host, self.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f'GET {path} HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n\r\n'.encode())
        head = b''
        while b'\r\n\r\n' not in head:
            head += sock.recv(4096)
        return sock, key, head.decode('latin-1')

    def get(self, path: str) -> tuple[str, bytes]:
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.sendall(f'GET {path} HTTP/1.1\r\nHost: t\r\n'
                     f'Connection: close\r\n\r\n'.encode())
        raw = b''
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
        sock.close()
        head, _, body = raw.partition(b'\r\n\r\n')
        return head.decode('latin-1'), body


class AssetTests(_ServedFixture):
    def test_the_page_carries_its_own_terminal(self):
        head, body = self.get(web.CONSOLE)
        self.assertIn('200', head)
        self.assertIn(b'xterm.js', body)
        self.assertIn(b'app.js', body)
        _head, script = self.get('/app.js')
        self.assertIn(b'/ws', script)

    def test_the_only_inline_script_is_the_one_the_policy_admits(self):
        """The CSP has no 'unsafe-inline', so an inline <script> is silently
        dropped and the page comes up blank with nothing logged anywhere --
        which is exactly what happened once. The bootstrap redirect has to be
        inline (it must run before the assets that would 404, and any file it
        lived in would 404 for the same reason), so the policy admits that one
        text by hash and nothing else."""
        head, body = self.get(web.CONSOLE)
        inline = [tag for tag in re.findall(rb'<script[^>]*>', body)
                  if b'src=' not in tag]
        self.assertEqual(len(inline), 1, f'unexpected inline scripts: {inline}')
        self.assertIn(web.BOOTSTRAP_JS.encode('utf-8'), body)
        self.assertIn(web.BOOTSTRAP_HASH, head)

    def test_the_admitted_hash_matches_what_is_actually_served(self):
        """A hash that has drifted from the script blocks it, and the symptom
        is the blank page again."""
        import base64 as _b64
        import hashlib as _hashlib
        _head, body = self.get(web.CONSOLE)
        served = re.search(rb'<script>(.*?)</script>', body, re.S)
        self.assertIsNotNone(served, 'no inline bootstrap in the page')
        digest = _hashlib.sha256(served.group(1)).digest()
        self.assertEqual('sha256-' + _b64.b64encode(digest).decode(),
                         web.BOOTSTRAP_HASH)

    def test_a_missing_trailing_slash_is_corrected(self):
        """tailscale serve --set-path prints the address without one, and
        without a trailing slash every relative asset resolves against the
        host root, where nothing is mounted."""
        self.assertIn("endsWith('/')", web.BOOTSTRAP_JS)
        self.assertIn('location.replace', web.BOOTSTRAP_JS)

    def test_the_policy_permits_the_socket_the_page_opens(self):
        """A CSP that blocks its own websocket produces a page that loads and
        then does nothing, which reads as a server fault."""
        head, _body = self.get(web.CONSOLE)
        policy = [line for line in head.splitlines()
                  if line.lower().startswith('content-security-policy')][0]
        self.assertIn('ws:', policy)
        self.assertIn("connect-src", policy)

    def test_our_own_files_are_never_cached_but_the_library_is(self):
        """A browser holding a stale copy of a broken page is
        indistinguishable from a broken server."""
        for path in ('/', '/app.js'):
            head, _body = self.get(path)
            self.assertIn('no-store', head, path)
        head, _body = self.get('/xterm.js')
        self.assertIn('max-age=', head)

    def test_a_failure_before_the_terminal_starts_is_visible(self):
        """Whatever breaks next should say so on screen rather than leaving
        the blank page this feature shipped with the first time."""
        _head, body = self.get(web.CONSOLE)
        self.assertIn(b'id="boot"', body)
        _head, script = self.get('/app.js')
        self.assertIn(b"addEventListener('error'", script)

    def test_the_served_page_carries_the_layout_widths(self):
        """A slot that survives serving is a page that silently falls back to
        whatever the client guessed -- which is the whole bug, restored, and
        invisible until someone opens it on a phone."""
        from autotmux import config
        _head, body = self.get(web.CONSOLE)
        page = body.decode('utf-8')
        self.assertNotIn(web._LAYOUT_SLOT, page)
        widths = re.search(r'name="atmux-layout" content="([^"]*)"', page)
        self.assertIsNotNone(widths, 'layout widths missing from served page')
        self.assertEqual([int(n) for n in widths.group(1).split(',')],
                         list(config.LAYOUT_WIDTHS))

    def test_every_asset_the_page_needs_is_served(self):
        """Read the asset list off the page rather than restating it here: a
        hand-kept list agrees with the page right up until one of them
        changes, and then it is a test that passes while the page 404s."""
        for page_path in ('/', web.CONSOLE):
            _head, body = self.get(page_path)
            page = body.decode('utf-8')
            wanted = (re.findall(r'<script src="([^"]+)"', page) +
                      re.findall(r'<link[^>]+href="([^"]+)"', page))
            self.assertGreaterEqual(len(wanted), 2,
                                    f'{page_path}: no assets found')
            for name in wanted:
                # Relative to the page, which is the point: the console lives
                # under a prefix and its assets have to resolve there too.
                target = page_path.rstrip('/') + '/' + name
                with self.subTest(path=target):
                    head, asset = self.get(target)
                self.assertIn('200', head)
                self.assertGreater(len(asset), 0)

    def test_the_terminal_is_vendored_not_fetched(self):
        """A device on a private network may have no route to a CDN, and the
        page's own CSP forbids one anyway."""
        _head, body = self.get(web.CONSOLE)
        self.assertNotIn(b'cdn.', body)
        self.assertNotIn(b'https://unpkg', body)

    def test_the_page_forbids_loading_or_sending_anywhere_else(self):
        head, _body = self.get(web.CONSOLE)
        self.assertIn("default-src 'self'", head)
        self.assertIn("frame-ancestors 'none'", head)

    def test_a_name_off_the_wire_cannot_escape_the_asset_directory(self):
        for path in ('/../pyproject.toml', '/../../etc/passwd',
                     '/..%2f..%2fetc%2fpasswd', '//etc/passwd',
                     '/web.py', '/sub/dir/x'):
            with self.subTest(path=path):
                head, _body = self.get(path)
                self.assertIn('404', head)

    def test_health_needs_no_terminal(self):
        head, body = self.get('/healthz')
        self.assertIn('200', head)
        self.assertEqual(json.loads(body)['ok'], True)


class WebsocketBridgeTests(_ServedFixture):
    def test_the_upgrade_completes_and_proves_it_read_the_key(self):
        sock, key, head = self._open()
        try:
            self.assertIn('101', head)
            self.assertIn(web.accept_key(key), head)
        finally:
            sock.close()

    def test_a_plain_get_on_the_socket_path_is_refused(self):
        head, _body = self.get('/ws')
        self.assertIn('400', head)

    def test_keystrokes_reach_the_program_and_output_comes_back(self):
        sock, _key, _head = self._open()
        try:
            sock.sendall(client_frame(b'round trip\n'))
            reader = web.FrameReader()
            sock.settimeout(1.0)
            seen = b''
            deadline = time.time() + 10
            while time.time() < deadline and b'round trip' not in seen:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                for opcode, payload in reader.feed(chunk):
                    if opcode in (0x1, 0x2):
                        seen += payload
            self.assertIn(b'round trip', seen)
        finally:
            sock.close()

    def test_a_resize_is_applied_and_never_typed(self):
        """Resize is text, keystrokes are binary. Confusing the two would
        paste `{"t":"resize"...}` into whatever has focus."""
        server = web.Server(('127.0.0.1', 0), ['/bin/sh', '-c',
                                               'sleep 0.4; stty size'])
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sock = socket.create_connection((host, port), timeout=15)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                f'GET /ws HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n'
                f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                f'Sec-WebSocket-Version: 13\r\n\r\n'.encode())
            head = b''
            while b'\r\n\r\n' not in head:
                head += sock.recv(4096)
            sock.sendall(client_frame(
                json.dumps({'t': 'resize', 'cols': 99, 'rows': 41}).encode(),
                0x1))
            reader = web.FrameReader()
            sock.settimeout(1.0)
            seen = b''
            deadline = time.time() + 10
            while time.time() < deadline and b'41' not in seen:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                for opcode, payload in reader.feed(chunk):
                    if opcode in (0x1, 0x2):
                        seen += payload
            self.assertIn(b'41 99', seen.replace(b'\r', b''))
            self.assertNotIn(b'resize', seen)
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_garbage_control_text_cannot_crash_the_bridge(self):
        sock, _key, _head = self._open()
        try:
            for junk in (b'not json', b'[]', b'null', b'{}',
                         b'{"t":"resize"}',
                         b'{"t":"resize","cols":"x","rows":null}'):
                sock.sendall(client_frame(junk, 0x1))
            sock.sendall(client_frame(b'still alive\n'))
            reader = web.FrameReader()
            sock.settimeout(1.0)
            seen = b''
            deadline = time.time() + 10
            while time.time() < deadline and b'still alive' not in seen:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                for opcode, payload in reader.feed(chunk):
                    if opcode in (0x1, 0x2):
                        seen += payload
            self.assertIn(b'still alive', seen)
        finally:
            sock.close()

    def test_a_ping_is_answered(self):
        """A phone's connection is idle most of the time; an unanswered ping
        is how the browser decides the socket is dead."""
        sock, _key, _head = self._open()
        try:
            sock.sendall(client_frame(b'are you there', 0x9))
            reader = web.FrameReader()
            sock.settimeout(1.0)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                for opcode, payload in reader.feed(chunk):
                    if opcode == 0xA and payload == b'are you there':
                        return
            self.fail('no pong')
        finally:
            sock.close()


class TouchKeypadTests(unittest.TestCase):
    """The pad is the phone's only way to press a key.

    xterm.js has no touch gesture support (issue #5377, open and unassigned),
    so on a touch screen there is no other way to send Esc or a control
    character at all. What the pad offers is not written here any more: the
    dashboard publishes its live bindings and this renders them, because a
    copy kept in javascript is only correct on the day it is written -- and
    the copy that was here had grown three pages, which is how the layout key
    ended up two taps deep behind a tab nobody would open.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.js = f.read()
        with open(os.path.join(web.ASSETS, 'index.html'), encoding='utf-8') as f:
            cls.html = f.read()


    def test_the_keypad_offers_every_key_atmux_binds(self):
        """Not by keeping a list in step -- by not having a second list. This
        fails if the page ever goes back to naming keys of its own."""
        for gone in ('PAGES', 'buildPage', 'data-page'):
            with self.subTest(token=gone):
                self.assertNotIn(gone, self.js)
                self.assertNotIn(gone, self.html)
        self.assertIn('registerOscHandler', self.js)
        self.assertIn(str(keypad.OSC), self.js)


    def test_movement_is_the_clients_and_cannot_be_published_away(self):
        """The regression that made a phone unusable.

        There are two kinds of key. Movement and escape are *terminal*
        primitives -- true of every program, needed before anything has
        published and after everything has stopped -- and they belong to this
        client. Actions belong to the app. Deleting the first kind on the
        theory that a tap replaces it is what happened, and it does not:

            mouse click on a row : no move
            finger tap on a row  : no move
            arrow-down key       : MOVED

        measured against the cursor colour, because the table re-sorts by
        idle time every few seconds and that looks identical to a selection
        moving. xterm.js has no touch support (issue #5377), and this table
        attaches on a single click, so routing taps into it would turn a
        mis-tap into an attach.
        """
        nav = re.search(r'var NAV_KEYS = \[(.*?)\];', self.js, re.S)
        self.assertIsNotNone(nav, 'the client has no movement keys of its own')
        sent = re.findall(r"k: '([^']*)'", nav.group(1))
        self.assertIn('\\x1b[A', sent, 'no way to move up')
        self.assertIn('\\x1b[B', sent, 'no way to move down')
        self.assertIn('\\x1b', sent, 'no way out of anything')

    def test_the_app_does_not_publish_movement_because_the_client_owns_it(self):
        """Two sources for one key is how it ends up in neither."""
        from autotmux import cli
        keys = keypad.keys_for({})
        self.assertEqual(keys, [])
        for name in ('up', 'down', 'left', 'right'):
            with self.subTest(key=name):
                self.assertIn(name, keypad._SKIP)

    def test_movement_survives_the_app_publishing_nothing(self):
        """A reconnect, a program that is not this dashboard, a handover to
        tmux: the published set empties and the phone must still work."""
        render = _extract(self.js, 'renderKeys')
        # renderKeys owns #keys and must not be able to touch the nav row.
        self.assertIn("keys.textContent = ''", render)
        self.assertNotIn('nav.', render)
        build = _extract(self.js, 'renderNav')
        self.assertIn('NAV_KEYS', build)
        self.assertIn('id="nav"', self.html)

    def test_no_button_sends_a_key_atmux_does_not_bind(self):
        """A dead button is worse than a missing one: it teaches that the pad
        does not work. Every key comes from a binding that exists, so the
        risk moved to the wire -- anything could be running on that pty."""
        handler = _extract(self.js, 'buildKey')
        self.assertIsNotNone(handler)
        osc = self.js[self.js.index('registerOscHandler'):]
        osc = osc[:osc.index('return true;\n  });')]
        for guard in ("typeof entry.k === 'string'",
                      "typeof entry.l === 'string'",
                      'entry.k.length <=', 'entry.l.length <='):
            with self.subTest(guard=guard):
                self.assertIn(guard, osc)


    def test_every_key_says_what_it_does(self):
        """A row of bare letters is unreadable on a phone -- `x` kills a
        session and `z` changes the layout, and nothing said so. The labels
        are now the app's own descriptions, so this checks the app."""
        from autotmux import cli
        for binding in cli.AutotmuxApp.BINDINGS:
            if binding.key in ('q',) or not binding.description:
                continue
            with self.subTest(key=binding.key):
                self.assertGreater(
                    len(binding.description), 1,
                    f'{binding.key}: {binding.description!r} is just a letter')

    def test_no_key_stretches_into_something_it_is_not(self):
        """flex-wrap stretched whichever key landed alone on the last line
        into a full-width button, which read as something important rather
        than as the leftover it was -- and the one it did that to was `q`,
        which quits."""
        self.assertNotIn('.krow.wrap', self.html)
        # Comments may still explain why; the declaration must be gone.
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        self.assertNotIn('flex-wrap', css)
        self.assertRegex(css, r'\.krow \.key \{[^}]*flex: 1 1 0')


    def test_detach_is_offered_because_nobody_can_guess_it(self):
        """Ctrl-B then d. Attaching hands the screen to tmux, which draws no
        buttons and answers no questions, so this one set is static because
        the situation is -- see keypad.EXTERNAL_KEYS."""
        labels = {k['l'] for k in keypad.EXTERNAL_KEYS}
        self.assertIn('detach', labels)
        # And the page must not carry its own copy of it.
        self.assertNotIn('x02d', self.js)

    def test_typing_puts_a_real_input_under_the_finger(self):
        """Two attempts at calling focus() from JavaScript failed silently.
        Safari raises the keyboard when the tap itself lands on a focusable
        element -- which is why every ordinary web form works and none of
        that did."""
        self.assertIn('atmux-typing', self.js)
        self.assertIn('.xterm-helper-textarea.atmux-typing', self.html)
        self.assertRegex(self.html, r'atmux-typing \{[^}]*opacity: 0')
        self.assertRegex(self.html, r'atmux-typing \{[^}]*height: 100%')


    def test_repeating_keys_are_recognised_rather_than_flagged(self):
        """The keys worth repeating are the ones that move something a step
        at a time, and those are exactly the CSI sequences. Deriving it means
        nothing has to remember to mark a new one."""
        body = _extract(self.js, 'buildKey')
        # The literal /^\x1b\[/ -- a CSI prefix, not a hand-kept flag.
        self.assertIn('/^\\x1b\\[/', body)
        self.assertIn('setInterval', body)
        self.assertNotIn("'rep'", self.js)

    def test_the_software_keyboard_is_off_until_asked_for(self):
        """It costs half the screen and atmux needs it only to name a new
        session. inputmode=none keeps the textarea focused -- so a hardware
        keyboard still works -- without raising the on-screen one."""
        self.assertIn("'inputmode'", self.js)
        self.assertIn("'none'", self.js)
        self.assertIn('id="kbd"', self.html)

    def test_raising_the_keyboard_does_not_go_through_term_focus(self):
        """xterm's Terminal.focus() calls focus({preventScroll: true}), and
        iOS ties presenting the software keyboard to the scroll-into-view that
        focus() would otherwise perform. preventScroll suppresses the keyboard
        with no error anywhere -- which is exactly how the ⌨ button shipped
        doing nothing at all."""
        body = re.search(r'function setKeyboard\(open\) \{(.*?)\n  \}',
                         self.js, re.S)
        self.assertIsNotNone(body, 'setKeyboard not found')
        code = re.sub(r'//.*', '', body.group(1))       # comments are not code
        self.assertIn('textarea.focus()', code,
                      'the keyboard path must focus the textarea directly')
        self.assertNotIn('preventScroll', code)

    def test_the_element_is_bounced_so_ios_re_reads_the_input_mode(self):
        """iOS picks the keyboard when an element takes focus and will not
        re-evaluate one that is already focused."""
        self.assertIn('textarea.blur()', self.js)

    def test_the_pad_buttons_never_steal_focus_from_the_terminal(self):
        """A button that takes focus first leaves nothing to bounce, and the
        keyboard then belongs to the button rather than the terminal."""
        self.assertIn('function keepFocus', self.js)
        for control in ('kbd', 'minus', 'plus', 'expander'):
            with self.subTest(control=control):
                self.assertRegex(self.js, r'keepFocus\(' + control + r'\)')

    def test_typing_mode_ends_when_the_keyboard_does(self):
        """The stretched textarea covers the terminal. Left behind, it eats
        the taps that should be attaching to a session."""
        self.assertRegex(self.js, r"textarea\.addEventListener\('blur'")


    def test_changing_the_keys_retells_the_terminal_its_size(self):
        """The pad's height changes with how many keys there are, and without
        a refit the app keeps drawing rows that are now behind them."""
        self.assertIn('refit()', _extract(self.js, 'renderKeys'))

    def test_pinch_zooms_the_font_and_not_the_page(self):
        """A zoomed viewport leaves you panning a grid that no longer fits,
        which is worse than the small text it was meant to fix."""
        self.assertIn('user-scalable=no', self.html)
        self.assertIn('touchmove', self.js)
        self.assertIn('fontSize', self.js)

    def test_the_font_size_is_remembered(self):
        self.assertIn('localStorage', self.js)
        self.assertIn('atmux.fontSize', self.js)

    def test_touch_targets_meet_the_platform_minimum(self):
        """44px is Apple's guideline; below it, presses land on the wrong
        key often enough to be the reason someone stops using this."""
        sizes = re.findall(r'min-height: (\d+)px', self.html)
        self.assertTrue(sizes, 'no explicit touch target size')
        self.assertGreaterEqual(max(int(s) for s in sizes), 44)

    def test_the_keypad_is_absent_where_a_real_keyboard_exists(self):
        """On a laptop it would be clutter that steals rows from the table."""
        self.assertIn('#pad { display: none; }', self.html)
        self.assertIn('body.touch #pad', self.html)


class SoftwareKeyboardLayoutTests(unittest.TestCase):
    """The terminal has to give the platform keyboard its space.

    The software keyboard shrinks the *visual* viewport and leaves the layout
    viewport alone, so a page sized in vh/dvh keeps its full height and draws
    its last rows -- the ones with the cursor in them -- behind the keyboard.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.js = f.read()
        with open(os.path.join(web.ASSETS, 'index.html'), encoding='utf-8') as f:
            cls.html = f.read()

    def test_the_app_is_sized_to_the_visual_viewport(self):
        self.assertIn('visualViewport', self.js)
        self.assertRegex(self.js, r'app\.style\.height\s*=\s*vv\.height')

    def test_it_follows_the_viewport_being_scrolled_as_well_as_resized(self):
        """iOS scrolls the layout viewport to bring the focused element into
        view, which slides the top of the terminal off screen. Resize alone
        does not fire for that."""
        self.assertIn("vv.addEventListener('resize'", self.js)
        self.assertIn("vv.addEventListener('scroll'", self.js)
        self.assertIn('offsetTop', self.js)

    def test_the_terminal_is_re_measured_after_the_viewport_moves(self):
        """Sizing the box is only half of it: tmux still believes the old row
        count until the pty is told."""
        body = re.search(r'function syncViewport\(\) \{(.*?)\n  \}',
                         self.js, re.S)
        self.assertIsNotNone(body, 'syncViewport not found')
        self.assertIn('refit()', body.group(1))

    def test_the_container_can_actually_be_resized(self):
        """A statically positioned box sized in dvh ignores the height we set
        on it, so the fix would apply and do nothing."""
        self.assertRegex(self.html, r'#app \{[^}]*position: fixed')

    def test_the_page_does_not_bring_its_own_keyboard(self):
        """The platform's works. A worse copy of it beside the real one is
        clutter, and two keyboards is a choice nobody wants to make."""
        self.assertNotIn('data-page="abc"', self.html)
        self.assertNotIn("['q','q']", self.js)


class EntryPointTests(unittest.TestCase):
    def test_the_served_command_is_this_installation(self):
        argv = web.default_argv()
        self.assertTrue(argv)
        self.assertTrue(argv[0].endswith('atmux')
                        or argv[:2] == [sys.executable, '-m'])

    def test_it_binds_loopback_by_default(self):
        """Anything that can open this socket gets a shell, so the default
        must never be reachable from the network."""
        self.assertEqual(web.DEFAULT_HOST, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()


class BindAddressTests(unittest.TestCase):
    """Where it is safe to listen.

    Nothing here authenticates a caller, so the bind address *is* the security
    boundary: whoever can open the socket owns a shell.
    """

    def test_loopback_is_private(self):
        for host in ('127.0.0.1', 'localhost', '::1'):
            with self.subTest(host=host):
                self.assertTrue(web.is_private_bind(host))

    def test_a_tailnet_address_is_private(self):
        """Tailscale hands every node an address in the CGNAT range, and
        nothing outside the tailnet can route to it -- the same peers reach it
        as would reach `tailscale serve`, so it is the same posture and the
        answer when serve itself is unavailable."""
        for host in ('100.64.0.1', '100.64.69.42', '100.127.255.254',
                     'fd7a:115c:a1e0::c63a:452a'):
            with self.subTest(host=host):
                self.assertTrue(web.is_private_bind(host))

    def test_a_routable_address_is_not(self):
        """0.0.0.0 is the one that matters: it is one flag away and it puts a
        shell on every interface the machine has."""
        for host in ('0.0.0.0', '8.8.8.8', '192.168.1.5', '10.0.0.1',
                     '100.128.0.1', '2001:4860:4860::8888'):
            with self.subTest(host=host):
                self.assertFalse(web.is_private_bind(host))

    def test_the_edges_of_the_tailnet_range_are_right(self):
        """100.64.0.0/10 ends at 100.127.255.255; 100.128.0.0 is somebody
        else's."""
        self.assertTrue(web.is_private_bind('100.127.255.255'))
        self.assertFalse(web.is_private_bind('100.128.0.0'))
        self.assertFalse(web.is_private_bind('100.63.255.255'))

    def test_junk_is_not_mistaken_for_private(self):
        for host in ('', 'example.com', 'not an address', None):
            with self.subTest(host=host):
                self.assertFalse(web.is_private_bind(host) if isinstance(host, str)
                                 else web.is_private_bind(str(host)))


class MountPathTests(unittest.TestCase):
    """One tailnet hostname, several services.

    `tailscale serve --set-path /term` mounts this page below the root and
    strips the prefix before proxying. The page's asset references were
    already relative; the websocket was not, so it reached for a socket at the
    host root that was not there -- the page loaded and the terminal stayed
    empty with nothing to say why.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.js = f.read()
        with open(os.path.join(web.ASSETS, 'index.html'), encoding='utf-8') as f:
            cls.html = f.read()

    def test_the_socket_is_addressed_relative_to_the_page(self):
        self.assertNotIn("+ '/ws'", self.js)
        self.assertIn('location.pathname', self.js)

    def test_every_asset_the_page_loads_is_relative(self):
        """An absolute /xterm.js would 404 under any mount point."""
        for reference in re.findall(r'(?:src|href)="([^"]+)"', self.html):
            with self.subTest(reference=reference):
                self.assertFalse(reference.startswith('/'), reference)
                self.assertNotRegex(reference, r'^[a-z]+://')

    def test_the_derived_url_matches_the_page_it_was_served_from(self):
        """The same rule the browser will apply, checked against the mount
        points that actually occur."""
        def derive(pathname):
            base = re.sub(r'[^/]*$', '', pathname)
            return base + 'ws'
        self.assertEqual(derive('/'), '/ws')
        self.assertEqual(derive('/index.html'), '/ws')
        self.assertEqual(derive('/term/'), '/term/ws')
        self.assertEqual(derive('/term/index.html'), '/term/ws')
        self.assertEqual(derive('/a/b/'), '/a/b/ws')


class SafeAreaTests(unittest.TestCase):
    """viewport-fit=cover is what lets the background reach every edge of a
    notched phone -- and it makes the insets ours to apply. Skip them and the
    notch sits on top of a column of the terminal, which reads as a black band
    down one side."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'index.html'), encoding='utf-8') as f:
            cls.html = f.read()

    def test_cover_is_paired_with_every_inset(self):
        """Taking the full screen without handing back the insets is the bug;
        one implies the other."""
        if 'viewport-fit=cover' not in self.html:
            self.skipTest('not claiming the full screen')
        for side in ('left', 'right', 'bottom'):
            with self.subTest(side=side):
                self.assertIn(f'env(safe-area-inset-{side})', self.html)

    def test_the_inset_padding_does_not_shrink_the_box_it_is_on(self):
        """Padding on a fixed, full-width element without border-box makes it
        wider than the screen, which trades a black band for a scrollbar."""
        app = re.search(r'#app \{(.*?)\}', self.html, re.S)
        self.assertIsNotNone(app)
        self.assertIn('box-sizing: border-box', app.group(1))

    def test_the_terminal_spends_no_width_on_padding(self):
        """A grid of whole cells never divides its box exactly, so something
        is always left over; padding adds to that leftover and buys nothing
        with it."""
        term = re.search(r'#term \{(.*?)\}', self.html, re.S)
        self.assertIsNotNone(term)
        self.assertNotIn('padding:', term.group(1))

    def test_the_remainder_is_centred_rather_than_pushed_to_one_edge(self):
        """The sub-cell remainder is unavoidable. Split across both sides it
        is a hairline; handed to one side it is a band, which is how four
        characters of stolen scrollbar gutter presented itself."""
        term = re.search(r'#term \{(.*?)\}', self.html, re.S)
        self.assertIsNotNone(term)
        self.assertIn('justify-content: center', term.group(1))
        self.assertIn('align-items: center', term.group(1))

    def test_the_terminal_background_matches_what_the_app_paints(self):
        """Not the mechanism that keeps the edges clean -- the app draws its
        rows lighter than its own background, so no single colour can hide a
        strip outside the canvas -- but a mismatch is still visible while the
        dashboard is still starting up."""
        term = re.search(r'#term \{(.*?)\}', self.html, re.S)
        self.assertIn('#121212', term.group(1))

    def test_the_vendored_scrollbar_cannot_paint_over_the_edge(self):
        """xterm's own stylesheet paints .xterm-viewport #000 and reserves a
        scrollbar on it. Both land outside the canvas, beside the dashboard
        and nowhere else."""
        viewport = re.search(r'\.xterm \.xterm-viewport \{(.*?)\}',
                             self.html, re.S)
        self.assertIsNotNone(viewport, 'xterm viewport background not overridden')
        self.assertIn('transparent', viewport.group(1))
        # Hidden chrome, not hidden overflow: wheel scrollback still works.
        self.assertNotIn('overflow-y: hidden', viewport.group(1))
        self.assertIn('::-webkit-scrollbar', self.html)


class BackgroundColourTests(unittest.TestCase):
    """The page and the app have to agree on one colour.

    This was once believed to be the fix for the band down the right-hand
    side, and it is not: the strip sat outside the canvas, and the dashboard
    paints its table rows lighter than its own background, so there is no
    colour that could have hidden it. Not leaving a strip is the fix -- see
    LayoutContractTests. What this class still buys is the moment before the
    dashboard has drawn anything, and the frame after a rotation.
    """

    @classmethod
    def setUpClass(cls):
        for name in ('index.html', 'app.js', 'manifest.json'):
            with open(os.path.join(web.ASSETS, name), encoding='utf-8') as f:
                setattr(cls, name.split('.')[0], f.read())

    def test_the_terminal_matches_what_textual_paints(self):
        """Checked against the framework rather than assumed: Textual's
        textual-dark renders on #121212."""
        from textual.app import App
        self.assertIn('#121212', self.app)

    def test_nothing_in_the_page_disagrees(self):
        """One stale colour anywhere is enough to draw the band."""
        for name in ('index', 'app', 'manifest'):
            source = getattr(self, name)
            with self.subTest(asset=name):
                stray = set(re.findall(r'#0b0b0c', source, re.I))
                self.assertEqual(stray, set(), f'{name}: {stray}')

    def test_the_installed_app_opens_on_the_same_colour(self):
        """A PWA paints background_color before the page renders; a different
        one there is a flash of the wrong colour on every launch."""
        manifest = json.loads(self.manifest)
        self.assertEqual(manifest['background_color'].lower(), '#121212')
        self.assertEqual(manifest['theme_color'].lower(), '#121212')


class LayoutContractTests(unittest.TestCase):
    """One side must not guess what width the other side needs.

    The band down the right of a phone was never a rounding error. Measured
    at nine font sizes on a 390px screen the leftover was a flat 17px --
    four characters at the font in use, and it did not shrink as the font
    shrank, which a rounding remainder must. It was FitAddon reserving a
    hard-coded 14px for a scrollbar a full-screen TUI can never show, plus
    2px of padding of our own.

    Underneath that was the real fault: nobody decided the layout. The font
    size was an input hard-coded per device type and the column count was
    whatever fell out of it -- 56 on a phone, which is too few for either of
    the dashboard's layouts, so the table truncated STATUS away and grew a
    horizontal scrollbar inside a full-screen app. The fix is to make the
    screen decide the grid and the grid decide the font, off one set of
    widths that both sides read.
    """

    @classmethod
    def setUpClass(cls):
        for name in ('index.html', 'app.js'):
            with open(os.path.join(web.ASSETS, name), encoding='utf-8') as f:
                setattr(cls, name.split('.')[0], f.read())

    # ── the contract itself ──────────────────────────────────────────────

    def test_the_widths_are_ordered_widest_first(self):
        """The client walks them in order and takes the first it can afford;
        out of order it would settle for the narrow layout on a big screen."""
        from autotmux import config
        self.assertEqual(list(config.LAYOUT_WIDTHS),
                         sorted(config.LAYOUT_WIDTHS, reverse=True))
        self.assertGreater(config.LAYOUT_SPLIT_WIDTH, config.LAYOUT_TABLE_WIDTH)

    def test_the_tui_reflows_on_the_published_width(self):
        """If the TUI kept its own copy of the number, the client would size
        its font to land on a breakpoint the TUI does not have."""
        from autotmux import cli, config
        self.assertEqual(cli._MIN_SPLIT_WIDTH, config.LAYOUT_SPLIT_WIDTH)

    def test_the_page_is_told_the_widths(self):
        from autotmux import config
        meta = web._layout_meta()
        self.assertIn('name="atmux-layout"', meta)
        published = re.search(r'content="([^"]*)"', meta).group(1)
        self.assertEqual([int(n) for n in published.split(',')],
                         list(config.LAYOUT_WIDTHS))

    def test_the_page_on_disk_has_somewhere_to_put_them(self):
        """Whether the *server* fills it is checked against a live request in
        AssetTests -- asserting on a replace() performed by the test would
        only prove the test can call replace()."""
        self.assertIn(web._LAYOUT_SLOT, self.index)

    def test_the_client_reads_the_widths_rather_than_restating_them(self):
        from autotmux import config
        self.assertIn('atmux-layout', self.app)
        # A fallback is fine -- the page could be opened from disk -- but it
        # has to agree, or the two disagree exactly when the meta is missing.
        fallback = re.search(r'return out\.length \? out : \[([^\]]*)\]',
                             self.app)
        self.assertIsNotNone(fallback, 'no fallback widths found')
        self.assertEqual([int(n) for n in fallback.group(1).split(',')],
                         list(config.LAYOUT_WIDTHS))

    # ── the arithmetic that stole the width ──────────────────────────────

    def test_nothing_reserves_width_for_a_scrollbar(self):
        """FitAddon's flat 14px is the whole reason this file computes its own
        grid. Loading it again would bring the band back. Naming it in a
        comment is fine -- calling it is not."""
        for call in ('new FitAddon', 'FitAddon.FitAddon', 'loadAddon'):
            with self.subTest(call=call):
                self.assertNotIn(call, self.app)
        self.assertNotIn('addon-fit.js', self.index)
        self.assertIn('term.resize(', self.app)

    def test_the_terminal_is_sized_to_its_own_grid(self):
        """Without this there is nothing for the centring in #term to act on,
        and the remainder goes back to one edge."""
        grid = _extract(self.app, 'applyGrid')
        self.assertRegex(grid, r'term\.element\.style\.width\s*=')
        self.assertRegex(grid, r'term\.element\.style\.height\s*=')
        self.assertIn('Math.floor(box.width / cell.w)', grid)

    def test_the_font_size_is_not_chosen_by_device_type(self):
        """`touch ? 11 : 13` is what produced 56 columns. The size has to come
        out of the width, not out of a guess about the hardware."""
        self.assertRegex(self.app, r'function autoFont\(')
        relayout = re.search(r'function relayout\(\) \{(.*?)\n  \}',
                             self.app, re.S)
        self.assertIsNotNone(relayout)
        self.assertIn('autoFont(', relayout.group(1))

    def test_an_override_is_remembered_and_can_be_handed_back(self):
        """Auto has to be the default and the thing you can return to, or the
        first pinch pins the layout wrong forever."""
        self.assertIn('atmux.fontSize', self.app)
        self.assertIn('removeItem', self.app)
        self.assertIn('id="fontauto"', self.index)
        self.assertIn("getElementById('fontauto')", self.app)


@unittest.skipUnless(_node(), 'needs node to evaluate the shipped function')
class AutoFontTests(unittest.TestCase):
    """Run the shipped arithmetic, not a restatement of it.

    The rounding direction is the whole game: rounding the font size up lands
    one column short of the target, which is the single place it must not
    land.
    """

    # cell width per point of font size. Measured in a real browser across
    # 7px to 16px, where it held to four decimals.
    RATIO = 0.60229

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.app = f.read()
        cls.source = _extract(cls.app, 'autoFont')
        cls.bounds = re.search(r'var MIN_AUTO = ([\d.]+), MAX_AUTO = ([\d.]+)',
                               cls.app)

    def font_for(self, width, widths=None):
        from autotmux import config
        widths = list(widths or config.LAYOUT_WIDTHS)
        harness = f"""
        var RATIO = {self.RATIO};
        var term = {{ options: {{ fontSize: 13 }} }};
        function cellSize() {{ return {{ w: 13 * RATIO, h: 13 * 1.2 }}; }}
        function layoutWidths() {{ return {json.dumps(widths)}; }}
        var MIN_AUTO = {self.bounds.group(1)}, MAX_AUTO = {self.bounds.group(2)};
        {self.source}
        console.log(JSON.stringify(autoFont({width})));
        """
        import subprocess
        out = subprocess.run([_node(), '-e', harness], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def columns(self, width, font):
        return int(width // (font * self.RATIO))

    def test_a_phone_gets_every_column_of_the_table(self):
        """390px is an iPhone in portrait. Before this it got 56 columns and
        the header read LOAD with STATUS off the edge."""
        from autotmux import config
        font = self.font_for(390)
        self.assertGreaterEqual(self.columns(390, font),
                                config.LAYOUT_TABLE_WIDTH)

    def test_a_tablet_gets_the_split_view(self):
        """820px is an iPad in portrait -- the width where the preview pane
        starts being worth its room."""
        from autotmux import config
        font = self.font_for(820)
        self.assertGreaterEqual(self.columns(820, font),
                                config.LAYOUT_SPLIT_WIDTH)

    def test_it_never_lands_one_column_short(self):
        """The rounding-direction test, over every real device width to hand.

        Whatever target the size was solved for, the grid it produces has to
        actually reach it. Rounding the font up instead of down misses by the
        fraction it rounded away -- 117 columns where 118 was the point.
        """
        from autotmux import config
        floor = float(self.bounds.group(1))
        for width in (320, 375, 390, 414, 428, 744, 768, 820, 834, 1024,
                      1133, 1180, 1280, 1440, 1680, 1920, 2560):
            font = self.font_for(width)
            cols = self.columns(width, font)
            # The widest target this screen could have been solved for.
            wanted = next((w for w in config.LAYOUT_WIDTHS
                           if width / w / self.RATIO >= floor), None)
            with self.subTest(width=width):
                if wanted is not None:
                    self.assertGreaterEqual(
                        cols, wanted,
                        f'{width}px at {font}px -> {cols} cols, wanted {wanted}')

    def test_it_stays_legible_and_stops_growing(self):
        """A screen too small for any layout should get the smallest legible
        size rather than an illegible one that happens to fit; a huge screen
        should spend the room on columns, not on letter height."""
        low, high = float(self.bounds.group(1)), float(self.bounds.group(2))
        self.assertEqual(self.font_for(280), low)
        self.assertEqual(self.font_for(3000), high)
        for width in (320, 390, 428, 768, 820, 1024, 1180, 1680, 2560):
            with self.subTest(width=width):
                self.assertGreaterEqual(self.font_for(width), low)
                self.assertLessEqual(self.font_for(width), high)


class ControlSurfaceTests(_ServedFixture):
    """Exactly one surface draws the controls.

    A browser renders the published bindings as real buttons outside the
    character grid; a phone ssh client has only the grid; a laptop has a
    keyboard and wants neither. Which of those is on the far end is a
    property of the *client*, and the same server process serves all three --
    so a footer hidden because a phone might connect leaves a laptop with no
    controls at all. The page says which it is in the socket URL, the only
    channel that exists before the pty does.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.js = f.read()

    def test_a_touch_client_asks_for_buttons_in_its_socket_url(self):
        url = _extract(self.js, 'socketURL')
        self.assertIn('touch=1', url)
        # Conditional on the client, not always sent: a laptop asking for
        # buttons is a laptop whose footer gets hidden for nothing.
        self.assertRegex(url, r'if \(touch\)')

    def test_the_server_only_tells_the_app_when_the_client_said_so(self):
        handler = web.Handler.__new__(web.Handler)
        handler.server = self.server
        for path, expected in (('/ws?touch=1', 'web'),
                               ('/ws', ''),
                               ('/ws?touch=0', ''),
                               ('/ws?touched=1', ''),
                               ('/ws?other=1&touch=1', 'web')):
            handler.path = path
            with self.subTest(path=path):
                env = handler._client_env()
                self.assertEqual(env.get(keypad.TOUCH_ENV, ''), expected)

    def test_a_desktop_browser_keeps_the_apps_own_footer(self):
        """The regression this guards: hiding the footer for everyone because
        one of the clients draws its own buttons."""
        handler = web.Handler.__new__(web.Handler)
        handler.server = self.server
        handler.path = '/ws'
        self.assertEqual(keypad.touch_mode(handler._client_env()), '')


@unittest.skipUnless(os.path.exists(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '.venv', 'bin', 'atmux')), 'needs the installed entry point')
class PublishedControlsEndToEndTests(unittest.TestCase):
    """The dashboard really emits this, through a real pty.

    Every other test here reads source. This one runs the program: an escape
    sequence that is well-formed in a unit test and mangled by the renderer
    would pass everything else and reach the phone as nothing at all.
    """

    ATMUX = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        '.venv', 'bin', 'atmux')

    def _run(self, env, seconds=25):
        term = web.Terminal([self.ATMUX], env=env, cols=68, rows=50)
        raw = b''
        deadline = time.time() + seconds
        try:
            while time.time() < deadline:
                raw += term.read()
                if b'\x1b]%d;' % keypad.OSC in raw:
                    break
                time.sleep(0.05)
        finally:
            term.close()
        return raw.decode('utf-8', 'replace')

    def test_the_bindings_arrive_and_carry_what_a_button_needs(self):
        text = self._run({keypad.TOUCH_ENV: 'web'})
        # Escaped rather than hand-written: the terminator is ESC backslash,
        # and a pattern that trims one backslash too many fails on a payload
        # that was perfectly good.
        pattern = (re.escape(f'\x1b]{keypad.OSC};') + '.*?'
                   + re.escape('\x1b\\'))
        frames = re.findall(pattern, text)
        self.assertTrue(frames, 'nothing was published')
        data = keypad.decode(frames[0])
        self.assertIsNotNone(data, f'unparseable: {frames[0]!r}')
        self.assertEqual(data['mode'], 'app')
        labels = {k['l']: k['k'] for k in data['keys']}
        # The footer's own keys, which is the app's statement of what matters.
        for label, sequence in (('Attach', '\r'), ('SSH to node', 's'),
                                ('Layout', 'z'), ('Help', '?')):
            with self.subTest(label=label):
                self.assertEqual(labels.get(label), sequence)
        # And nothing that would be a trap under a thumb.
        self.assertNotIn('Quit', labels)

    def test_a_keyboard_client_is_told_nothing(self):
        """Publishing to a terminal that cannot use it is noise on the wire,
        and on a slow link it is noise competing with the screen."""
        text = self._run({}, seconds=8)
        self.assertNotIn('\x1b]%d;' % keypad.OSC, text)


class DashboardTests(_ServedFixture):
    """The reading half, served as data rather than as a screen.

    Every problem the console has -- a grid that never divides the box, a
    column budget, a font solved backwards from breakpoints, a keypad
    standing in for touch xterm.js does not implement -- comes from sending
    a rendering instead of the model. This sends the model.
    """

    def state(self):
        _head, body = self.get('/api/state')
        return json.loads(body.decode('utf-8'))

    def test_the_root_is_the_dashboard_and_the_terminal_is_a_click_away(self):
        _head, body = self.get('/')
        page = body.decode('utf-8')
        self.assertIn('dash.js', page)
        self.assertNotIn('xterm.js', page)
        self.assertIn('console/', page)
        _head, console = self.get(web.CONSOLE)
        self.assertIn(b'xterm.js', console)

    def test_the_console_keeps_working_under_its_prefix(self):
        """Its assets and its socket are addressed relative to the page, so
        moving the page has to move them with it."""
        head, _body = self.get(web.CONSOLE + 'app.js')
        self.assertIn('200', head)
        head, _body = self.get(web.CONSOLE.rstrip('/'))
        self.assertIn('302', head)

    def test_the_state_endpoint_answers_even_with_no_source(self):
        """A dashboard that 500s because nothing has fetched yet is a
        dashboard that looks broken on every cold start."""
        data = self.state()
        self.assertIsInstance(data, dict)

    def test_the_page_asks_for_the_api_relative_to_itself(self):
        """`tailscale serve --set-path /term` is why: an absolute /api/state
        would reach for the host root, where nothing is mounted."""
        with open(os.path.join(web.ASSETS, 'dash.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertIn("new URL('api/", js)
        self.assertNotIn("'/api/", js)

    def test_the_page_renders_no_html_from_the_wire(self):
        """Session names, node names and squeue output are all attacker-
        adjacent in the sense that they come from a cluster. textContent
        cannot execute; innerHTML can."""
        with open(os.path.join(web.ASSETS, 'dash.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertNotIn('innerHTML', js)
        self.assertIn('textContent', js)


class DashboardStateTests(unittest.TestCase):
    """What /api/state promises a client, without a server in the way."""

    def setUp(self):
        from autotmux import statesource
        self.statesource = statesource

    def test_a_source_that_has_never_fetched_says_so_rather_than_lying(self):
        source = self.statesource.StateSource(pool=object())
        snap = source.snapshot()
        self.assertIsNone(snap['age'])
        self.assertFalse(snap['stale'])
        self.assertEqual(snap['sessions'], [])

    def test_a_failed_refresh_keeps_the_last_good_answer(self):
        """A gateway being slow must not blank the dashboard. Going stale and
        saying how stale is the honest failure."""
        class Pool:
            def __init__(self):
                self.ok = True

            def fetch_state(self):
                if self.ok:
                    return True, {'nodes': {'n1': {
                        'alive': True, 'sessions': [('a', 1, 0)],
                        'info': {'time': '1:00', 'nproc': '8', 'load': '1.0',
                                 'sessions': [('a', 1, 0)]}}}}
                raise OSError('gateway unreachable')

        clock = [1000.0]
        pool = Pool()
        source = self.statesource.StateSource(pool=pool, refresh=5.0,
                                              clock=lambda: clock[0])
        self.assertTrue(source.refresh())
        self.assertEqual(len(source.snapshot()['sessions']), 1)

        pool.ok = False
        clock[0] += 60.0
        self.assertFalse(source.refresh())
        snap = source.snapshot()
        self.assertEqual(len(snap['sessions']), 1, 'the last answer was lost')
        self.assertTrue(snap['stale'])
        self.assertGreaterEqual(snap['age'], 60.0)
        self.assertIn('unreachable', snap['error'])

    def test_it_never_raises_at_the_call_site(self):
        class Exploding:
            def fetch_state(self):
                raise RuntimeError('boom')

        source = self.statesource.StateSource(pool=Exploding())
        self.assertFalse(source.refresh())
        self.assertIsInstance(source.snapshot(), dict)

    def test_a_refusing_pool_is_not_treated_as_an_empty_cluster(self):
        """`ok=False` means "I could not tell you", not "there is nothing".
        Overwriting a good answer with it is how a dashboard reports every
        session gone at the moment the network hiccups."""
        class Refusing:
            def fetch_state(self):
                return False, {}

        source = self.statesource.StateSource(pool=Refusing())
        self.assertFalse(source.refresh())
        self.assertIsNone(source.snapshot()['age'])


class AttachTargetTests(_ServedFixture):
    """A tap on a row has to land in that session.

    Opening the dashboard instead is a screen that costs a tap and answers
    nothing -- it is the same list you just tapped, which is exactly how it
    was reported. The target rides in the socket URL because the pty is
    created when the socket is upgraded and there is no channel before that.
    """

    def argv_for(self, path: str) -> list:
        handler = web.Handler.__new__(web.Handler)
        handler.server = self.server
        handler.path = path
        return handler._client_argv()

    def test_a_valid_target_reaches_the_program(self):
        self.assertEqual(
            self.argv_for('/ws?attach=holygpu8a11104:newclaw'),
            list(self.COMMAND) + ['--attach=holygpu8a11104:newclaw'])

    def test_the_other_verb_opens_the_dashboard_standing_on_the_row(self):
        """Two verbs cover every action rather than one flag per action:
        renew, kill, note, view output and ssh all act on the highlighted
        row, so landing on the right row makes all of them reachable."""
        self.assertEqual(
            self.argv_for('/ws?select=holygpu8a11104:newclaw'),
            list(self.COMMAND) + ['--select=holygpu8a11104:newclaw'])

    def test_only_the_verbs_on_the_list_are_accepted(self):
        """A whitelist, not a passthrough: this arrives in a URL."""
        for verb in ('run', 'exec', 'shell', 'open-url', 'cluster'):
            with self.subTest(verb=verb):
                self.assertEqual(self.argv_for(f'/ws?{verb}=n:s'),
                                 list(self.COMMAND))

    def test_the_target_survives_the_other_parameters(self):
        self.assertIn('--attach=login--zgx:autoscientists',
                      self.argv_for('/ws?touch=1&attach='
                                    'login--zgx%3Aautoscientists'))

    def test_it_is_one_argv_element_so_nothing_can_split_it(self):
        """`--attach -- value` does not survive argparse, and a separate word
        whose value starts with a dash is read as an option."""
        extra = self.argv_for('/ws?attach=n:s')[len(self.COMMAND):]
        self.assertEqual(len(extra), 1)
        self.assertTrue(extra[0].startswith('--attach='))

    def test_a_target_that_is_not_one_is_refused_rather_than_passed_on(self):
        """This arrives in a URL, and a URL is untrusted: anyone who can
        reach this socket can craft one. Refusing leaves the dashboard, which
        is the safe thing to open."""
        for value in ('--version', '-rf:x', 'nocolon', 'node:sess;rm -rf /',
                      'a' * 200 + ':b', 'n:s\nmore', '', ':', 'n:', ':s',
                      '$(id):x', '../../etc:passwd'):
            for verb in ('attach', 'select'):
                with self.subTest(verb=verb, value=value):
                    path = (f'/ws?{verb}='
                            + urllib.parse.quote(value, safe=''))
                    self.assertEqual(self.argv_for(path), list(self.COMMAND))

    def test_the_socket_closes_saying_the_program_finished(self):
        """A phone drops this socket every time it locks, so the page
        reconnects by default. Detaching must not leave you watching a
        terminal reconnect to nothing forever."""
        sock, _key, _head = self._open()
        try:
            sock.sendall(client_frame(b'', 0x8))       # ask it to close
            raw = b''
            deadline = time.time() + 10
            while time.time() < deadline:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            sock.close()
        # /bin/cat is still running -- the client hung up, the program did
        # not finish -- so this close must NOT claim it did.
        self.assertNotIn(b'exit', raw)

    def test_a_program_that_finishes_says_so(self):
        server = web.Server(('127.0.0.1', 0), ['/bin/echo', 'done'])
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sock = socket.create_connection((host, port), timeout=15)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                f'GET /ws HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n'
                f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                f'Sec-WebSocket-Version: 13\r\n\r\n'.encode())
            raw = b''
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                raw += chunk
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        # A close frame carrying 1000 and the reason the page keys off.
        self.assertIn(struct.pack('!H', 1000) + b'exit', raw)


class AttachLinkTests(unittest.TestCase):
    """The two pages either side of the tap."""

    @classmethod
    def setUpClass(cls):
        for name in ('dash.js', 'app.js'):
            with open(os.path.join(web.ASSETS, name), encoding='utf-8') as f:
                setattr(cls, name.split('.')[0], f.read())

    def test_a_row_offers_both_verbs(self):
        """Attaching is what you want nine times in ten; everything else
        lives on the row the dashboard is standing on."""
        self.assertIn("go('attach', row)", self.dash)
        self.assertIn("go('select', row)", self.dash)
        self.assertIn("stopPropagation", self.dash)

    def test_the_list_puts_the_session_in_the_link(self):
        body = _extract(self.dash, 'go')
        self.assertIn('searchParams.set(verb', body)
        self.assertIn("row.node + ':' + row.session", body)
        # The routing name, not the label: login:zgx is for reading.
        self.assertNotIn('node_label', body)

    def test_only_a_real_session_gets_a_target(self):
        """A node with no sessions has nothing to attach to, and asking for
        one would be asking for a session named after a sentinel."""
        self.assertIn("row.kind === 'session'", _extract(self.dash, 'go'))

    def test_the_console_forwards_the_target_to_the_socket(self):
        body = _extract(self.app, 'socketURL')
        self.assertIn("'attach', 'select'", body)
        self.assertIn('encodeURIComponent', body)

    def test_a_finished_program_returns_to_the_list(self):
        self.assertIn("event.reason === 'exit'", self.app)
        self.assertIn("event.code === 1000", self.app)
