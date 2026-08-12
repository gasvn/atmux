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


def _extract_list(source: str, name: str) -> str:
    """One top-level `var NAME = [...]` out of app.js, by bracket depth.

    Same reason as _extract: these are tables of bytes, and a test that
    restated them here would be checking its own copy.

    String-aware, unlike the brace scanner above, because these tables are
    full of `'\\x1b[D'` -- counting the bracket inside that literal runs the
    scanner off the end of the file and reports the table as unbalanced.
    """
    start = source.index(f'var {name} = [')
    depth, i, quote = 0, source.index('[', start), ''
    while i < len(source):
        char = source[i]
        if quote:
            if char == '\\':
                i += 1
            elif char == quote:
                quote = ''
        elif char in '\'"':
            quote = char
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth == 0:
                return source[start:i + 1] + ';'
        i += 1
    raise AssertionError(f'unbalanced brackets in {name}')


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

    def request(self, path: str, method: str = 'GET',
                headers: dict | None = None) -> tuple[str, bytes]:
        """One request, with whatever method and headers the test needs.

        Compression and identity are both decided by request headers, so a
        helper that can only send the default set cannot exercise either.
        """
        lines = [f'{method} {path} HTTP/1.1', 'Host: t', 'Connection: close']
        for name, value in (headers or {}).items():
            lines.append(f'{name}: {value}')
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        raw = b''
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
        sock.close()
        head, _, body = raw.partition(b'\r\n\r\n')
        return head.decode('latin-1'), body

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


class TransferTests(_ServedFixture):
    """What actually goes over the wire.

    Measured before any of this: the vendored terminal went out at 488,663
    bytes with `Accept-Encoding: gzip` on the request and nothing negotiating
    it on the way back -- 590 KB for a whole first load that gzip takes to
    156 KB. The caching was already right, so it only cost the first load,
    which is the one happening on a phone away from wifi.
    """

    def test_a_browser_that_offers_gzip_gets_it(self):
        head, body = self.request('/console/xterm.js',
                                  headers={'Accept-Encoding': 'gzip, deflate'})
        self.assertIn('200', head)
        self.assertIn('Content-Encoding: gzip', head)
        raw = open(os.path.join(web.ASSETS, 'xterm.js'), 'rb').read()
        self.assertLess(len(body), len(raw) / 2)
        import gzip as _gzip
        self.assertEqual(_gzip.decompress(body), raw)

    def test_a_client_that_does_not_offer_it_gets_the_file(self):
        head, body = self.request('/console/xterm.js',
                                  headers={'Accept-Encoding': 'identity'})
        self.assertNotIn('Content-Encoding', head)
        self.assertEqual(
            body, open(os.path.join(web.ASSETS, 'xterm.js'), 'rb').read())

    def test_a_client_that_refuses_gzip_is_believed(self):
        """`gzip;q=0` is how a client says no to exactly this encoding, and
        reading it as a yes because the word appears is the obvious bug."""
        head, _ = self.request('/console/xterm.js',
                               headers={'Accept-Encoding': 'gzip;q=0'})
        self.assertNotIn('Content-Encoding', head)

    def test_the_encoding_is_named_in_vary_whether_or_not_it_was_used(self):
        """These are served `public, immutable` for a week. A shared cache
        that kept one encoding for every client would hand a gzip body to one
        that never asked."""
        for encoding in ('gzip', 'identity'):
            with self.subTest(accept=encoding):
                head, _ = self.request('/console/xterm.js',
                                       headers={'Accept-Encoding': encoding})
                self.assertIn('Vary: Accept-Encoding', head)

    def test_what_does_not_compress_is_not_compressed(self):
        """A PNG through gzip costs CPU to come out slightly larger."""
        head, _ = self.request('/console/icon-192.png',
                               headers={'Accept-Encoding': 'gzip'})
        self.assertIn('200', head)
        self.assertNotIn('Content-Encoding', head)

    def test_something_smaller_than_the_header_is_left_alone(self):
        head, body = self.request('/healthz',
                                  headers={'Accept-Encoding': 'gzip'})
        self.assertNotIn('Content-Encoding', head)
        self.assertEqual(body, b'{"ok":true}\n')

    # ── HEAD ──────────────────────────────────────────────────────────────

    def test_head_describes_the_response_without_sending_it(self):
        """It answered 501. Harmless until something in front of it -- a
        health probe, a proxy, a link preview -- asks."""
        head, body = self.request('/', method='HEAD')
        self.assertIn('200', head)
        self.assertEqual(body, b'')
        get_head, get_body = self.get('/')
        self.assertIn(f'Content-Length: {len(get_body)}', head)
        self.assertIn('Content-Type: text/html', head)

    def test_head_on_a_socket_is_refused_rather_than_performed(self):
        """A handshake is a connection, not a document: there is nothing to
        describe without doing it."""
        head, _ = self.request('/console/ws', method='HEAD')
        self.assertIn('405', head)

    def test_head_on_something_missing_is_still_missing(self):
        head, body = self.request('/console/nope.js', method='HEAD')
        self.assertIn('404', head)
        self.assertEqual(body, b'')


class TailnetIdentityTests(unittest.TestCase):
    """Who is allowed in, when anyone is.

    `tailscale serve` sets Tailscale-User-Login on everything it proxies.
    Believing a header like that is normally the mistake -- whoever can reach
    the port can also write it -- and the reason it can be believed here is
    that the server listens on loopback, so that is this machine and nothing
    else. serve() enforces the precondition rather than assuming it.
    """

    def serve(self, allow_users):
        server = web.Server(('127.0.0.1', 0), ['/bin/cat'],
                            allow_users=allow_users)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[:2]

    def ask(self, address, login=None, path='/healthz'):
        host, port = address
        lines = [f'GET {path} HTTP/1.1', 'Host: t', 'Connection: close']
        if login is not None:
            lines.append(f'Tailscale-User-Login: {login}')
        sock = socket.create_connection((host, port), timeout=10)
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        raw = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        sock.close()
        return int(raw.split()[1])

    def test_no_list_is_the_historical_behaviour(self):
        """Off by default. A tailnet of one does not need this, and turning a
        check on for everybody would break the deployment that exists."""
        address = self.serve([])
        self.assertEqual(self.ask(address), 200)
        self.assertEqual(self.ask(address, 'anyone@example.com'), 200)

    def test_a_named_login_gets_in_and_nobody_else_does(self):
        address = self.serve(['alice@example.com'])
        self.assertEqual(self.ask(address, 'alice@example.com'), 200)
        for who in (None, '', 'mallory@example.com', 'alice@example.com.evil'):
            with self.subTest(login=who):
                self.assertEqual(self.ask(address, who), 403)

    def test_the_comparison_does_not_care_about_case_or_spaces(self):
        """The config is hand-written and the header comes off the wire."""
        address = self.serve(['  Alice@Example.COM '])
        self.assertEqual(self.ask(address, ' ALICE@example.com  '), 200)

    def test_the_socket_is_behind_the_same_door_as_the_page(self):
        """The check has to be on the route that hands out a shell, not only
        on the one that hands out HTML."""
        address = self.serve(['alice@example.com'])
        self.assertEqual(self.ask(address, None, path='/console/ws'), 403)
        self.assertEqual(self.ask(address, None, path='/console/'), 403)
        self.assertEqual(self.ask(address, None, path='/api/state'), 403)

    def test_a_list_with_a_bind_that_cannot_honour_it_refuses_to_start(self):
        """A security control that is quietly ineffective is worse than one
        that is absent: off loopback, anyone who can reach the port can also
        write the header this checks."""
        import unittest.mock as mock
        with mock.patch.object(web.config, 'load_web',
                               lambda: {'allow_users': ['a@b.c']}):
            for host in ('0.0.0.0', '100.64.1.2'):
                with self.subTest(host=host):
                    with self.assertRaises(SystemExit) as caught:
                        web.serve(host, 0)
                    self.assertIn('allow_users', str(caught.exception))

    def test_loopback_is_recognised_however_it_is_spelled(self):
        for host, expected in (('127.0.0.1', True), ('127.0.0.53', True),
                               ('localhost', True), ('::1', True), ('', True),
                               ('0.0.0.0', False), ('100.64.1.2', False),
                               ('example.com', False)):
            with self.subTest(host=host):
                self.assertEqual(web.is_loopback_bind(host), expected)


class HomeScreenTests(_ServedFixture):
    """The mark, and the offer to install that does not appear without it.

    It matters more here than on an ordinary site: Safari's chrome is about
    110px of an 844px screen -- roughly ten rows of terminal, the same order
    as the landscape bug -- and installing to the home screen is how you get
    them back. The page was already built for it and was missing the one asset
    that makes the offer appear.
    """

    def test_the_manifest_names_icons_that_are_actually_served(self):
        _head, body = self.get('/manifest.json')
        manifest = json.loads(body)
        self.assertTrue(manifest.get('icons'), 'no icons at all')
        for icon in manifest['icons']:
            with self.subTest(src=icon['src']):
                head, data = self.get('/console/' + icon['src'])
                self.assertIn('200', head)
                self.assertTrue(data)

    def test_a_launcher_that_crops_the_icon_does_not_crop_the_mark(self):
        """A maskable icon is cut to whatever shape the launcher wants, so it
        must be full bleed and the mark must sit inside the middle."""
        _head, body = self.get('/manifest.json')
        purposes = [i.get('purpose', '') for i in json.loads(body)['icons']]
        self.assertTrue(any('maskable' in p for p in purposes))

    def test_both_pages_offer_the_icon_relative_to_wherever_they_are(self):
        """An absolute path is a claim about where this server is mounted,
        and behind `tailscale serve --set-path` it is the wrong one."""
        for path in ('/', web.CONSOLE):
            with self.subTest(page=path):
                _head, body = self.get(path)
                page = body.decode('utf-8')
                self.assertIn('rel="apple-touch-icon" href="icon-180.png"',
                              page)
                self.assertIn('rel="icon" href="icon.svg"', page)

    def test_the_policy_admits_the_icon_it_asks_for(self):
        head, _ = self.get(web.CONSOLE)
        self.assertIn("img-src 'self'", head)

    def test_both_pages_opt_into_the_same_transition(self):
        """Cross-document transitions need both documents to agree; one side
        alone is a hard cut with extra CSS."""
        for path in ('/', web.CONSOLE):
            with self.subTest(page=path):
                _head, body = self.get(path)
                self.assertIn('@view-transition { navigation: auto; }',
                              body.decode('utf-8'))

    def test_the_transition_does_not_scale_a_character_grid(self):
        """Opacity only. Scaling the incoming page for 180ms is how you turn
        a transition into a blurry frame of a terminal."""
        _head, body = self.get(web.CONSOLE)
        page = body.decode('utf-8')
        block = page[page.index('@view-transition'):page.index('html, body')]
        self.assertNotIn('scale', block)
        self.assertIn('prefers-reduced-motion', block)


class BuildStampTests(_ServedFixture):
    """Which build the reader is actually running.

    Both pages declare apple-mobile-web-app-capable, so on a phone they are
    home-screen apps, and iOS resumes one of those from a snapshot rather than
    fetching it. A deploy landed, was served correctly -- verified byte for
    byte through the tailnet URL the phone uses, `no-store` and all -- and the
    screen did not change, because the screen was days old and said nothing
    about it. "Did you deploy it?" and "yes, I checked the bytes on the wire"
    were true at the same time and neither one answered the question.

    So the page carries its own name now, and asks.
    """

    def test_both_pages_say_which_build_they_are(self):
        for path in ('/', web.CONSOLE):
            with self.subTest(page=path):
                _head, body = self.get(path)
                page = body.decode('utf-8')
                found = re.search(
                    r'<meta name="atmux-build" content="([^"]+)">', page)
                self.assertTrue(found, 'the page cannot name itself')
                self.assertTrue(found.group(1).strip())

    def test_the_slot_is_filled_rather_than_shipped(self):
        """A page still carrying the comment is a page whose stamp silently
        did nothing -- which is the exact failure this exists to catch."""
        for path in ('/', web.CONSOLE):
            with self.subTest(page=path):
                _head, body = self.get(path)
                self.assertNotIn(web._BUILD_SLOT.encode('ascii'), body)

    def test_the_page_and_the_server_agree_on_the_name(self):
        """The whole mechanism is a comparison. If the two sides derive it
        differently, every load reports an update that reloading never fixes:
        an infinite `tap to load` that is worse than no notice at all."""
        _head, body = self.get(web.CONSOLE)
        page = re.search(r'<meta name="atmux-build" content="([^"]+)">',
                         body.decode('utf-8')).group(1)
        _head, served = self.get('/api/build')
        self.assertEqual(json.loads(served)['build'], page)

    def test_the_dashboard_poll_already_carries_it(self):
        """The page that most needs to notice a deploy asks every five
        seconds anyway. A second request beside it, on the same timer, for
        seven bytes, would be traffic for nothing."""
        _head, body = self.get('/api/state')
        state = json.loads(body)
        self.assertIn('build', state)
        _head, served = self.get('/api/build')
        self.assertEqual(state['build'], json.loads(served)['build'])

    def test_the_answer_is_never_cached(self):
        """A cached freshness check is a contradiction: it would go stale in
        exactly the case it exists to report."""
        head, _ = self.get('/api/build')
        self.assertIn('no-store', head)
        self.assertNotIn('max-age', head)

    def test_it_names_the_bytes_and_not_the_version(self):
        """An rsync onto a running server is how this actually gets deployed,
        and it does not touch the package version. Naming the build after a
        version string would leave the one deployment path that matters
        reporting no change at all -- which is the bug."""
        import shutil
        import tempfile

        original = web.ASSETS
        seen = list(web._build_seen)
        room = tempfile.mkdtemp()
        try:
            for name in web._VOLATILE_ASSETS:
                with open(os.path.join(room, name), 'w') as handle:
                    handle.write('first')
            web.ASSETS = room
            web._build_seen.clear()
            before = web.build_id()

            web._build_seen.clear()
            self.assertEqual(web.build_id(), before,
                             'the same bytes got two names')

            with open(os.path.join(room, 'app.js'), 'w') as handle:
                handle.write('second, and a different length')
            web._build_seen.clear()
            self.assertNotEqual(web.build_id(), before,
                                'a changed asset went unnoticed')
        finally:
            web.ASSETS = original
            web._build_seen[:] = seen
            shutil.rmtree(room, ignore_errors=True)

    def test_a_missing_asset_is_a_name_rather_than_a_crash(self):
        """This runs on the request path of every page load. Whatever a
        half-finished rsync leaves behind, the page still has to serve."""
        import shutil
        import tempfile

        original = web.ASSETS
        seen = list(web._build_seen)
        room = tempfile.mkdtemp()
        try:
            web.ASSETS = room                      # nothing in it at all
            web._build_seen.clear()
            self.assertTrue(web.build_id())
        finally:
            web.ASSETS = original
            web._build_seen[:] = seen
            shutil.rmtree(room, ignore_errors=True)

    def test_the_console_asks_beside_itself_and_not_under_itself(self):
        """The console is at /console/ and the api is its sibling. `api/build`
        would resolve to /console/api/build, which is an asset name, which is
        a 404 -- and a check that always fails quietly is a check that is not
        running. Relative, because the prefix `tailscale serve --set-path`
        adds is not something this page can know."""
        _head, script = self.get('/console/app.js')
        source = script.decode('utf-8')
        self.assertIn("'../api/build'", source)
        _head, body = self.get('/api/build')
        self.assertTrue(json.loads(body)['build'])

    def test_the_notice_offers_the_one_action_that_fixes_it(self):
        """A notice you cannot act on is a worse version of no notice."""
        for path, ident in (('/', 'update'), (web.CONSOLE, 'newbuild')):
            with self.subTest(page=path):
                _head, body = self.get(path)
                page = body.decode('utf-8')
                self.assertIn(f'id="{ident}"', page)
                self.assertIn('hidden', page[page.index(f'id="{ident}"'):
                                             page.index(f'id="{ident}"') + 200])
        for asset in ('/console/app.js', '/dash.js'):
            with self.subTest(script=asset):
                _head, script = self.get(asset)
                self.assertIn('location.reload()', script.decode('utf-8'))

    def test_the_console_asks_again_every_time_it_comes_back(self):
        """Resuming from a snapshot is the moment a stale page reappears, so
        it is the moment worth asking. Checking only at load would never fire
        on the one path that produces the problem."""
        source = self.get('/console/app.js')[1].decode('utf-8')
        handler = _extract(source, 'checkBuild')
        self.assertIn('api/build', handler)
        visible = source[source.index("addEventListener('visibilitychange'"):]
        self.assertIn('checkBuild()', visible[:400])

    def test_the_notice_overlays_the_terminal_instead_of_resizing_it(self):
        """Every row of this layout is a row the terminal does not get, and a
        notice that reflowed the pty to announce itself would be a worse bug
        than the one it reports. Measured at 390x844: the bar appears, the
        terminal stays 495px and 45 rows.
        """
        _head, body = self.get(web.CONSOLE)
        css = re.sub(r'/\*.*?\*/', '', body.decode('utf-8'), flags=re.S)
        block = re.search(r'#overlays \{(.*?)\}', css, re.S).group(1)
        self.assertIn('position: absolute', block)
        # The empty strip must not eat the gesture that reads the scrollback.
        self.assertIn('pointer-events: none', block)
        self.assertIn('#overlays > button { pointer-events: auto; }', css)

    def test_two_things_talking_at_once_stack_rather_than_hide_each_other(self):
        """Both are plausible together: you scroll up to read something, the
        app goes to the background, and it comes back with a deploy waiting.
        Pinned separately to top: 0 they were exactly on top of each other,
        and the one underneath -- always the newer of the two -- was simply
        never seen.

        Measured with both showing: history 0-35, the build notice 35-69, and
        the terminal still 495px of 45 rows.
        """
        _head, body = self.get(web.CONSOLE)
        page = body.decode('utf-8')
        wrap = page[page.index('<div id="overlays">'):]
        wrap = wrap[:wrap.index('</div>')]
        self.assertEqual(re.findall(r'<button id="(\w+)"', wrap),
                         ['hist', 'newbuild'])
        css = re.sub(r'/\*.*?\*/', '', page, flags=re.S)
        for ident in ('#hist', '#newbuild'):
            with self.subTest(bar=ident):
                block = re.search(re.escape(ident) + r' \{(.*?)\}',
                                  css, re.S).group(1)
                self.assertNotIn('position: absolute', block,
                                 'pinned to the top again, so it stacks on '
                                 'the other one instead of under it')
                self.assertIn('display: block', block)
                # A hidden bar has to take no space, or one that never fires
                # still costs the terminal a row.
                self.assertIn(ident + '[hidden] { display: none; }', css)

    def test_the_quiet_stamp_costs_the_terminal_nothing(self):
        """It sits in the drawer's own scroll. Anywhere else on this page is
        a row of a layout whose whole point is that the terminal gets what is
        left over."""
        _head, body = self.get(web.CONSOLE)
        page = body.decode('utf-8')
        sheet = page[page.index('<div id="sheet">'):page.index('id="grab"')]
        self.assertIn('id="buildline"', sheet)


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

    def test_the_terminal_does_not_hand_the_swipe_back_to_the_browser(self):
        """`touch-action: pan-y` declares the vertical drag to be the
        browser's, which is the exact gesture that pages the scrollback.

        A declared pan arrives uncancelable and on iOS can end in
        touchcancel, so the listener runs and achieves nothing -- and no test
        that synthesises touches can catch it, because CDP-dispatched events
        bypass the compositor's gesture arbitration entirely. It survived one
        deploy that way. Nothing under #term ever scrolled natively: xterm 6
        removed the scrollable viewport and #app is position:fixed.
        """
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        term = re.search(r'#term \{[^}]*\}', css).group(0)
        self.assertIn('touch-action: none', term)
        self.assertNotIn('pan-y', css)

    def test_the_small_text_clears_the_contrast_floor(self):
        """Two of these did not. The drawer's section headings measured
        3.96:1 and the grip strip 3.64:1 against their own backgrounds, where
        text this small needs 4.5:1 -- and the headings are the only
        navigation a list of sixty keys has, while the grip is the only way
        back to the pad once it is hidden.

        Computed from the stylesheet rather than pinned to the hex values, so
        this stays true through a repaint.
        """
        def luminance(hexcolour):
            parts = [int(hexcolour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            parts = [c / 12.92 if c <= 0.03928
                     else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
            return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

        def ratio(fg, bg):
            first, second = sorted((luminance(fg), luminance(bg)),
                                   reverse=True)
            return (first + 0.05) / (second + 0.05)

        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)

        # The palette is named once in :root and used by reference, which is
        # the whole point -- three hand-picked dark greys is how one panel
        # ends up looking like three. So the token has to be resolved before
        # it can be measured.
        tokens = dict(re.findall(r'(--[\w-]+): (#[0-9a-fA-F]{6});', css))

        def colour_of(selector):
            rule = re.search(re.escape(selector) + r' \{[^}]*\}', css)
            self.assertIsNotNone(rule, selector)
            found = re.search(r'color: (#[0-9a-fA-F]{6}|var\(--[\w-]+\))',
                              rule.group(0))
            self.assertIsNotNone(found, f'{selector} sets no colour')
            value = found.group(1)
            if value.startswith('var('):
                name = value[4:-1]
                self.assertIn(name, tokens, f'{selector} uses undefined {name}')
                return tokens[name]
            return value

        # Backgrounds are the panel behind each: the sheet is flat #17171d so
        # a pinned heading can sit on it invisibly, and the grip is #141418.
        for selector, background in (('.ghead', '#17171d'),
                                     ('body.touch.nopad #grip', '#141418')):
            with self.subTest(selector=selector):
                measured = ratio(colour_of(selector), background)
                self.assertGreaterEqual(
                    round(measured, 2), 4.5,
                    f'{selector} is {measured:.2f}:1 against {background}')

    def test_the_one_state_banner_is_announced_and_can_be_focused(self):
        """It was a <div> with a click handler: the app's only you-are-not-
        live indicator, invisible to a screen reader, while every key around
        it carried an aria-label."""
        banner = re.search(r'<button id="hist"[^>]*>', self.html)
        self.assertIsNotNone(banner, '#hist is not a button')
        self.assertIn('aria-live="polite"', banner.group(0))
        self.assertIn('aria-label=', banner.group(0))

    def test_the_client_speaks_one_language(self):
        """Two strings arrived in Chinese in an interface that says
        `connected`, `reconnecting…`, `no sessions` and `tap keys to keep
        them` everywhere else. Either is a fine choice; a mixture is not
        one."""
        strings = re.findall(r"'([^'\\\n]*)'", self.js)
        chinese = [s for s in strings
                   if any('一' <= ch <= '鿿' for ch in s)]
        self.assertEqual(chinese, [])

    def test_a_gesture_the_system_takes_away_does_not_seed_the_next_one(self):
        """iOS fires touchcancel rather than touchend when it claims a drag.
        Leftover state from a drag that never ended reads a later pinch as a
        swipe."""
        self.assertIn('touchcancel', self.js)

    def test_no_key_stretches_into_something_it_is_not(self):
        """flex-wrap stretched whichever key landed alone on the last line
        into a full-width button, which read as something important rather
        than as the leftover it was -- and the one it did that to was `q`,
        which quits.

        The fault is wrapping *a row whose children grow*, not wrapping. It
        was written as "no flex-wrap anywhere", which is a bigger rule than
        the reason for it, and it made the settings row -- where nothing
        grows -- unable to rescue a 320px phone from losing `hide` off the
        right edge. So it is stated as what it means: keys never wrap, and
        anything that does wrap has nothing in it that can stretch.
        """
        self.assertNotIn('.krow.wrap', self.html)
        # Comments may still explain why; the declaration must be gone.
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        self.assertRegex(css, r'\.krow \.key \{[^}]*flex: 1 1 0')
        krow = re.search(r'\.krow \{(.*?)\}', css, re.S).group(1)
        self.assertNotIn('flex-wrap', krow, 'the key rows may never wrap')
        # Whatever else wraps must not be able to stretch a lone leftover.
        for block in re.findall(r'([#.][\w-]+) \{([^}]*flex-wrap: wrap[^}]*)\}',
                                css):
            name = block[0]
            with self.subTest(wraps=name):
                self.assertNotIn('.krow', name)
                kids = re.search(re.escape(name) + r' \w+ \{(.*?)\}', css,
                                 re.S)
                if kids:
                    self.assertNotIn('flex: 1 1', kids.group(1),
                                     f'{name} wraps and its children grow')


    def test_detach_moved_to_the_side_that_can_offer_it_all_the_time(self):
        """Ctrl-B then d: the one key nobody can guess, and being stuck inside
        a session is the failure this whole feature would otherwise create.

        It used to be published from python, with ^C, ^D, ^Z, PgUp and PgDn.
        Every one of those describes a *terminal* rather than atmux, and a
        copy of the terminal's vocabulary kept on the app's side is exactly
        what the published list exists to avoid. Worse, it only ever arrived
        while atmux was suspending: in a bare shell nothing published, and
        there was no detach at all. The client owns it now, which is also why
        it is built from the prefix instead of being spelled out -- a rebound
        prefix moves every chord at once. See KeypadVocabularyTests.
        """
        self.assertEqual(list(keypad.EXTERNAL_KEYS), [])
        self.assertNotIn('x02d', self.js)
        self.assertIn("{ l: 'detach', s: 'd' }", self.js)

    def test_what_the_app_cannot_be_asked_travels_instead(self):
        """The prefix byte. A client cannot work it out and `set -g prefix
        C-a` is a rebinding nothing on the wire announces, so twelve tmux
        buttons would quietly mean something else."""
        payload = keypad.decode(
            keypad.encode('external', keypad.EXTERNAL_KEYS,
                          keypad.tmux_prefix({})))
        self.assertEqual(payload['prefix'], '\x02')
        self.assertIn('data.prefix', self.js)

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
        """The keys worth repeating are the ones that move or remove
        something a step at a time -- the CSI sequences and backspace.
        Deriving it means nothing has to remember to mark a new one."""
        body = _extract(self.js, 'buildKey')
        # The literal /^\x1b\[/ -- a CSI prefix, not a hand-kept flag.
        self.assertIn('/^\\x1b\\[/', body)
        self.assertIn("'\\x7f'", body)
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


class TrailingSlashTests(_ServedFixture):
    """The address `tailscale serve` prints has no trailing slash.

    `https://host/term` and `https://host/term/` are different base URLs: with
    no slash the browser treats `term` as a filename and resolves `dash.js`
    against the host root, where nothing is mounted. Measured against the live
    tailnet front, all four forms:

        /term/          7 rows            ok
        /term           404 /dash.js      header, two buttons, no list
        /term/console/  terminal drawn    ok
        /term/console   404 /console/     "404 page not found"

    Both failures are one mistake -- assuming this server is mounted at the
    root -- made in two places, and the second page was the one people open.
    """

    PAGES = ('index.html', 'dash.html')

    def test_both_pages_can_put_the_slash_back(self):
        """The console had the bootstrap from the start. The dashboard did
        not, so it was the one page that could not correct the address, and
        it is the one the printed URL lands on."""
        for name in self.PAGES:
            with self.subTest(page=name):
                with open(os.path.join(web.ASSETS, name), encoding='utf-8') as f:
                    self.assertIn(web._BOOTSTRAP_SLOT, f.read())

    def test_the_server_fills_the_slot_in_on_the_way_out(self):
        """A slot left as a comment is a page that still cannot correct."""
        for path, marker in (('/', b'dash.js'), (web.CONSOLE, b'app.js')):
            with self.subTest(path=path):
                head, body = self.get(path)
                self.assertIn('200', head)
                self.assertIn(marker, body)
                self.assertIn(web.BOOTSTRAP_JS.encode('utf-8'), body)
                self.assertNotIn(web._BOOTSTRAP_SLOT.encode('ascii'), body)

    def test_the_redirect_does_not_claim_to_know_where_it_is_mounted(self):
        """`Location: /console/` is such a claim, and behind --set-path the
        prefix is stripped before we ever see it, so the browser was sent to
        a path nothing serves. A relative target resolves against whatever it
        actually asked for."""
        head, _ = self.get(web.CONSOLE.rstrip('/'))
        self.assertIn('302', head)
        location = re.search(r'(?im)^Location:\s*(\S+)', head)
        self.assertIsNotNone(location, head)
        target = location.group(1)
        self.assertFalse(target.startswith('/'), target)
        self.assertNotRegex(target, r'^[a-z]+://')
        self.assertEqual(target, 'console/')

    def test_the_browser_rule_lands_where_it_should_under_any_mount(self):
        """urljoin is the rule a browser applies to a relative Location."""
        from urllib.parse import urljoin
        for asked, expected in (
                ('https://h/console', 'https://h/console/'),
                ('https://h/term/console', 'https://h/term/console/'),
                ('https://h/a/b/console', 'https://h/a/b/console/')):
            with self.subTest(asked=asked):
                self.assertEqual(urljoin(asked, 'console/'), expected)

    def test_the_dashboard_loads_nothing_by_an_absolute_path(self):
        """One absolute reference is a page that only works at the root --
        which is the whole bug, in the other direction."""
        with open(os.path.join(web.ASSETS, 'dash.html'), encoding='utf-8') as f:
            html = f.read()
        for reference in re.findall(r'(?:src|href)="([^"]+)"', html):
            with self.subTest(reference=reference):
                self.assertFalse(reference.startswith('/'), reference)
                self.assertNotRegex(reference, r'^[a-z]+://')


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


# Enough of a DOM to run the pad's own rendering. Not a mock of what it does:
# the real functions come out of app.js and build real trees in this, so a key
# that stops being drawn stops being drawn here too.
_DOM_STUB = r'''
function El(tag) {
  this.tag = tag; this.children = []; this.attrs = {}; this.style = {};
  this._text = ''; this._cls = {}; this._on = {};
  // What a scrolling box knows about itself. The sheet decides whether it is
  // showing its own last row from these, and takes the fade off when it is.
  this.scrollHeight = 0; this.clientHeight = 0; this._scrollTop = 0;
  this.parentNode = null;
  var self = this;
  this.classList = {
    add: function (n) { self._cls[n] = 1; },
    remove: function (n) { delete self._cls[n]; },
    toggle: function (n, on) {
      if (on) self._cls[n] = 1; else delete self._cls[n];
    },
    contains: function (n) { return !!self._cls[n]; }
  };
}
Object.defineProperty(El.prototype, 'className', {
  set: function (v) {
    var self = this; this._cls = {};
    String(v).split(/\s+/).forEach(function (n) { if (n) self._cls[n] = 1; });
  },
  get: function () { return Object.keys(this._cls).join(' '); }
});
Object.defineProperty(El.prototype, 'textContent', {
  set: function (v) { this._text = String(v); this.children = []; },
  get: function () { return this._text; }
});
Object.defineProperty(El.prototype, 'childElementCount', {
  get: function () { return this.children.length; }
});
// Clamped, like the real thing. A plain number here let a glide keep adding
// to a box that had nowhere left to go, so "stop at the end instead of
// grinding on it" could not be caught -- the stub was more permissive than a
// browser, which is the wrong direction for a stub to be wrong in.
Object.defineProperty(El.prototype, 'scrollTop', {
  set: function (v) {
    var max = Math.max(0, (this.scrollHeight || 0) - (this.clientHeight || 0));
    this._scrollTop = Math.max(0, Math.min(max, Number(v) || 0));
  },
  get: function () { return this._scrollTop; }
});
El.prototype.appendChild = function (c) {
  this.children.push(c); c.parentNode = this; return c;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
El.prototype.addEventListener = function (type, fn) {
  (this._on[type] = this._on[type] || []).push(fn);
};
// A gesture, delivered the way the browser delivers one. The pad decides
// whether a touch was a tap or a scroll from these alone, so the tests have
// to be able to send them.
El.prototype.emit = function (type, event) {
  var detail = Object.assign(
    { preventDefault: function () {}, pointerType: 'touch',
      clientX: 0, clientY: 0 }, event || {});
  (this._on[type] || []).forEach(function (fn) { fn(detail); });
};
// Width is the one measurement the pad reads back out of the layout: it is
// what decides how many keys go across. Height is the second: the sheet is
// capped at the room the terminal actually has, and whether the pad's own
// height changed is what decides whether the pty is resized at all.
El.prototype.getBoundingClientRect = function () {
  return { width: this._width || 0, height: this._height || 0 };
};
// A text node, because the page writes its live readouts through nodeValue
// rather than textContent: replacing an element's children is a structural
// change, and on iOS a structural change mid-gesture ends the gesture.
function TextNode(v) { this.nodeValue = String(v); this.parentNode = null; }
var document = { createElement: function (t) { return new El(t); },
                 createTextNode: function (v) { return new TextNode(v); },
                 body: new El('body') };

// What the pad needs around it, and nothing more. Every one of these is a
// thing the browser owns, not a thing the keypad decides.
var keys = new El('div'), nav = new El('div');
// The drawer's edge, and the only control that opens it. `grabCount` is the
// word beside the grip: `37 more keys` closed, `close` open -- it used to be
// a bare `⌄ 38` four buttons away from a near-identical `▾`.
var expander = new El('button'), grabCount = new El('b');
var sheetTop = new El('div');
var hist = new El('button');
var pinRow = new El('div'), editButton = new El('button');
// The pad, the terminal it sits under, and the sheet that covers that
// terminal without displacing it. `pad` is measured before and after every
// render: whether its height changed is what decides whether the pty is
// resized, and the whole point of the sheet is that opening it does not.
var pad = new El('div'), host = new El('div');
var sheet = new El('div'), sheetKeys = new El('div');
// The last line of the drawer's own scroll: which build this page is. Height,
// not a stub of zero -- it is counted into the drawer's content precisely so
// that it cannot end up just under the fold of a drawer reporting itself as
// exactly full.
var buildLine = new El('div');
buildLine._height = 28;
// The pad's height is the rows it lays out, which is the thing the page reads
// it for: whether that number changed is what decides whether the pty gets
// resized. Modelled rather than stubbed at a constant, because a constant
// would let "opening the sheet does not resize the terminal" pass for the
// wrong reason -- and the sheet is deliberately not in this sum, since it
// covers the terminal instead of displacing it.
pad.getBoundingClientRect = function () {
  var rows = 0;
  [nav, pinRow, keys].forEach(function (box) {
    if (box.style.display === 'none') return;
    rows += box.children.filter(function (c) { return c._cls.krow; }).length;
  });
  return { width: keys._width || 0, height: 44 * rows + 51 };
};
var current = [], expanded = false;
var sent = [];
// Enough of localStorage to prove a choice is remembered, and to be thrown
// garbage the way a hand-edited one would be.
var store = {};
var localStorage = {
  getItem: function (k) { return k in store ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; }
};
// Enough of xterm to answer the one question the drawer asks it: which row
// is being written on. Everything below that row is blank, or tmux's filler,
// or a status line, and the drawer may cover all three.
var term = {
  rows: 46,
  buffer: { active: { cursorY: 44, viewportY: 0,
                      getLine: function (y) {
                        return { translateToString: function () {
                          return y <= 44 ? 'x' : ''; } };
                      } } }
};

// A browser global like the two above it. The page reads it once, to decide
// whether ?debug=1 asked for the readout; without it the module throws on
// load and every test in here fails for a reason that has nothing to do
// with what it is testing.
var location = { search: '' };
function say() {}
function refit() {}
function haptic() {}
function sendText(text) { sent.push(text); }

function buttons(node, out) {
  if (node._cls.key && !node._cls.gap) out.push(node);
  node.children.forEach(function (child) { buttons(child, out); });
  return out;
}
function keyAt(node, n) { return buttons(node, [])[n]; }
function keyLabelled(node, label) {
  return buttons(node, []).find(function (e) { return e.textContent === label; });
}

function drawn(node, out) {
  if (node._cls.key && !node._cls.gap) out.push(node.textContent);
  node.children.forEach(function (child) { drawn(child, out); });
  return out;
}
function rows(node, out) {
  if (node._cls && node._cls.krow) out.push(node);
  node.children.forEach(function (child) { rows(child, out); });
  return out;
}
function headings(node, out) {
  if (node._cls.ghead) out.push(node.textContent);
  node.children.forEach(function (child) { headings(child, out); });
  return out;
}
'''


@unittest.skipUnless(_node(), 'needs a javascript runtime')
class KeypadVocabularyTests(unittest.TestCase):
    """The keys the *client* owns, run rather than restated.

    The split is the design: what the app can do is published by the app and
    never written in javascript, because a copy is only correct on the day it
    is written. What a terminal can do is written in javascript and never
    published, because it is true of every program and has to work when
    nothing is publishing -- in a bare shell, during a reconnect, after the
    handover to tmux.

    The regression these exist for is the second half being neglected. It
    once held three keys (↑ ↓ esc), which is a row chosen for one screen of
    this dashboard: no tab, no ← →, no ctrl, and no way to reach a single
    tmux binding from inside the session you had just attached to.
    """

    TABLES = ('NAV_KEYS', 'TMUX_VERBS', 'CTRL_KEYS', 'MOVE_KEYS', 'TYPE_KEYS')
    FUNCTIONS = ('bufferType', 'swipeKind',
                 'showDebug', 'feed', 'holdWrites', 'flushHeld', 'flushSoon',
                 'swipeBy', 'emitScroll', 'scrollSoon', 'payScroll',
                 'lineHeight', 'wheelReport', 'cellAt', 'duringLabel',
                 'typed', 'paintHistory', 'enterHistory',
                 'leaveHistory',
                 'ctrlify', 'applyLatch', 'paintLatch', 'press',
                 'clock', 'frame', 'stopGlide', 'dragSheet',
                 'releaseDrawer', 'glideStep',
                 'loadPins', 'savePins', 'togglePin', 'own', 'pinnedKeys',
                 'buildModifier', 'buildKey', 'tmuxKeys', 'groups', 'perRow',
                 'renderRows', 'recolumn', 'renderNav', 'band', 'renderPins',
                 'firstRow',
                 'renderKeys', 'setEditing', 'toggleDrawer', 'setPad',
                 'padCover', 'sheetBase', 'keepVisibleRow', 'freeSpace',
                 'neededShift', 'reshiftSoon', 'sheetContent',
                 'sheetRowHeight', 'sheetRoom', 'sheetPeek', 'sheetFloor',
                 'shiftTerminal',
                 'sizeSheet', 'markSheetEnd')
    SCALARS = ('var published =', 'var debugBox =', 'var moves =',
               'var held =', 'var SLOP =', 'var scrolledBack =',
               'var mouseOn =', 'var GAIN =', 'var lastX =',
               'var owed =', 'var NAV_PER_ROW =',
               'var OFF =', 'var ROWS_COLLAPSED =',
               'var TERM_KEEP =', 'var navColumns =',
               'var SHEET_PEEK_ROWS =',
               'var CURSOR_MARGIN =', 'var shiftedBy =',
               'var reshiftTimer =',
               'var glideTimer =', 'var GLIDE_WINDOW =', 'var GLIDE_MIN =',
               'var GLIDE_DECAY =', 'var SHEET_SLOP =', 'var sheetDragged =',
               'var sheetHeight =', 'var GRAB_SLOP =',
               'var columnsUsed =', 'var latch =', 'var modButtons =',
               'var prefixSeq =', 'var PINS =',
               'var DEFAULT_PINS =', 'var pins =',
               'var PAD_STATE =')

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.js = f.read()
        with open(os.path.join(web.ASSETS, 'index.html'), encoding='utf-8') as f:
            cls.html = f.read()
        parts = [_DOM_STUB]
        for head in cls.SCALARS:
            match = re.search(re.escape(head) + r'[^;]*;', cls.js)
            assert match, head
            parts.append(match.group(0))
        parts += [_extract_list(cls.js, name) for name in cls.TABLES]
        parts += [_extract(cls.js, name) for name in cls.FUNCTIONS]
        cls.harness = '\n'.join(parts)

    def run_js(self, body):
        import subprocess
        out = subprocess.run([_node(), '-e', self.harness + '\n' + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def table(self, name):
        return self.run_js(f'console.log(JSON.stringify({name}));')

    # ── the persistent row ────────────────────────────────────────────────

    def test_the_row_covers_what_a_terminal_needs_and_a_phone_lacks(self):
        """Tab is completion, ← → is editing a command line, and Enter is how
        you run the command you just recalled with ↑. None of the three was
        reachable at all once the software keyboard was down."""
        nav = self.table('NAV_KEYS')
        bytes_for = {e['l']: e.get('k') for e in nav}
        self.assertEqual(bytes_for.get('tab'), '\t')
        self.assertEqual(bytes_for.get('⏎'), '\r')
        self.assertEqual(bytes_for.get('esc'), '\x1b')
        self.assertEqual([bytes_for.get(g) for g in ('←', '↓', '↑', '→')],
                         ['\x1b[D', '\x1b[B', '\x1b[A', '\x1b[C'])

    def test_the_prefix_latches_like_the_other_modifiers(self):
        """The prefix button this once had sent C-b and then wanted a letter,
        which meant raising the keyboard over the screen you were acting on.
        A latch survives the keyboard and can be locked for a run of chords,
        and it turns four verbs someone chose for you into tmux's whole
        binding table."""
        mods = {e['l']: e for e in self.table('NAV_KEYS') if 'm' in e}
        self.assertEqual(sorted(mods), ['alt', 'ctrl', 'pfx'])
        self.assertEqual(mods['pfx']['m'], 'prefix')
        self.assertNotIn('k', mods['pfx'])

    def test_the_prefix_comes_before_the_key_rather_than_rewriting_it(self):
        """ctrl and alt rewrite a character and so have to refuse anything
        that is already a sequence. The prefix is a keystroke that precedes
        the next one, so it composes with the arrows: `prefix ←` is
        select-pane -L, and refusing it would be a modifier that works on
        some keys and silently not on others."""
        self.assertEqual(self.run_js('''
            prefixSeq = '\\x02';
            latch = { ctrl: OFF, alt: OFF, prefix: ARMED };
            var arrow = applyLatch('\\x1b[D');
            latch.prefix = ARMED;
            var letter = applyLatch('c');
            latch.prefix = LOCKED;
            var held1 = applyLatch('n'), held2 = applyLatch('n');
            console.log(JSON.stringify([arrow, letter, held1, held2,
                                        latch.prefix]));'''),
            ['\x02\x1b[D', '\x02c', '\x02n', '\x02n', 2])

    def test_every_control_is_big_enough_to_hit(self):
        """The bottom row was left at 40x34 when the keys were fixed -- under
        the minimum in BOTH directions, which is why it was the one row you
        had to aim at. Measured on a 390px phone after: 44x44, and seven of
        them plus gaps and padding is 363px, so they fit."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        # The rules that size them, not merely the ones that mention them --
        # the reduced-motion block names the same selector earlier. Two size
        # it now: the default, and the short-landscape override.
        sized = [r for r in re.findall(r'#tabs button \{[^}]*\}', css)
                 if 'min-height' in r]
        self.assertEqual(len(sized), 2, css.count('#tabs button'))
        for want in ('min-height: 44px', 'min-width: 44px'):
            with self.subTest(want=want):
                self.assertIn(want, sized[0])

    def test_landscape_spends_height_and_keeps_width(self):
        """A phone in landscape is 390px tall and four rows of controls do not
        fit in it at 44 -- but width is the direction a thumb aims, and ten
        keys across 844px are 79px wide, so the target ends up larger than the
        162x44 monsters it replaces. 40px is Android's minimum where 44 is
        Apple's, and it is spent only where the screen is genuinely short."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        block = re.search(
            r'@media \(orientation: landscape\) and \(max-height: (\d+)px\)'
            r' \{(.*?)\n  \}\n', css, re.S)
        self.assertIsNotNone(block, 'no landscape rules at all')
        self.assertLessEqual(int(block.group(1)), 600)
        body = block.group(2)
        self.assertIn('min-height: 40px', body)
        self.assertNotIn('min-width', body)
        # The cap is what pushed the settings row off the bottom of the
        # screen: with the drawer overlaying rather than displacing, the pad
        # is four rows of fixed height and nothing may squeeze them.
        self.assertIn('max-height: none', body)

    def test_the_landscape_rules_come_last_so_they_actually_win(self):
        """They did not. Written above the block that sizes a key, every one
        of these lost the cascade at equal specificity and the whole media
        query silently did half its job -- the padding shrank and the keys
        stayed 44px tall."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        landscape = css.index('@media (orientation: landscape)')
        for selector in ('.key {', '#tabs button {', '#nav {', '#keys {',
                         '#pins {', '.krow {'):
            with self.subTest(selector=selector):
                self.assertLess(css.index(selector), landscape,
                                f'{selector} is defined after the override')

    def test_the_row_is_wide_enough_to_aim_at(self):
        """`.key` asks for a 44px minimum touch target and got it in height
        only: ten keys across a 390px phone measured 33px wide -- a quarter
        under the minimum in the direction you actually aim. Five is 71px,
        measured on the same viewport."""
        self.assertEqual(self.run_js("""
            nav = new El('div'); navColumns = 0;
            renderNav(perRow(390));
            console.log(JSON.stringify(nav.children.map(function (row) {
              return buttons(row, []).map(function (k) { return k.textContent; });
            })));"""),
            [['esc', 'ctrl', 'alt', 'pfx', 'tab'], ['⏎', '←', '↓', '↑', '→']])

    def test_landscape_puts_the_same_ten_keys_on_one_row(self):
        """Measured at 844x390 before this: two rows of five made each key
        162px wide -- four times what a thumb needs -- and cost 51px of a
        390px-tall screen to do it. The terminal had twelve rows, which is
        less room than the keys attached to it."""
        self.assertEqual(self.run_js("""
            nav = new El('div'); navColumns = 0;
            renderNav(perRow(844));
            console.log(JSON.stringify(nav.children.map(function (row) {
              return buttons(row, []).map(function (k) { return k.textContent; });
            })));"""),
            [['esc', 'ctrl', 'alt', 'pfx', 'tab',
              '⏎', '←', '↓', '↑', '→']])

    def test_an_armed_modifier_survives_the_row_being_rebuilt(self):
        """Rotating rebuilds the navigation row, and the buttons showing a
        latched ctrl are destroyed with it. Without repainting the state onto
        the new ones, ctrl stays armed invisibly and the next key you tap
        comes out as a control character."""
        self.assertEqual(self.run_js("""
            nav = new El('div'); navColumns = 0;
            renderNav(5);
            latch.ctrl = LOCKED;
            paintLatch();
            renderNav(10);
            var ctrl = keyLabelled(nav, 'ctrl');
            console.log(JSON.stringify(
              [nav.children.length, !!ctrl._cls.on, applyLatch('c')]));"""),
            [1, True, '\x03'])

    def test_a_fixed_key_acts_on_the_way_down(self):
        """The fixed rows cannot scroll, so waiting for the finger to lift is
        latency and nothing else -- a real key acts on press. The drawer keeps
        release, because a drag through it is how it scrolls and typing what
        you dragged past would be worse than the wait."""
        self.assertEqual(self.run_js("""
            sent = [];
            var fixed = buildKey({l: 'tab', k: '\\t'}, true);
            fixed.emit('pointerdown', {}); var onDown = sent.slice();
            fixed.emit('pointerup', {});   var afterUp = sent.slice();
            sent = [];
            var drawer = buildKey({l: 'tab', k: '\\t'});
            drawer.emit('pointerdown', {}); var quiet = sent.slice();
            drawer.emit('pointerup', {});
            console.log(JSON.stringify([onDown, afterUp, quiet, sent]));"""),
            [['\t'], ['\t'], [], ['\t']])

    def test_a_fixed_key_still_chooses_rather_than_types_while_editing(self):
        """Editing is picking what to keep. A key that typed on the way down
        because it happens to sit in a fixed row would be the exact bug the
        whole editing mode exists to prevent."""
        self.assertEqual(self.run_js("""
            sent = []; editing = true;
            var fixed = buildKey({l: 'tab', k: '\\t'}, true);
            fixed.emit('pointerdown', {});
            fixed.emit('pointerup', {});
            editing = false;
            console.log(JSON.stringify(sent));"""), [])

    def test_a_modifier_is_a_latch_rather_than_a_byte(self):
        """There is no byte for ctrl. A key that claimed to send one would
        send nothing, look pressed, and be indistinguishable from broken."""
        mods = {e['l']: e for e in self.table('NAV_KEYS') if 'm' in e}
        self.assertEqual(sorted(mods), ['alt', 'ctrl', 'pfx'])
        for entry in mods.values():
            self.assertNotIn('k', entry)

    def test_the_row_never_reflows(self):
        """Nothing below it may change its length: a key that moves because
        the far end published a different number of actions is a key you have
        to look for every time."""
        self.assertNotIn('NAV_KEYS.push', self.js)
        self.assertNotIn('NAV_KEYS =', self.js.replace('var NAV_KEYS =', ''))

    # ── the latch ─────────────────────────────────────────────────────────

    def test_ctrl_folds_a_character_to_its_control_code(self):
        pairs = {'a': '\x01', 'c': '\x03', 'z': '\x1a', 'A': '\x01',
                 'r': '\x12', '@': '\x00', ' ': '\x00', '[': '\x1b',
                 '\\': '\x1c', ']': '\x1d', '^': '\x1e', '_': '\x1f',
                 '?': '\x7f'}
        got = self.run_js(
            'console.log(JSON.stringify(' +
            json.dumps(list(pairs)) + '.map(ctrlify)));')
        self.assertEqual(got, list(pairs.values()))

    def test_a_character_with_no_control_code_is_left_alone(self):
        """Rather than folded into whatever byte happens to be nearby."""
        self.assertEqual(self.run_js(
            'console.log(JSON.stringify(["1", "!", "é"].map(ctrlify)));'),
            ['1', '!', 'é'])

    def test_an_armed_modifier_applies_once_and_then_clears(self):
        """One-shot is the whole point: a ctrl that stayed on would turn the
        rest of what you typed into control codes, silently."""
        self.assertEqual(self.run_js('''
            latch.ctrl = ARMED;
            var first = applyLatch('c'), second = applyLatch('c');
            console.log(JSON.stringify([first, second, latch.ctrl]));'''),
            ['\x03', 'c', 0])

    def test_a_locked_modifier_keeps_applying_until_it_is_cleared(self):
        self.assertEqual(self.run_js('''
            latch.ctrl = LOCKED;
            console.log(JSON.stringify(
                [applyLatch('a'), applyLatch('b'), latch.ctrl]));'''),
            ['\x01', '\x02', 2])

    def test_a_modifier_never_mangles_a_sequence_that_is_already_one(self):
        """An arrow is an escape sequence and a tmux chord is two bytes.
        Folding either would send a byte nobody asked for -- the failure this
        whole module is written to avoid. They pass through and clear the
        latch, which is what a mis-tap deserves."""
        self.assertEqual(self.run_js('''
            latch.ctrl = ARMED;
            var arrow = applyLatch('\\x1b[A');
            latch.ctrl = ARMED;
            var chord = applyLatch('\\x02d');
            console.log(JSON.stringify([arrow, chord, latch.ctrl]));'''),
            ['\x1b[A', '\x02d', 0])

    def test_alt_sends_escape_then_the_key(self):
        self.assertEqual(self.run_js('''
            latch.alt = ARMED;
            console.log(JSON.stringify(applyLatch('b')));'''), '\x1bb')

    def test_the_two_modifiers_compose(self):
        """M-C-b is a real chord and holding both is not a gesture a thumb
        has, which is the reason these latch at all."""
        self.assertEqual(self.run_js('''
            latch.ctrl = ARMED; latch.alt = ARMED;
            console.log(JSON.stringify(applyLatch('b')));'''), '\x1b\x02')

    def test_nothing_armed_leaves_what_you_typed_exactly_as_it_was(self):
        self.assertEqual(self.run_js(
            'console.log(JSON.stringify(applyLatch("hello world")));'),
            'hello world')

    # ── tmux ──────────────────────────────────────────────────────────────

    def test_every_tmux_button_is_the_prefix_and_one_key(self):
        """`prefix` alone was already offered and was very nearly useless: it
        sends C-b and then wants a letter, and a letter means raising the
        software keyboard over the screen you were trying to act on."""
        chords = self.run_js('console.log(JSON.stringify(tmuxKeys()));')
        self.assertEqual(len(chords), len(self.table('TMUX_VERBS')))
        for chord in chords:
            self.assertTrue(chord['k'].startswith('\x02'), chord)
            self.assertLessEqual(len(chord['k']), 2, chord)

    def test_detach_is_the_first_of_them(self):
        """It is the one key nobody can guess, and being stuck inside a
        session is the failure this whole feature would otherwise create. It
        has to survive into the two rows a closed drawer shows."""
        self.assertEqual(self.table('TMUX_VERBS')[0],
                         {'l': 'detach', 's': 'd'})

    def test_the_chords_follow_a_rebound_prefix(self):
        """`set -g prefix C-a` is a thing people do, and nothing on the wire
        announces it -- so the app says so, and every chord moves at once."""
        chords = self.run_js('''
            prefixSeq = '\\x01';
            console.log(JSON.stringify(tmuxKeys().map(function (c) {
              return c.k; })));''')
        self.assertTrue(all(c.startswith('\x01') for c in chords), chords)

    # ── the drawer ────────────────────────────────────────────────────────

    def render(self, published=(), expanded=False, width=390):
        """Whichever surface is showing.

        Two surfaces now, not one: #keys is a single row that is always in the
        layout, and the sheet is everything, over the terminal rather than
        displacing it. `row` is the first, `keys` is whichever one you are
        looking at.
        """
        return self.run_js(f'''
            current = {json.dumps(list(published))};
            expanded = {json.dumps(expanded)};
            keys._width = {width};
            renderKeys();
            var surface = expanded ? sheetKeys : keys;
            console.log(JSON.stringify({{
              keys: drawn(surface, []), heads: headings(surface, []),
              rows: rows(surface, []).length,
              row: drawn(keys, []),
              open: !!sheet._cls.open,
              more: grabCount.textContent }}));''')

    def test_a_closed_drawer_shows_the_apps_own_keys(self):
        """What you can do on the screen in front of you, two rows of it --
        which is what it has always shown and what it should keep showing."""
        published = [{'k': 'z', 'l': 'Layout'}, {'k': 's', 'l': 'SSH'}]
        self.assertEqual(self.render(published)['keys'], ['Layout', 'SSH'])

    def test_a_closed_drawer_falls_back_to_tmux_when_nothing_is_published(self):
        """Nothing published means the screen belongs to tmux or a shell.
        Showing an empty drawer there is what put detach behind a tap."""
        shown = self.render()['keys']
        self.assertEqual(shown[0], 'detach')
        # One row when collapsed, not two -- the pad was taking 45% of the
        # screen. Five across, because the whole panel is on one pitch now:
        # the navigation row above holds ten keys and has to divide evenly,
        # so it picks five, and this follows it. It used to be four here and
        # five above, and none of the vertical seams lined up.
        self.assertEqual(len(shown), 5)

    def test_an_open_drawer_reaches_every_section(self):
        published = [{'k': 'z', 'l': 'Layout'}]
        page = self.render(published, expanded=True)
        self.assertEqual(page['heads'], ['tmux', 'ctrl', 'move', 'type'])
        for label in ('Layout', 'detach', '^C', 'PgUp', '|'):
            self.assertIn(label, page['keys'])

    def test_an_open_drawer_holds_no_key_twice(self):
        """Two buttons that do the same thing is how a row starts reading as
        broken, and the app's own list is deduplicated for the same reason."""
        page = self.render([{'k': 'z', 'l': 'Layout'}], expanded=True)
        self.assertEqual(len(page['keys']), len(set(page['keys'])))

    def test_the_expander_is_offered_even_with_nothing_published(self):
        """It used to hide itself when the published list fitted, which in a
        bare shell -- where nothing is published at all -- left the pad at
        three keys and no way to reach any others."""
        self.assertTrue(self.render()['more'].endswith('more keys'))
        self.assertFalse(self.render()['more'].startswith('0 '))

    def test_the_count_says_what_it_counts(self):
        """`⌄ 38` was a chevron and a bare number sitting four buttons away
        from `▾`, a near-identical chevron meaning "hide the keypad". A number
        with no noun beside it is something you have to be told."""
        published = [{'k': 'z', 'l': 'Layout'}]
        closed = self.render(published)
        opened = self.render(published, expanded=True)
        self.assertEqual(int(closed['more'].split()[0]),
                         len(opened['keys']) - len(closed['keys']))
        self.assertIn('more keys', closed['more'])
        # And it says what tapping does once it is open, rather than showing
        # the same chevron upside down.
        self.assertEqual(opened['more'], 'close')

    # ── how many across ───────────────────────────────────────────────────

    def test_the_whole_panel_is_on_one_pitch(self):
        """Five or ten, and nothing between.

        Two rules that were each right alone: the navigation row is fixed at
        five so the movement keys never move, and the drawer derived its own
        columns from a key width. At 390px that gave 5 x 71px above 4 x 86px
        -- two grids sharing one panel with none of their seams aligned, which
        is what stopped it reading as a single machined face.

        The navigation row has ten keys and must divide evenly, so the only
        answers are two rows of five or one row of ten; everything else
        follows whichever it picks. Ten only where ten fit: at 390px they
        would be 33px wide, a quarter under the minimum in the direction you
        aim.
        """
        for width, expected in ((390, 5), (320, 5), (619, 5), (620, 10),
                                (768, 10), (844, 10), (1400, 10), (0, 5)):
            with self.subTest(width=width):
                self.assertEqual(
                    self.run_js(f'console.log(JSON.stringify(perRow({width})))'),
                    expected)

    def test_the_row_and_the_sheet_never_disagree_about_the_pitch(self):
        """The failure is visual and silent: nothing errors, the seams just
        stop lining up. So the three grids are read back and compared to each
        other rather than each being trusted separately."""
        for width, columns in ((320, 5), (390, 5), (768, 10), (844, 10)):
            with self.subTest(width=width):
                self.assertEqual(self.run_js(f'''
                    nav = new El('div'); navColumns = 0;
                    keys._width = {width};
                    current = []; expanded = true;
                    renderKeys();
                    var wide = function (node) {{
                      var row = rows(node, [])[0];
                      return row ? row.children.length : 0; }};
                    console.log(JSON.stringify(
                      [wide(nav), wide(keys), wide(sheetKeys), columnsUsed]));
                    '''), [columns] * 4)

    # ── the sheet ─────────────────────────────────────────────────────────

    def test_opening_the_drawer_does_not_resize_the_terminal(self):
        """The one that cost the most. renderKeys ended in refit(), so opening
        the drawer took the terminal from 57 rows to 29 -- which resizes the
        pty, which makes tmux reflow and repaint the whole screen, and again
        on close. That is the same jolt that made swiping feel unstable,
        arriving on a tap instead of a drag.

        Measured after, in a browser at 390x844 and at 844x390: the row count
        is identical open and closed. Here the proxy is the pad's own height,
        which is what refit() is decided from.
        """
        self.assertEqual(self.run_js('''
            var fits = 0;
            refit = function () { fits++; };
            keys._width = 390; current = []; expanded = false;
            renderKeys();
            var settled = fits;
            expanded = true; renderKeys();
            var afterOpen = fits;
            expanded = false; renderKeys();
            console.log(JSON.stringify(
              [afterOpen - settled, fits - afterOpen, !!sheet._cls.open]));'''),
            [0, 0, False])

    def test_the_pad_growing_a_row_does_resize_it(self):
        """The other direction, and the reason this is measured rather than
        assumed: the pinned row appearing genuinely does take rows from the
        terminal, and skipping refit() there leaves the app drawing rows that
        are now behind the keys."""
        self.assertEqual(self.run_js('''
            var fits = 0;
            refit = function () { fits++; };
            store[PINS] = '[]'; pins = loadPins();
            keys._width = 390; current = []; expanded = false;
            renderKeys();
            var settled = fits;
            var before = pad.getBoundingClientRect().height;
            pins = ['detach'];          // as if a key had just been kept
            renderKeys();
            console.log(JSON.stringify(
              [fits - settled,
               pad.getBoundingClientRect().height - before]));'''), [1, 44])

    def test_a_rotation_that_changes_the_rows_resizes_it(self):
        """Ten keys on one row instead of two is a row of the pad given back,
        and the terminal has to be told or it keeps drawing rows that are now
        behind the keys."""
        self.assertEqual(self.run_js('''
            var fits = 0;
            refit = function () { fits++; };
            store[PINS] = '[]'; pins = loadPins();
            keys._width = 390; current = []; expanded = false;
            renderKeys();
            var narrow = pad.getBoundingClientRect().height;
            var settled = fits;
            keys._width = 844; renderKeys();
            console.log(JSON.stringify(
              [fits - settled, narrow - pad.getBoundingClientRect().height]));
            '''), [1, 44])

    def test_a_drag_cannot_shrink_it_past_being_useful_or_grow_it_past_the_room(
            self):
        self.assertEqual(self.run_js('''
            expanded = true; host._height = 600;
            sizeSheet(-500); var floor = sheetHeight;
            sizeSheet(99999); var ceiling = sheetHeight;
            console.log(JSON.stringify(
              [floor, ceiling, sheetFloor(), sheetRoom()]));'''),
            [110, 504, 110, 504])

    # ── how much room the drawer takes ────────────────────────────────────

    SETUP = ("nav._height = 105; pinRow._height = 77; keys._height = 73;"
             "host._height = 514; term.rows = 46;")

    def sizing(self, script, cursor=44):
        return self.run_js(
            self.SETUP
            + f'term.buffer.active.cursorY = {cursor};'
            + 'expanded = true; sheetHeight = 0;'
            + script)

    def test_the_drawer_opens_over_the_keys_it_is_replacing(self):
        """The pad's own key rows -- movement, kept, this screen's -- are
        255px of a 390x844 phone, and every key in them is also in the list.
        So while the drawer was open they showed the reader something the
        drawer was showing them again, and the drawer took 252px off the
        terminal to do it. It opens over them now, and the default costs the
        terminal nothing.

        Verified with real touch events: tapping the handle opens a 255px
        drawer with the terminal's transform still 'none', 46 rows before and
        after, and tmux's status line three pixels above it.
        """
        self.assertEqual(self.sizing(
            'sizeSheet();'
            'console.log(JSON.stringify('
            '  [padCover(), sheetHeight, host.style.transform]));'),
            [255, 255, ''])

    def test_the_dead_end_of_the_terminal_belongs_to_the_drawer_too(self):
        """The one the phone actually showed. With a session also open on a
        laptop, tmux sizes the window to the smaller client, fills the rest of
        this one with its own dotted filler and pins its status line to the
        very last row. Measured on a phone: a screenful of filler, and a
        drawer opening to three rows above it.

        Deciding what to keep visible by "the last row with anything in it" is
        what caused that -- that row is the status line, and holding it out
        from under the drawer keeps the whole band of filler on display. The
        cursor is where you are; everything under it is blank, filler or a
        status line, and all three are worth less than more keys.

        Measured after, with tmux pinned to 18 rows in a 46-row terminal: the
        drawer opens to 671px showing all fifty keys, and the terminal still
        does not move.
        """
        free, height, shift = self.sizing(
            'sizeSheet();'
            'console.log(JSON.stringify('
            '  [Math.round(freeSpace()), sheetHeight, host.style.transform]));',
            cursor=8)
        self.assertGreater(free, 600, 'the dead rows were not counted')
        self.assertEqual(height, free)
        self.assertEqual(shift, '', 'the terminal moved for space it was '
                                    'not using')

    def test_what_will_not_fit_there_comes_out_of_the_terminal(self):
        """Past what was free, the rest is borrowed from the top -- by sliding
        the terminal, never by resizing it, because resizing is what makes
        tmux reflow and repaint everything.

        The arithmetic needs a real layout, so the numbers come from a browser
        rather than from here: at 390x844 the drawer at 255px leaves the
        transform empty and at 455px sets it to -200, which is exactly the
        part that would not fit over the pad. What this pins is the wiring --
        that the shift is asked for rather than assumed, and asked in terms of
        the row being written on.
        """
        self.assertIn('shiftTerminal(neededShift())',
                      _extract(self.js, 'sizeSheet'))
        self.assertIn('keepVisibleRow()', _extract(self.js, 'neededShift'))
        self.assertIn('keepVisibleRow()', _extract(self.js, 'freeSpace'))
        # And nothing may reach for the pty to make room.
        self.assertNotIn('refit', _extract(self.js, 'sizeSheet'))

    def test_output_arriving_is_rechecked_rather_than_ignored(self):
        """The rows the drawer sits over are not permanently blank: a bare
        shell starts with one prompt and forty-five empty rows and fills them,
        and the line being written would go under the drawer.

        On a cadence rather than per write, because a build log writes
        hundreds of times a second and the answer only changes when the cursor
        moves."""
        self.assertIn('reshiftSoon()', _extract(self.js, 'feed'))
        recheck = _extract(self.js, 'reshiftSoon')
        self.assertIn('if (!expanded || reshiftTimer) return;', recheck)
        self.assertIn('neededShift()', recheck)

    def test_the_row_it_keeps_out_is_the_one_being_written_on(self):
        """Not the last row with anything in it. That row is tmux's status
        line, and holding it above the drawer keeps a whole band of filler on
        display -- which is what a phone showed."""
        for cursor, expected in ((44, 45), (8, 9), (45, 45)):
            with self.subTest(cursorY=cursor):
                self.assertEqual(self.run_js(
                    'term.rows = 46;'
                    f'term.buffer.active.cursorY = {cursor};'
                    'console.log(JSON.stringify(keepVisibleRow()));'),
                    expected)

    def test_opening_it_never_uses_a_height_kept_from_last_time(self):
        """A remembered pixel height is a measurement of whatever the layout
        was the day it was dragged. It survived a change of orientation, of
        font, of how many keys the far end publishes, and of which device it
        was -- and the symptom was a drawer opening at two rows over a screen
        with room for ten.

        There is nothing left to go stale: opening computes the size from what
        is on screen now. A height dragged smaller is still honoured, it just
        cannot outlive what the drawer is being asked to hold.
        """
        self.assertNotIn('atmux.sheet', self.js)
        self.assertNotIn('rememberSheet', self.js)
        opened, dragged, reopened = self.sizing(
            'sizeSheet(); var opened = sheetHeight;'
            'sizeSheet(150); var dragged = sheetHeight;'
            'sizeSheet(); '
            'console.log(JSON.stringify([opened, dragged, sheetHeight]));')
        self.assertEqual(opened, 255)
        self.assertEqual(dragged, 150)
        self.assertEqual(reopened, 150, 'a smaller drag was not honoured')

    def test_it_never_opens_taller_than_there_is_list_to_show(self):
        """The tablet. A wide screen fits ten keys to a row, so the same sixty
        keys are six rows there and ten on a phone -- and opening to all the
        free space made an iPad show 431px of keys in a 990px drawer covering
        84% of the screen, two thirds of it empty.

        Measured after, at 820x1180: 431px, 37% of the screen, all forty-two
        keys on show, and the terminal still does not move. The phone is
        unchanged.

        300 of keys, 14 of the sheet's own padding, and 28 for the build line
        under them -- everything the drawer holds, and nothing it does not.
        """
        self.assertEqual(self.run_js(
            self.SETUP
            + 'term.buffer.active.cursorY = 4;'      # lots of free space
            + 'sheetKeys._height = 300;'             # a short list, as on a tablet
            + 'expanded = true; sheetHeight = 0; sizeSheet();'
            + 'console.log(JSON.stringify('
            + '  [sheetContent(), Math.round(freeSpace()), sheetHeight]));'),
            [342, 702, 342])

    def test_an_unmeasured_list_is_not_a_short_one(self):
        """Zero rows means the keys have not been laid out yet, which is not
        the same as a drawer with nothing to show -- and capping the drawer at
        an unmeasured content height opens it at nothing.

        The caller used to tell the two apart by the number being small, which
        held only while nothing else sat under the keys. Adding one 28px line
        was enough to carry an empty drawer past the threshold and open it two
        rows tall on a screen with room for ten -- the exact symptom, from a
        new cause, that this whole section exists to have fixed.
        """
        self.assertEqual(self.sizing(
            'sheetKeys._height = 0;'                 # nothing rendered yet
            'sizeSheet();'
            'console.log(JSON.stringify([sheetContent(), sheetHeight]));'),
            [0, 255])

    def test_selecting_text_is_a_mode_and_undoes_exactly_what_blocks_it(self):
        """Two rules stop a finger selecting anything, and both are
        load-bearing the rest of the time: xterm's own stylesheet sets
        user-select: none on the terminal, and #term sets touch-action: none,
        which is what stops a drag being handed to the browser -- and a long
        press is a gesture the browser has to be allowed to recognise before
        it can offer Copy.

        A drag can mean read the scrollback or it can mean select this text.
        It cannot mean both, so which one is a mode rather than something
        inferred from how long a finger rested: guessing costs a page of
        scrollback every time it guesses wrong, and there is no way to tell
        the reader what it guessed.

        This does not try to make xterm's rows selectable. That was the first
        attempt: lift the two blocking rules and stand the gesture handlers
        down. Measured afterwards on this page, user-select really did go
        none -> text the whole way down to the span and touch-action none ->
        auto -- and a real long press still selected nothing, because xterm
        also parks a focused offscreen textarea in front of the gesture, and
        because touch selection over xterm does not work on iOS with any
        renderer at all (xtermjs/xterm.js#3727, open). A stack like that is
        not worth fighting one layer at a time.

        So `copy` lays the same text out as an ordinary <pre> over the
        terminal. Nothing about it is clever, which is the point: a <pre> is
        the construct every browser has known how to long-press, select and
        copy since long before this one existed.
        """
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        block = re.search(r'#seltext \{(.*?)\}', css, re.S)
        self.assertIsNotNone(block, 'there is no selectable layer')
        body = block.group(1)
        for rule in ('user-select: text', '-webkit-user-select: text',
                     '-webkit-touch-callout: default', 'touch-action: auto'):
            with self.subTest(rule=rule):
                self.assertIn(rule, body)
        # The columns are per line now, so the line is what holds them.
        self.assertRegex(css, r'\.sline \{[^}]*white-space: pre')

    def test_the_selectable_layer_holds_more_than_the_screen_does(self):
        """buffer.active starts at the oldest retained line, not at the top of
        the screen -- so what you can select from includes the scrollback,
        which is the part the terminal itself could never offer a finger."""
        source = _extract(self.js, 'bufferLines')
        self.assertIn('term.buffer.active', source)
        self.assertIn('translateToString', source)
        # From 0, not from baseY: starting at the viewport would leave the
        # scrollback out and this would only ever be the visible screen.
        self.assertIn('i = 0', source.replace(' ', ' '))
        self.assertNotIn('baseY', source)

    def test_press_and_hold_copies_the_line_without_any_button(self):
        """What iOS does natively, done here because natively it does not
        happen at all: touch selection over xterm does not work on iOS with
        any renderer (xtermjs/xterm.js#3727, open since 2022), and no engine
        on this machine reproduces the iOS long press either -- headless
        Chrome and Playwright's WebKit both decline to select a bare <pre>
        under one. A gesture resting on their recogniser could be neither
        made to work nor shown to work, which is how two fixes shipped that
        measured perfectly here and failed on the phone.

        This gesture is our own timer instead: finger down, 500ms, no
        movement. Measurable here, and the same code on the phone.

        Measured, with real touch events: a 120ms tap copies nothing and
        marks nothing; a 700ms hold puts that line on the clipboard and says
        so; a plain flick copies nothing; and a hold on a blank row does
        nothing at all rather than appearing to work.
        """
        self.assertIn('LONG_PRESS_MS', self.js)
        start = self.js[self.js.index("host.addEventListener('touchstart'"):]
        start = start[:start.index("host.addEventListener('touchmove'")]
        self.assertIn('pressTimer = setTimeout', start)
        # A press that never moves never reaches touchmove, so the position
        # has to be taken at the start or cellAt reads wherever the last
        # gesture happened to end.
        self.assertIn('lastX = event.touches[0].clientX', start)

    def test_a_press_hands_over_to_the_text_rather_than_copying_a_line(self):
        """Copying a whole line is coarse: it cannot give you half a path or
        two words out of a command. A line was what the gesture could reach,
        not what anyone wanted -- the terminal itself cannot be selected on
        iOS, so a line was the largest unit that needed no selection at all.

        The text can be selected. So a press lands in the same text, at the
        same size, in the same place, with the pressed line already selected,
        and from there it is the system's own selection: its handles, and the
        menu it offers over one. Which is also what iOS does natively --
        press, adjust, copy. It was never one gesture there either.

        Measured at 390x844 against a 39-row screen: pressing the first,
        middle and last row on screen each put that line back at exactly the
        offset it was pressed at -- 0px of drift in all three -- at the same
        row height as the terminal and on the terminal's own background. And
        the thing a line could never do: selecting '/to/file' out of the
        middle of a path and copying it puts '/to/file' on the clipboard.
        """
        start = self.js[self.js.index("host.addEventListener('touchstart'"):]
        start = start[:start.index("host.addEventListener('touchmove'")]
        self.assertIn('setSelecting(true, {', start)
        self.assertNotIn('copyLine(span.text)', start,
                         'still copying a line instead of handing over')
        reveal = _extract(self.js, 'revealLine')
        self.assertIn('scrollTop', reveal)
        self.assertIn('selectNodeContents', reveal)
        self.assertIn('addRange', reveal)

    def test_pressing_a_line_you_scrolled_back_to_find_keeps_it(self):
        """The workflow the gesture exists for: scroll up, find the thing,
        press it.

        setSelecting used to call leaveHistory() before reading the screen --
        from when selecting was a mode reached from a button, where the worry
        was being left somewhere that cannot be typed into. Under a press it
        is backwards: having scrolled up IS the reason you are pressing, and
        going back to live first takes the snapshot of a screen the reader
        has already scrolled away from.

        Measured: scrolled back from line-3961 to line-3920, pressed
        line-3924, and the view opens on the scrolled-back screen -- first
        line 3920 -- with 3924 selected.
        """
        source = _extract(self.js, 'setSelecting')
        # Comments stripped first. The reason this changed is written in one,
        # and matching it would pass whatever the code went on to do -- the
        # same mistake as reading CSS by selector instead of by declaration.
        code = re.sub(r'//[^\n]*', '', source)
        self.assertNotIn('leaveHistory', code,
                         'it still jumps to live before reading the screen')
        # And read before anything else can repaint it.
        self.assertLess(code.index('bufferLines()'),
                        code.index('renderPickable'))

    def test_an_empty_screen_does_not_open_an_empty_view(self):
        """A view with nothing in it is worse than no view: there is nothing
        to select and no sign of why."""
        source = _extract(self.js, 'setSelecting')
        self.assertIn('!lines.length', source)
        self.assertIn('nothing on screen to copy', source)

    def test_nothing_is_left_overlaying_the_rows_it_just_aligned(self):
        """#hist and the build notice sit at z-index 15 over a view at 4, so
        they cover its first rows -- the very rows a press has just been
        careful to put back where they were. They are about the terminal, and
        while this view is up the terminal is not what is being read."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        self.assertIn('body.selecting #overlays { display: none; }', css)

    def test_the_gesture_left_nothing_behind_it_no_longer_uses(self):
        """The line-flash marked which line was about to be copied, and
        nothing is copied on the press any more -- the selection is the
        feedback now. Dead CSS and a dead element are the same trap as a dead
        clipboard path: they rot, and they read as features."""
        for name in ('lineflash', 'flashRows', 'unflash'):
            with self.subTest(gone=name):
                self.assertNotIn(name, self.js)
                self.assertNotIn(name, self.html)

    def test_the_selectable_layer_stands_where_the_terminal_stood(self):
        """Arriving in it has to read as "the text went selectable", not as a
        screen that opened over what you were reading. Same background, same
        font size, and rows the height of the terminal's own."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        view = re.search(r'#selview \{(.*?)\}', css, re.S).group(1)
        term = re.search(r'#term \{(.*?)\}', css, re.S).group(1)
        found = re.search(r'background: (#[0-9a-fA-F]{3,6})', term).group(1)
        self.assertIn('background: ' + found, view,
                      'the layer is a different colour from the terminal')
        # Rows are sized from the same measurement the terminal is drawn
        # with, or a selection lands off what it looks like it is landing on.
        self.assertIn('lineHeight()', _extract(self.js, 'renderPickable'))
        # And nothing above the text, or a line near the top of the screen
        # cannot be put back where it was pressed. Measured: 6px off.
        seltext = re.search(r'#seltext \{(.*?)\}', css, re.S).group(1)
        self.assertRegex(seltext, r'padding: 0;')
        page = self.html
        self.assertLess(page.index('id="seltext"'), page.index('id="selbar"'),
                        'the bar is above the text again')

    def test_the_release_is_not_waited_for_because_it_may_not_come(self):
        """The bug that made the whole gesture do nothing on the phone.

        It hung its work on touchend -- and this file already says, in the
        comment directly above that handler, that iOS fires touchcancel
        instead when the system takes a gesture away. A finger held still for
        half a second over text is exactly when iOS starts its own callout
        and magnifier, so touchend was the one event not guaranteed to
        arrive, and `name === 'touchend'` excluded precisely the case that
        does.

        Nothing waits for the release now: the handover happens in the timer,
        and the release handler only tidies up -- for touchcancel too.
        """
        end = self.js[self.js.index("['touchend', 'touchcancel']"):]
        end = end[:end.index('function spread')]
        self.assertNotIn("name === 'touchend'", end,
                         'the cancel case is excluded again')
        start = self.js[self.js.index("host.addEventListener('touchstart'"):]
        start = start[:start.index("host.addEventListener('touchmove'")]
        self.assertIn('setSelecting(true, {', start)

    def test_there_is_one_way_to_reach_the_clipboard_and_it_is_used(self):
        """A second clipboard path that nothing calls is a trap: it rots,
        and the next person to need one finds two and picks wrong. The line
        copier went with the gesture that used it."""
        self.assertNotIn('copyLine', self.js)
        self.assertIn('legacyCopy', _extract(self.js, 'copyPicked'))

    def test_ios_is_told_to_keep_its_own_long_press_off_the_terminal(self):
        """Starting the system callout is how the web touch gets taken away,
        and the hold is ours to interpret. Lifted again for the copy view,
        where the native gesture is the entire point."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        term = re.search(r'#term \{(.*?)\}', css, re.S).group(1)
        self.assertIn('-webkit-touch-callout: none', term)
        self.assertIn('user-select: none', term)
        lifted = re.search(r'body\.selecting #term \{(.*?)\}', css, re.S)
        self.assertIsNotNone(lifted)
        for rule in ('touch-action: auto', '-webkit-touch-callout: default',
                     'user-select: text'):
            with self.subTest(rule=rule):
                self.assertIn(rule, lifted.group(1))

    def test_the_deprecated_fallback_is_the_one_ios_actually_honours(self):
        """readonly + select() is the recipe everyone writes, and on iOS it
        selects nothing -- iOS refuses a caret in a readonly field -- so it
        copies nothing, silently, while returning true. contentEditable is
        what makes the field selectable, a Range is what selects it, and
        setSelectionRange is what iOS honours.

        Not verifiable here either way: execCommand('copy') returns true and
        reaches no clipboard in headless Chrome or in Playwright's WebKit,
        both of which lack a system clipboard. So this pins the recipe rather
        than the result.
        """
        source = _extract(self.js, 'legacyCopy')
        self.assertIn('contentEditable', source)
        self.assertIn('createRange', source)
        self.assertIn('setSelectionRange', source)
        self.assertNotIn("setAttribute('readonly'", source)
        # 16px, or iOS zooms the page to meet a focused field -- a visible
        # lurch on a terminal, for a field nobody can see.
        self.assertIn("fontSize = '16px'", source)
        # And it is removed however it exits, or a failed copy leaves an
        # invisible editable parked over the page.
        self.assertIn('finally', source)

    def test_a_hold_that_becomes_a_drag_is_a_drag(self):
        """Otherwise a slow scroll copies a line out from under itself. The
        travel is measured from where the finger started, not from the last
        frame: a slow drag never moves far in one frame and would hold its
        way into a copy."""
        move = self.js[self.js.index("host.addEventListener('touchmove'"):]
        move = move[:move.index("['touchend', 'touchcancel']")]
        self.assertIn('cancelPress()', move)
        self.assertIn('LONG_PRESS_SLOP', move)
        self.assertIn('pressed.from', move)

    def test_a_wrapped_line_copies_whole_or_not_at_all(self):
        """A path long enough to be worth copying is long enough to wrap, and
        half of one is worse than none because it looks like it worked.

        Worth knowing what this cannot do: under tmux the wrap has already
        happened at the far end -- tmux repositions the cursor and writes
        each row itself, so xterm never sees a wrap and isWrapped is false.
        Measured: an 80-column path across two rows came back as the row that
        was touched. The walk is still right, and free, wherever xterm did
        the wrapping.
        """
        source = _extract(self.js, 'lineSpan')
        self.assertIn('isWrapped', source)
        # Joined, not newline-separated: the pieces are one line.
        self.assertIn("out.join('')", source)

    def test_the_gesture_is_mentioned_once_and_only_once(self):
        """There is nothing on a terminal to hint at a gesture, and a tip
        repeated on every attach is noise from the second time onwards."""
        source = _extract(self.js, 'offerPressHint')
        self.assertIn('localStorage', source)
        self.assertIn('setItem', source)
        self.assertIn('press and hold', source)

    def test_copying_does_not_need_a_gesture_to_be_recognised(self):
        """Long press is what everyone reaches for, and twice it has not
        worked on the device that matters while working in every measurement
        available here -- and a harness that cannot reproduce a failure
        cannot confirm a fix either. So it is no longer the only way in.

        A tap picks a line; `copy` writes the picked lines with the clipboard
        API. Both are ordinary clicks with no gesture recognition anywhere in
        the path. Measured with real touch taps: one tap reads '1 line', two
        read '2 lines', and the clipboard afterwards holds both lines joined
        by a newline.
        """
        page = self.html
        self.assertIn('id="selcopy"', page)
        source = _extract(self.js, 'copyPicked')
        self.assertIn('navigator.clipboard', source)
        self.assertIn('writeText', source)
        # A tailnet address served over http, or an older iOS, has no
        # clipboard API at all -- and then the deprecated call is the only
        # thing left that works.
        self.assertIn('legacyCopy', source)
        self.assertIn('execCommand', _extract(self.js, 'legacyCopy'))

    def test_a_drag_is_a_selection_and_a_tap_is_a_line(self):
        """Dragging across these lines is how the native selection is made,
        so toggling a line at the end of one would fight the very gesture
        this exists to back up. Measured: a drag the width of a line leaves
        nothing picked; a tap on the same line picks it, and again unpicks."""
        source = self.js[self.js.index('selText.addEventListener(\'pointerup\''):]
        source = source[:source.index('document.addEventListener')]
        self.assertIn('> 8', source, 'no slop, so a drag counts as a tap')
        self.assertIn("classList.toggle('picked')", source)

    def test_a_dragged_selection_outranks_the_tapped_lines(self):
        """Someone who went to the trouble of dragging out half a line means
        that half line, not the whole one underneath it."""
        source = _extract(self.js, 'pickedText')
        # `.sline.picked`, not `picked` -- the function is called pickedText,
        # so the loose substring matches its own name and the assertion holds
        # no matter which order the body is in.
        self.assertLess(source.index('getSelection'),
                        source.index('.sline.picked'),
                        'the picked lines are consulted first')

    def test_the_one_action_with_an_invisible_result_confirms_itself(self):
        """Clearing the picks after a copy drops the selection, which fires
        selectionchange, which repaints the bar -- so 'copied' was written
        and overwritten inside one tick, and the only action whose whole
        result is off-screen was the one with nothing to show for it."""
        self.assertIn('copiedUntil', _extract(self.js, 'paintPicked'))
        source = _extract(self.js, 'copyPicked')
        self.assertIn('copiedUntil', source)
        self.assertIn('copied', source)

    def test_nothing_else_holds_the_caret_while_selecting(self):
        """xterm parks a 6x11 textarea at opacity 0 behind the screen and
        keeps it focused so the software keyboard has somewhere to type. A
        focused editable is what a long press reaches for before it reaches
        for the text under the finger, and it stayed focused through the
        whole of the first attempt -- measured, in both modes."""
        toggle = _extract(self.js, 'setSelecting')
        self.assertIn('blur()', toggle)
        self.assertIn('textarea', toggle)

    def test_the_instructions_are_not_part_of_what_you_are_selecting(self):
        """Measured: a drag aimed at a line of output came back with 'press
        and hold to select', because the bar above it was ordinary selectable
        text sitting in the same layer."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        bar = re.search(r'#selbar \{(.*?)\}', css, re.S).group(1)
        self.assertIn('user-select: none', bar)
        self.assertIn('-webkit-user-select: none', bar)

    def test_paste_is_offered_because_nothing_can_be_pasted_into(self):
        """The only editable on this page is xterm's offscreen helper, one
        cell wide and behind the screen. There is nowhere for a long press to
        paste, so the clipboard has to be read directly -- and that is the
        one clipboard call that needs a real gesture behind it, which is why
        it hangs off a button rather than happening on open."""
        source = _extract(self.js, 'pasteFromClipboard')
        self.assertIn('navigator.clipboard', source)
        self.assertIn('readText', source)
        self.assertIn('sendText', source)
        # A browser that refuses must say so rather than look broken.
        self.assertIn('catch', source)
        page = self.html
        self.assertIn('id="selpaste"', page)

    def test_the_controls_still_fit_the_narrowest_phone(self):
        """Seven 44px targets and six 5px gaps in 10px of padding is 348, and
        no arrangement makes that fit 320 -- so on a 320 phone `hide`, which
        is the way out of the pad, sat 28px past the right edge with nothing
        able to reach it. It predates the layer this commit is about.

        Measured after: 430/390/375/360 one row and never over; 348 exactly
        348 of 348; 347 and 320 on two rows with nothing off the edge.
        """
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        row = re.search(r'#tabs \{(.*?)\}', css, re.S).group(1)
        # Not an unconditional wrap: wrapping is decided before shrinking, so
        # a plain `flex-wrap: wrap` puts 375 on two rows for four pixels.
        self.assertNotIn('flex-wrap: wrap', row)
        guard = re.search(r'@media \(max-width: (\d+)px\) \{\s*'
                          r'#tabs \{ flex-wrap: wrap; \}', css)
        self.assertIsNotNone(guard, 'nothing rescues the narrowest phone')
        self.assertEqual(int(guard.group(1)), 347)

    def test_leaving_selection_mode_puts_the_terminal_back(self):
        """A selection left behind is a selection the next tap extends."""
        toggle = _extract(self.js, 'setSelecting')
        self.assertIn('removeAllRanges', toggle)
        self.assertIn('term.focus()', toggle)
        # Reading and selecting are both "not live"; being in one while
        # entering the other leaves a selection of a screen that cannot be
        # typed into.
        self.assertIn('leaveHistory()', toggle)

    def test_the_handle_is_big_enough_to_hit(self):
        """It was 26px tall -- under the minimum in the direction you aim at
        it -- and its neighbour above is a row of keys, so missing low costs
        nothing and missing high types something. The padding is uneven for
        that reason: the grip sits low in its target."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        rule = [r for r in re.findall(r'body\.touch #grab \{[^}]*\}', css)
                if 'height' in r]
        self.assertEqual(len(rule), 1)
        self.assertIn('height: 44px', rule[0])
        padding = re.search(r'padding: (\d+)px 0 (\d+)px', rule[0])
        self.assertIsNotNone(padding, 'the grip is not biased low')
        self.assertGreater(int(padding.group(1)), int(padding.group(2)))

    def test_a_closed_drawer_has_no_height_at_all(self):
        """Not a zero-height open one: it is out of the layout entirely, which
        is what keeps the terminal's rows the same."""
        self.assertEqual(self.run_js('''
            expanded = true; host._height = 600; sizeSheet();
            expanded = false; sizeSheet();
            console.log(JSON.stringify(
              [sheet.style.height, !!sheet._cls.open]));'''), ['', False])

    def test_the_fade_comes_off_at_the_end_of_the_list(self):
        """It says "there is more below". Once there is not, it would be
        saying something false -- and a hard cut against the settings row is
        what it replaced, which read as damage rather than as more."""
        self.assertEqual(self.run_js('''
            sheet.scrollHeight = 900; sheet.clientHeight = 300;
            sheet.scrollTop = 0; markSheetEnd();
            var atTop = !!sheet._cls.atbottom;
            sheet.scrollTop = 600; markSheetEnd();
            var atEnd = !!sheet._cls.atbottom;
            sheet.scrollHeight = 250; sheet.scrollTop = 0; markSheetEnd();
            console.log(JSON.stringify(
              [atTop, atEnd, !!sheet._cls.atbottom]));'''),
            [False, True, True])

    def test_a_wider_screen_spends_fewer_rows_on_the_same_keys(self):
        """Which is the point: the drawer is height taken from the terminal,
        and on a landscape phone there is very little height to take."""
        narrow = self.render(expanded=True, width=390)
        wide = self.render(expanded=True, width=844)
        self.assertEqual(sorted(narrow['keys']), sorted(wide['keys']))
        self.assertLess(wide['rows'], narrow['rows'])

    # ── tap versus scroll ─────────────────────────────────────────────────

    def gesture(self, script):
        """An open sheet with more in it than fits, and a finger doing
        something to it. The sizes matter: scrollTop clamps to what there is
        to scroll, so a box that reports nothing scrollable cannot be
        scrolled -- which is exactly what the drawer is not."""
        return self.run_js(f'''
            keys._width = 390; current = []; expanded = true;
            renderKeys();
            sheet.scrollHeight = 900; sheet.clientHeight = 253;
            sent = [];
            {script}''')

    def test_a_tap_sends_the_key_under_the_finger(self):
        self.assertEqual(self.gesture('''
            var key = keyAt(sheetKeys, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 100});
            key.emit('pointerup', {clientX: 50, clientY: 100});
            console.log(JSON.stringify([sent, sheet.scrollTop]));'''),
            [['\x02d'], 0])

    def test_a_small_wobble_is_still_a_tap(self):
        """A thumb is not a stylus. Treating every pixel of movement as a
        scroll would make the pad feel broken rather than careful."""
        self.assertEqual(self.gesture('''
            var key = keyAt(sheetKeys, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 100});
            key.emit('pointermove', {clientX: 53, clientY: 104});
            key.emit('pointerup', {clientX: 53, clientY: 104});
            console.log(JSON.stringify([sent, sheet.scrollTop]));'''),
            [['\x02d'], 0])

    def test_a_cancelled_pointer_types_nothing(self):
        self.assertEqual(self.gesture('''
            var key = keyAt(sheetKeys, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 100});
            key.emit('pointercancel', {});
            console.log(JSON.stringify(sent));'''), [])

    def test_the_fixed_row_does_not_scroll_the_drawer(self):
        """It is fixed on purpose, and dragging a key that stays put to move
        something that does not is a gesture nobody asked for.

        The key does type, once, on the way down -- that is what a fixed row
        is for and it is covered above. What must not happen is the drawer
        moving underneath it, and it must not type again on release.
        """
        self.assertEqual(self.gesture('''
            var key = keyAt(nav, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 200});
            [170, 140].forEach(function (y) {
              key.emit('pointermove', {clientX: 50, clientY: y}); });
            key.emit('pointerup', {clientX: 50, clientY: 140});
            console.log(JSON.stringify([sent, sheet.scrollTop]));'''),
            [['\x1b'], 0])

    # ── who scrolls the drawer ────────────────────────────────────────────

    def test_the_whole_drawer_scrolls_and_not_just_the_keys(self):
        """The hole this leaves when it is wrong is enormous and silent.

        The scroll handler used to hang off each key. With the browser told
        not to pan (touch-action: none) and a handler only on the keys, every
        part of the box that is not a key -- the gaps between them, the row
        margins, the section headings, the toolbar, the padding -- was a place
        where a drag did nothing at all. Measured with real touch events
        through the browser's own gesture pipeline: a finger landing on a key
        scrolled 169px, a finger landing 55px lower scrolled 0. A drawer that
        ignores half the thumbs put on it reads as broken.

        So the listener is on the box that scrolls, and the keys' events reach
        it by bubbling. Verified across a grid of 119 starting points covering
        the whole drawer: all 119 scroll, and none of them types.
        """
        self.assertIn("sheet.addEventListener('pointerdown'", self.js)
        self.assertIn("sheet.addEventListener('pointermove'", self.js)
        self.assertIn("sheet.addEventListener('pointerup'", self.js)
        # And no key may be the thing that scrolls, which is what left holes.
        self.assertNotIn('dragSheet', _extract(self.js, 'buildKey'))
        self.assertNotIn('scrollDrawer', self.js)

    def test_the_drawer_never_hands_a_drag_to_the_browser(self):
        """It did, for one commit: `touch-action: pan-y` on a key, so the
        browser panned and the platform arbitrated tap-versus-scroll. Correct
        reasoning, works in Chromium, wrong call -- once the browser claims
        the gesture it decides on its own whether a pointercancel follows, and
        a key that never hears one types whatever you dragged across. A
        control character into a live shell is not worth a nicer scroll, and
        it is not checkable from here.

        None everywhere in the drawer, so the answer is the same on every
        browser and can be tested on this one.
        """
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        self.assertNotIn('pan-y', css)
        # Selected by what the rule declares, not by where it sits. Matching
        # on the selector alone has now picked the wrong block three times in
        # this file -- there are several rules for #tabs button, for .ghead
        # and for #sheet.open, and the first one found is never the one that
        # sets the property under test.
        declaring = [(sel.strip(), body) for sel, body
                     in re.findall(r'([^{}]*)\{([^}]*touch-action[^}]*)\}', css)
                     if '#sheet' in sel]
        self.assertTrue(declaring, 'the drawer declares no touch-action')
        for selector, body in declaring:
            with self.subTest(selector=selector):
                self.assertIn('touch-action: none', body)

    def test_a_drag_that_began_on_a_control_does_not_also_press_it(self):
        """`keep keys` lives inside the drawer, so a drag that started on it
        scrolled the drawer -- and the click that follows must not also change
        a mode."""
        self.assertIn('if (sheetDragged) return;', self.js)

    def test_a_key_still_refuses_the_focus_the_keyboard_is_attached_to(self):
        """The reason any of this was hand-written. A button allowed its
        default takes focus, and the software keyboard is attached to the
        terminal's textarea -- so the row would close the keyboard exactly
        when you reached for the symbol row.

        Verified in a browser too: after a tap on a drawer key the active
        element is still xterm-helper-textarea, and the drawer still pans."""
        build = _extract(self.js, 'buildKey')
        down = build[build.index('function down('):]
        down = down[:down.index('\n    }')]
        self.assertIn('event.preventDefault();', down)

    def test_a_pan_the_browser_claims_types_nothing(self):
        """Once a pan wins, the browser sends pointercancel and the key must
        treat that as "not a tap" -- which is the platform's own arbitration
        between a tap and a scroll, and better than any threshold measured
        here."""
        self.assertEqual(self.gesture('''
            var key = keyAt(sheetKeys, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 200});
            key.emit('pointermove', {clientX: 50, clientY: 160});
            key.emit('pointercancel', {});
            key.emit('pointerup', {clientX: 50, clientY: 160});
            console.log(JSON.stringify(sent));'''), [])

    def test_a_pointer_that_never_pans_still_cannot_type_by_dragging(self):
        """A mouse or a stylus is never cancelled by a pan, so the distance
        check stays as the backstop for those."""
        self.assertEqual(self.gesture('''
            var key = keyAt(sheetKeys, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 200});
            key.emit('pointermove', {clientX: 50, clientY: 130});
            key.emit('pointerup', {clientX: 50, clientY: 130});
            console.log(JSON.stringify(sent));'''), [])

    def test_each_section_holds_its_own_heading(self):
        """A sticky heading is held by its parent's edges. Headings that are
        all siblings pin to the same spot and stack there -- which happened to
        paint the right one on top, because the current section is the last in
        the document, and would be wrong the moment that stopped being true.

        Measured before boxing them, at four scroll positions: the headings
        pinned were tmux, then tmux+ctrl, then tmux+ctrl+move. Measured after:
        exactly one, and the right one.
        """
        self.assertEqual(self.run_js('''
            keys._width = 390; current = []; expanded = true;
            renderKeys();
            var sections = sheetKeys.children.filter(function (c) {
              return c._cls.gsec; });
            console.log(JSON.stringify(sections.map(function (s) {
              return headings(s, []).length; })));'''),
            [1, 1, 1, 1])

    def test_a_closed_drawer_has_nothing_to_scroll(self):
        self.assertEqual(self.run_js('''
            keys._width = 390; current = []; expanded = false;
            renderKeys(); sent = [];
            var key = keyAt(keys, 0);
            key.emit('pointerdown', {clientX: 50, clientY: 200});
            key.emit('pointermove', {clientX: 50, clientY: 140});
            key.emit('pointerup', {clientX: 50, clientY: 140});
            console.log(JSON.stringify([sent, keys.scrollTop]));'''),
            [[], 0])

    def test_a_held_key_repeats_and_does_not_fire_again_on_release(self):
        """Firing on release is what makes a drag safe; it must not also add
        a keystroke to the end of a hold that has already been repeating."""
        self.assertEqual(self.gesture('''
            var key = keyLabelled(sheetKeys, 'PgUp');
            key.emit('pointerdown', {clientX: 0, clientY: 0});
            setTimeout(function () {
              var before = sent.length;
              key.emit('pointerup', {clientX: 0, clientY: 0});
              console.log(JSON.stringify([
                before >= 2, sent.length === before,
                sent.every(function (s) { return s === '\\x1b[5~'; })]));
            }, 560);'''),
            [True, True, True])

    # ── the keys you keep ─────────────────────────────────────────────────

    def edit(self, script, stored=None):
        """An open drawer in editing mode, with whatever was remembered."""
        keep = '' if stored is None else (
            f'store[PINS] = {json.dumps(json.dumps(stored))}; '
            'pins = loadPins();')
        return self.run_js(f'''
            {keep}
            keys._width = 390;
            current = [{{k: 'x', l: 'Kill session'}}];
            expanded = true; editing = true;
            renderKeys();
            sent = [];
            {script}''')

    def test_what_you_keep_is_remembered(self):
        self.assertEqual(self.edit('''
            var key = keyLabelled(sheetKeys, 'detach');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            console.log(JSON.stringify([pins, store[PINS], sent]));''',
            stored=[]),
            [['detach'], '["detach"]', []])

    def test_keeping_it_twice_puts_it_back(self):
        self.assertEqual(self.edit('''
            var key = keyLabelled(sheetKeys, '^C');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            key = keyLabelled(sheetKeys, '^C');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            console.log(JSON.stringify([pins, store[PINS]]));''',
            stored=['detach']),
            [['detach'], '["detach"]'])

    def test_a_kept_key_gets_a_fixed_row_of_its_own(self):
        """Outside the drawer, because the drawer scrolls and the point of
        keeping a key is that it stops moving."""
        self.assertEqual(self.edit('''
            console.log(JSON.stringify(
              [drawn(pinRow, []), pinRow.style.display]));''',
            stored=['detach', '|']),
            [['detach', '|'], ''])

    def test_no_kept_keys_means_no_row_at_all(self):
        """An empty choice is still a choice: someone who cleared the row gets
        it cleared, not helpfully refilled with the defaults."""
        self.assertEqual(self.edit('''
            console.log(JSON.stringify(
              [drawn(pinRow, []), pinRow.style.display]));''', stored=[]),
            [[], 'none'])

    def test_the_row_exists_on_a_phone_that_has_never_been_here(self):
        """It used to render empty, which made the whole feature's on-screen
        presence an unlabelled star acting on the four keys the closed drawer
        happened to be showing. Nobody discovers a row that is not there."""
        page = self.edit('''
            console.log(JSON.stringify(
              [drawn(pinRow, []), pinRow.style.display]));''')
        self.assertEqual(page[1], '')
        self.assertEqual(page[0], ['^C', '^R', 'PgUp', '/'])

    def test_the_defaults_are_keys_this_client_actually_has(self):
        """A default naming something that is not in any group would render
        as nothing at all -- an empty row with extra steps."""
        self.assertEqual(self.run_js('''
            store[PINS] = null; delete store[PINS];
            pins = loadPins();
            console.log(JSON.stringify(pinnedKeys().map(function (e) {
              return e.l; })));'''), ['^C', '^R', 'PgUp', '/'])

    def test_a_default_pin_does_not_duplicate_the_way_out(self):
        """`detach` is in the settings row now. Three ways to leave, stacked
        one above the other, is not three times as useful."""
        self.assertNotIn('detach', self.run_js(
            'console.log(JSON.stringify(DEFAULT_PINS));'))

    def test_clearing_the_last_kept_key_stays_cleared(self):
        """The store is written even when the list is empty, because the key
        existing is what records that a choice was made."""
        self.assertEqual(self.edit('''
            var key = keyLabelled(sheetKeys, 'detach');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            console.log(JSON.stringify([pins, store[PINS], loadPins()]));''',
            stored=['detach']),
            [[], '[]', []])

    def test_a_kept_key_follows_a_rebound_prefix(self):
        """Which is why labels are stored and bytes are not. `detach` frozen
        as \\x02d would keep sending C-b to somebody running C-a."""
        self.assertEqual(self.run_js('''
            store[PINS] = '["detach"]'; pins = loadPins();
            prefixSeq = '\\x01';
            console.log(JSON.stringify(pinnedKeys()[0].k));'''), '\x01d')

    def test_a_label_that_no_longer_names_anything_just_stops_appearing(self):
        """Rather than becoming a button that types nothing, or an exception
        on the way to drawing the pad."""
        self.assertEqual(self.run_js('''
            store[PINS] = '["detach", "no such key"]'; pins = loadPins();
            console.log(JSON.stringify(pinnedKeys().map(function (e) {
              return e.l; })));'''), ['detach'])

    def test_a_hand_edited_store_cannot_break_the_pad(self):
        for raw in ('not json', '{}', '"detach"', '[1, 2]', '[null]',
                    '["' + 'x' * 40 + '"]'):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_js(
                    f'store[PINS] = {json.dumps(raw)};'
                    'console.log(JSON.stringify(loadPins()));'), [])

    def test_only_so_many_fit(self):
        """The row is fixed and above the drawer, so an unbounded one would
        eat the terminal it is meant to be helping you use."""
        self.assertEqual(self.run_js('''
            store[PINS] = JSON.stringify(
              ['detach','^C','^D','^Z','^L','^R','^A','^E','^W','^K']);
            pins = loadPins();
            var before = pins.length;
            togglePin('^U');
            console.log(JSON.stringify([before, pins.length,
                                        pins.indexOf('^U')]));'''),
            [8, 8, -1])

    # ── nothing fires while you are choosing ──────────────────────────────

    def test_choosing_a_key_does_not_press_it(self):
        self.assertEqual(self.edit('''
            var key = keyLabelled(sheetKeys, '^C');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            console.log(JSON.stringify([sent, pins]));''', stored=[]),
            [[], ['^C']])

    def test_an_app_key_is_inert_while_choosing_rather_than_merely_unkeepable(
            self):
        """The one that matters. The app's own keys share this drawer, they
        cannot be kept, and one of them is `Kill session` -- a tap falling
        through to it because it happened not to be pinnable would be the
        worst bug in this file."""
        self.assertEqual(self.edit('''
            var key = keyLabelled(sheetKeys, 'Kill session');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            console.log(JSON.stringify(
              [sent, pins, key._cls.locked === 1, !!key._cls.choose]));''',
            stored=[]),
            [[], [], True, False])

    def test_a_held_key_does_not_keep_and_unkeep_itself(self):
        """PgUp repeats. Ten pins a second is the obvious way to get this
        wrong, so nothing repeats while choosing."""
        self.assertEqual(self.edit('''
            var key = keyLabelled(sheetKeys, 'PgUp');
            key.emit('pointerdown', {});
            setTimeout(function () {
              key.emit('pointerup', {});
              console.log(JSON.stringify([sent, pins]));
            }, 560);''', stored=[]),
            [[], ['PgUp']])

    # ── the way out ───────────────────────────────────────────────────────

    def test_the_way_out_is_visible_without_opening_anything(self):
        """From inside a session the only route back to the list was
        `detach` -- seventh key into the sheet, under `tmux` -- and this page
        is built to be installed to the home screen, where there is no browser
        chrome to fall back on.

        It sends `prefix d` rather than navigating: detaching ends the pty,
        the socket closes with reason "exit", and onclose is what returns you.
        So the button does not have to know where the list is, and it follows
        a rebound prefix for free.
        """
        self.assertEqual(self.run_js('''
            sent = [];
            prefixSeq = '\\x01';
            typed(prefixSeq + 'd');
            console.log(JSON.stringify(sent));'''), ['\x01d'])
        self.assertIn("typed(prefixSeq + 'd')", self.js)

    def test_leaving_stops_reading_history_first(self):
        """Typing into a pane that is still in copy-mode reaches nothing --
        measured. A detach that silently did nothing because you had swiped
        up first is the worst version of this button."""
        self.assertEqual(self.run_js('''
            scrolledBack = true; sent = [];
            typed(prefixSeq + 'd');
            console.log(JSON.stringify([sent, scrolledBack]));'''),
            [['q', '\x02d'], False])

    def test_pressing_a_kept_key_normally_sends_it(self):
        self.assertEqual(self.run_js('''
            store[PINS] = '["detach"]'; pins = loadPins();
            keys._width = 390; current = []; expanded = false; editing = false;
            renderKeys(); sent = [];
            var key = keyLabelled(pinRow, 'detach');
            key.emit('pointerdown', {}); key.emit('pointerup', {});
            console.log(JSON.stringify(sent));'''), ['\x02d'])

    # ── swiping the terminal ──────────────────────────────────────────────

    def swipe(self, buffer_type, mode, lines=3):
        """One swipe of `lines` rows, with the terminal in a stated state."""
        return self.run_js(f'''
            sent = [];
            scrolledBack = false;
            mouseOn = false; wheelDebt = 0; pageDebt = 0; owed = 0;
            published = {json.dumps(mode)};
            term = {{
              rows: 40,
              scrollLines: function (n) {{ sent.push('scrollLines:' + n); }},
              buffer: {{ active: {{ type: {json.dumps(buffer_type)} }} }}
            }};
            var acted = swipeBy({json.dumps(int(lines))}); payScroll();
            console.log(JSON.stringify([swipeKind(), acted, sent]));''')

    def test_a_plain_shell_scrolls_without_the_program_hearing_anything(self):
        """xterm holds that scrollback -- measured, Shift+PageUp moved the top
        row from 245 to 77 -- so this never reaches the wire at all."""
        self.assertEqual(self.swipe('normal', ''),
                         ['local', True, ['scrollLines:-9']])
        self.assertEqual(self.swipe('normal', '', lines=-3),
                         ['local', True, ['scrollLines:9']])

    def test_tmux_is_asked_the_way_tmux_asks_itself(self):
        """prefix+PageUp is tmux's own default binding (copy-mode -u) and
        needs no `mouse on`. Measured against tmux 3.6a with mouse off and
        400 lines of history: top row 346 -> 292 [54/346] -> 238 [108/346],
        PageDown back, q returns to live."""
        self.assertEqual(self.swipe('alternate', 'external'),
                         ['tmux', True,
                          ['\x02[', '\x1b[1;5A' * 9]])
        # Down only means something once you are up in the history; from
        # live there is nothing below to reach for.
        self.assertEqual(self.run_js("""
            sent = []; scrolledBack = false; published = 'external';
            term = { rows: 40, buffer: { active: { type: 'alternate' } } };
            swipeBy(4); payScroll(); sent = []; swipeBy(-2); payScroll();
            console.log(JSON.stringify(sent));"""),
            ['\x1b[1;5B' * 6])

    def test_the_dashboard_is_not_tmux_and_is_not_sent_a_prefix(self):
        """Textual would make its own sense of \\x02, and none of it is
        'scroll up'."""
        # A screen of travel, because Textual's arrows move the selection
        # that decides what Enter attaches to -- not a viewport.
        self.assertEqual(self.swipe('alternate', 'app', lines=14),
                         ['keys', True, ['\x1b[5~']])
        self.assertEqual(self.swipe('alternate', 'app', lines=3),
                         ['keys', True, []])

    def test_a_program_that_asked_for_the_mouse_is_given_the_mouse(self):
        """Claude Code, vim, htop, less and tmux-with-`mouse on` draw in the
        alternate buffer and scroll themselves. tmux cannot do it for them:
        an alternate-screen program's lines never enter the pane's history,
        so copy-mode would scroll back through whatever was on screen before
        the program started -- the wrong content entirely.

        Measured end to end against a target that turns on 1000+1006 inside
        tmux: the swipe sent ESC[<64;35;33M and the program echoed back
        having received exactly that, while tmux never entered copy-mode.
        """
        self.assertEqual(self.run_js("""
            mouseOn = true;
            term = { rows: 40, buffer: { active: { type: 'alternate' } } };
            var a = swipeKind();
            published = 'external';
            var b = swipeKind();
            term.buffer.active.type = 'normal';
            var c = swipeKind();
            mouseOn = false;
            var d = swipeKind();
            console.log(JSON.stringify([a, b, c, d]));"""),
            ['wheel', 'wheel', 'wheel', 'local'])
        # It outranks both other branches on purpose: they are true at the
        # same time whenever a mouse-driven program runs inside tmux, and the
        # program is the one holding the scrollback being reached for.
        body = _extract(self.js, 'swipeKind')
        self.assertLess(body.index('mouseOn'), body.index('bufferType'))

    def test_the_report_is_spelled_the_way_the_program_asked_for(self):
        """1006 says how, not whether. The original encoding biases three
        bytes by 32 and cannot express a coordinate past 223, so a report
        that would point somewhere else is not sent at all."""
        self.assertEqual(self.run_js("""
            term = { cols: 80, rows: 40 };
            host = { getBoundingClientRect: function () {
              return {left: 0, top: 0, width: 800, height: 400}; } };
            lastX = 345; lastY = 155;              // col 35, row 16
            mouseSgr = true;
            var sgr = [wheelReport(true), wheelReport(false)];
            mouseSgr = false;
            var x10 = wheelReport(true);
            term.cols = 400; lastX = 3990;         // past what 3 bytes can say
            var refused = wheelReport(true);
            console.log(JSON.stringify([sgr, x10, refused]));"""),
            [['\x1b[<64;35;16M', '\x1b[<65;35;16M'],
             '\x1b[M' + chr(32 + 64) + chr(32 + 35) + chr(32 + 16),
             ''])

    def test_a_notch_is_not_a_line(self):
        """Every program turns one wheel notch into however many lines it
        thinks a notch is worth -- tmux's own binding is five. One report per
        line would scroll several times too fast for the same travel."""
        got = self.run_js("""
            sent = []; mouseOn = true; mouseSgr = true; wheelDebt = 0;
            term = { cols: 80, rows: 40, buffer: { active: { type: 'alternate' } } };
            host = { getBoundingClientRect: function () {
              return {left: 0, top: 0, width: 800, height: 400}; } };
            lastX = 0; lastY = 0;
            swipeBy(1); payScroll(); var one = sent.length;
            swipeBy(2); payScroll(); var three = sent.length;
            console.log(JSON.stringify([one, three, NOTCH, GAIN]));""")
        # One row of finger is GAIN lines is exactly one notch, which is what
        # a notch is worth in most programs -- so the two cancel and the
        # content keeps up with the thumb.
        self.assertEqual(got, [1, 3, 3, 3])

    def test_wanting_the_mouse_and_spelling_it_are_read_apart(self):
        """xterm 6 removed term.modes and reading it throws, so DECSET is
        watched directly. 1000/1002/1003 say the program wants events at all;
        1006 only says how it wants them spelled, and a program that sent
        1006 without one of the others is not asking to be scrolled."""
        setup = self.js[self.js.index('var mouseOn'):]
        setup = setup[:setup.index('function bufferType')]
        wants = setup[setup.index('mouseOn = pair[1]') - 220:
                      setup.index('mouseOn = pair[1]')]
        for mode in ('1000', '1002', '1003'):
            with self.subTest(mode=mode):
                self.assertIn(mode, wants)
        self.assertNotIn('1006', wants)
        spells = setup[setup.index('mouseSgr = pair[1]') - 60:
                       setup.index('mouseSgr = pair[1]')]
        self.assertIn('1006', spells)
        # Encodings nobody asks for any more are not tracked at all.
        for ignored in ('1005', '1015'):
            with self.subTest(ignored=ignored):
                self.assertNotIn(ignored, setup)
        # Returning false leaves xterm to act on the sequence as well.
        self.assertIn('return false', setup)

    def test_an_unrecognised_state_sends_nothing_at_all(self):
        """The safety argument for the whole feature. In the alternate buffer
        with mouse reporting off -- tmux's default -- a wheel event becomes
        ARROW KEYS: measured, \\x1bOA / \\x1bOB going out and nothing
        scrolling, which walks the shell's command history one Enter away
        from re-running something. Every case this cannot positively identify
        has to land here instead of guessing."""
        for buffer_type, mode in (('alternate', ''),
                                  ('alternate', 'nonsense'),
                                  ('', 'external'),
                                  ('', ''),
                                  ('something-new', 'external')):
            with self.subTest(buffer=buffer_type, mode=mode):
                self.assertEqual(self.swipe(buffer_type, mode),
                                 ['', False, []])

    def test_the_handover_is_recorded_so_a_swipe_knows_who_is_listening(self):
        """The wire says nothing else about who is drawing, and the dashboard
        and tmux want different keys for the same gesture.

        Measured broken end to end before this line existed: on
        `/console/?attach=NODE:SESSION` the mode was never recorded, so
        swipeKind() saw '' and every swipe on the attach path -- the one a
        phone takes when it taps a session row -- refused. The other half of
        that fix is cli._announce_external, which is what sends this.
        """
        handler = self.js[self.js.index('registerOscHandler'):]
        handler = handler[handler.index('{') + 1:handler.index('return true;\n  });')]
        self.assertEqual(self.run_js(f'''
            var seen = [];
            function renderKeys() {{}}
            function setKeys() {{}}
            function osc(payload) {{ {handler} return true; }}
            ['external', 'app', 'nonsense', ''].forEach(function (mode) {{
              osc(JSON.stringify({{mode: mode, keys: []}}));
              seen.push(published);
            }});
            console.log(JSON.stringify(seen));'''),
            # The last two never happened: an unknown mode leaves the last
            # known one standing rather than resetting to "cannot tell".
            ['external', 'app', 'app', 'app'])

    def test_a_terminal_that_cannot_be_asked_reads_as_unknown(self):
        """A renamed or removed API must fail closed, not throw and not come
        back as a truthy string."""
        self.assertEqual(self.run_js('''
            term = {};
            var a = bufferType();
            term = { buffer: { active: {} } };
            var b = bufferType();
            term = { get buffer() { throw new Error('gone'); } };
            var c = bufferType();
            console.log(JSON.stringify([a, b, c]));'''), ['', '', ''])

    def history(self, body):
        """The bar, with hist wired up and the terminal in tmux."""
        return self.run_js('''
            sent = [];
            mouseOn = false; wheelDebt = 0; pageDebt = 0; owed = 0;
            var shown = [];
            hist = new El('div');
            hist.hidden = true;
            Object.defineProperty(hist, 'hidden', {
              get: function () { return this._h; },
              set: function (v) { this._h = v; shown.push(v ? '' : 'on'); }
            });
            histText = null;
            scrolledBack = false;
            published = 'external';
            term = { rows: 40, write: function () {},
                     buffer: { active: { type: 'alternate' } } };
            ''' + body + '''
            console.log(JSON.stringify(
              [sent, scrolledBack, histText ? histText.nodeValue : null]));''')

    def test_a_swipe_up_says_you_are_reading_history(self):
        """Measured: after `prefix PageUp` the pane reports pane_in_mode=1 and
        stays there through a swipe back to the bottom, across a detach, and
        for every keystroke after -- a typed `echoXYZ` produced nothing at
        all. The screen looks live and is not."""
        sent, back, label = self.history('swipeBy(3); payScroll();')
        self.assertEqual(sent, ['\x02[', '\x1b[1;5A' * 9])
        self.assertTrue(back)
        self.assertIn('lines', label)           # the drag's own count first

    def test_the_bar_is_the_way_back_to_live(self):
        """q is copy-mode's cancel in both key tables."""
        sent, back, label = self.history('''
            swipeBy(3); payScroll(); sent = []; leaveHistory();''')
        self.assertEqual(sent, ['q'])
        self.assertFalse(back)
        self.assertEqual(label, '')

    def test_typing_leaves_history_before_it_types(self):
        """The other door, and the one that matters: the bar only helps
        someone who reads it. Anything typed is a statement that you are done
        reading, so no sequence of taps can strand you in a mode that eats
        your keystrokes."""
        sent, back, _ = self.history('''
            swipeBy(3); payScroll(); sent = []; typed('l'); typed('s');''')
        self.assertEqual(sent, ['q', 'l', 's'])
        self.assertFalse(back)

    def test_leaving_twice_does_not_send_q_twice(self):
        """A stray q reaches the shell as a character."""
        sent, _, _ = self.history('''
            swipeBy(3); payScroll(); sent = [];
            leaveHistory(); leaveHistory(); typed('x');''')
        self.assertEqual(sent, ['q', 'x'])

    def test_a_swipe_down_from_live_reaches_for_nothing(self):
        """There is nothing below the bottom of the scrollback. Entering
        copy-mode to look would put you in a mode you did not ask for, to be
        shown the screen you were already on."""
        sent, back, _ = self.history('swipeBy(-3); payScroll();')
        self.assertEqual(sent, [])
        self.assertFalse(back)

    def test_a_swipe_down_inside_history_scrolls_back_toward_live(self):
        sent, back, _ = self.history('swipeBy(3); payScroll(); sent = []; swipeBy(-2); payScroll();')
        self.assertEqual(sent, ['\x1b[1;5B' * 6])
        self.assertTrue(back)

    def test_nothing_is_drawn_while_a_finger_is_down(self):
        """Safari iOS stops firing touch events for the rest of a gesture as
        soon as the DOM changes structurally under it -- appendChild counts,
        innerHTML does not. xterm's DOM renderer rebuilds rows that way and
        tmux repaints for every page, so the first page a swipe sent ended
        the gesture that sent it. Held bytes arrive in order on release."""
        self.assertEqual(self.run_js('''
            var drawn = [];
            term = { write: function (d) { drawn.push(d); } };
            feed('a');                      // no gesture: straight through
            holdWrites(true);
            feed('b'); feed('c');
            var during = drawn.slice();
            holdWrites(false);
            feed('d');
            console.log(JSON.stringify([during, drawn]));'''),
            [['a'], ['a', 'b', 'c', 'd']])

    def test_a_gesture_that_never_ends_cannot_freeze_the_terminal(self):
        """touchend and touchcancel release the hold; the timer is only a
        backstop. Without it a lost release leaves a terminal that has
        stopped drawing and no way for the user to know why."""
        held = _extract(self.js, 'holdWrites')
        self.assertIn('setTimeout', held)
        self.assertIn('clearTimeout', held)
        self.assertRegex(held, r'setTimeout\([^,]*,\s*(\d{4})\)')

    def test_the_readout_fits_a_phone(self):
        """The first report back from a real phone carried two of the five
        fields; the three that would have told us which fault it was ran off
        the right edge. One line of this is ~60 characters at 11px, which is
        wider than a 390px screen."""
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        rule = re.search(r'#debug \{[^}]*\}', css).group(0)
        self.assertIn('white-space: normal', rule)
        self.assertNotIn('nowrap', rule)

    def test_the_readout_names_the_unit_actually_in_force(self):
        """dy is shown against the distance one line costs, which is what the
        drag is now measured in."""
        self.assertIn('lineHeight()', _extract(self.js, 'showDebug'))

    def test_a_line_is_a_row_of_the_terminal(self):
        """What makes this scrolling rather than paging: the content moves by
        exactly as many rows as the finger crossed."""
        self.assertEqual(self.run_js('''
            term = { rows: 40 };
            host = { getBoundingClientRect: function () { return {height: 800}; } };
            var normal = lineHeight();
            host = { getBoundingClientRect: function () { return {height: 0}; } };
            var degenerate = lineHeight();
            console.log(JSON.stringify([normal, degenerate]));'''), [20, 6])

    def test_the_readout_cannot_kill_the_gesture_it_reports_on(self):
        """textContent replaces an element's children, which is the exact
        structural change that ends the cascade. nodeValue on a node made
        once does not."""
        readout = _extract(self.js, 'showDebug')
        self.assertIn('nodeValue', readout)
        self.assertNotIn('textContent', readout)

    def test_the_readout_exists_only_when_it_is_asked_for(self):
        """A permanent overlay across the top of a terminal costs a row and
        buys nothing once the answer is known."""
        # The element is built inside the guard, not built and then hidden:
        # a permanent node over the terminal is a row someone paid for.
        guard = self.js[self.js.index('var debugBox'):]
        guard = guard[:guard.index('createElement')]
        self.assertIn('/[?&]debug=1/.test(location.search)', guard)
        self.assertEqual(self.run_js('''
            var re = /[?&]debug=1/;
            console.log(JSON.stringify(['?debug=1', '?attach=a:b&debug=1',
                                        '', '?attach=a:b', '?nodebug=1']
              .map(function (q) { return re.test(q); })));'''),
            [True, True, False, False, False])
        # It reports what the branch table reads, not a restatement of it.
        readout = _extract(self.js, 'showDebug')
        for value in ('bufferType()', 'published', 'swipeKind()', 'lineHeight()'):
            with self.subTest(value=value):
                self.assertIn(value, readout)

    def test_the_content_follows_the_finger_row_for_row(self):
        """Paging was chosen because `prefix PageUp` enters copy-mode and
        scrolls in one keystroke, and that convenience is exactly why reading
        back felt like jumping. C-Up is one line -- measured through
        #{scroll_position}: five presses gave 5, three back gave 2, while one
        PageUp jumped 41."""
        body = _extract(self.js, 'emitScroll')
        self.assertIn('\\x1b[1;5A', body)
        self.assertIn('\\x1b[1;5B', body)
        self.assertNotIn('\\x1b[5~\'', body.split("kind === 'keys'")[0])

    # ── getting out of the way ────────────────────────────────────────────

    def test_hiding_the_pad_hides_all_of_it(self):
        """Not a smaller pad and not a row of controls left behind. The pad is
        two hundred pixels of a phone, and there are stretches -- reading a
        log, watching a job -- where you want none of it."""
        self.assertEqual(self.run_js('''
            setPad(false);
            console.log(JSON.stringify(
              [!!document.body._cls.nopad, store[PAD_STATE]]));'''),
            [True, 'hidden'])

    def test_putting_the_pad_away_ends_editing_with_it(self):
        """Coming back to a pad whose keys quietly keep instead of press,
        because of something you did before you hid it, is exactly the trap
        a mode has to avoid."""
        self.assertEqual(self.run_js('''
            keys._width = 390; current = []; editing = false;
            setEditing(true);
            setPad(false);
            console.log(JSON.stringify([editing, expanded]));'''),
            # The sheet closes with it too. It covers the terminal rather than
            # sitting inside the pad, so leaving it open while the pad is
            # hidden would leave sixty keys over a screen with no way back.
            [False, False])

    def test_closing_the_drawer_ends_editing_too(self):
        """A lit star in the corner is not enough to explain why `detach`
        just unkept itself instead of detaching."""
        self.assertEqual(self.run_js('''
            keys._width = 390; current = []; editing = false; expanded = false;
            setEditing(true);
            var opened = [editing, expanded];
            toggleDrawer();
            console.log(JSON.stringify([opened, [editing, expanded]]));'''),
            [[True, True], [False, False]])

    def test_the_choice_survives_a_reload(self):
        self.assertEqual(self.run_js('''
            setPad(false); setPad(true);
            console.log(JSON.stringify(
              [!!document.body._cls.nopad, store[PAD_STATE]]));'''),
            [False, ''])

    def test_hiding_it_leaves_exactly_one_way_back(self):
        """A hidden pad with no way back is a terminal you have to reload to
        type in. The grip is outside #pad, so hiding the pad cannot hide it."""
        self.assertIn('id="grip"', self.html)
        pad = self.html[self.html.index('<div id="pad">'):]
        self.assertLess(pad.index('</div>'), pad.index('id="grip"'))
        css = re.sub(r'/\*.*?\*/', '', self.html, flags=re.S)
        self.assertRegex(css, r'body\.touch\.nopad #pad \{[^}]*display: none')
        self.assertRegex(css, r'body\.touch\.nopad #grip \{[^}]*display: block')

    def test_the_controls_all_fit_across_a_phone(self):
        """Seven buttons and a spacer in 390px. Measured in a browser --
        390px of 390 exactly, with the gap down to 5 to make room for the
        seventh -- and the regression this catches is someone adding an eighth
        without measuring, because the one that does not fit goes off the edge
        rather than wrapping.

        `back` is here and `edit` is not, and that is the trade: the way out
        of a session had to be visible somewhere, and choosing which keys to
        keep is only meaningful while you can see the keys, which is when the
        sheet is open. So it moved into the sheet.
        """
        # Every button on the page, in order. The first three cost the
        # settings row nothing: `selpaste` and `seldone` live inside the
        # selecting layer and only exist while it is open, and `hist` and
        # `newbuild` overlay the terminal rather than taking a row.
        controls = re.findall(r'<button id="(\w+)"', self.html)
        self.assertEqual(controls,
                         ['selcopy', 'selpaste', 'seldone', 'hist', 'newbuild',
                          'edit', 'grab', 'back', 'fontminus', 'fontauto',
                          'fontplus', 'kbd', 'sel', 'hide', 'grip'])
        row = self.html[self.html.index('<div id="tabs">'):]
        row = row[:row.index('</div>')]
        self.assertEqual(re.findall(r'<button id="(\w+)"', row),
                         ['back', 'fontminus', 'fontauto', 'fontplus',
                          'kbd', 'sel', 'hide'])
        # Six, and each of the two that carry a verb gets a word. There was
        # never room for that at eight, which is why `‹` and `▾` were bare
        # glyphs -- and `▾` sat four buttons from `⌄`, which meant something
        # else entirely.
        self.assertIn('>‹ list</button>', self.html)
        self.assertIn('>hide</button>', self.html)

    def test_the_drawer_opens_from_the_last_row(self):
        """It used to open from a button in the settings row, at the far end
        of the pad from the drawer itself. A drawer opens from its edge -- and
        this drawer's edge is its bottom, one row above the settings row,
        because it grows upward and because that is the part of the pad a
        thumb reaches without the hand moving.

        The handle comes after the sheet in the document so it paints above
        it: opening the drawer must never cover the way to close it.
        """
        pad = self.html[self.html.index('<div id="pad">'):]
        order = re.findall(r'<(?:div|button) id="(\w+)"', pad)[1:]   # skip #pad
        for earlier, later in (('nav', 'grab'), ('pins', 'grab'),
                               ('keys', 'grab'), ('sheet', 'grab'),
                               ('grab', 'tabs')):
            with self.subTest(order=f'{earlier} before {later}'):
                self.assertLess(order.index(earlier), order.index(later),
                                f'wrong order: {order}')

    def test_a_rotation_redraws_only_when_the_count_actually_changed(self):
        """Rebuilding the pad on every resize would drop a held key and clear
        an armed modifier -- and the keyboard rising fires resize too."""
        self.assertEqual(self.run_js('''
            keys._width = 390; renderKeys();
            var marker = keys.children[0];
            recolumn();
            var untouched = keys.children[0] === marker;
            keys._width = 844; recolumn();
            var rebuilt = keys.children[0] !== marker;
            console.log(JSON.stringify([untouched, rebuilt, columnsUsed]));'''),
            [True, True, 10])


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

    def test_the_gesture_replaced_the_bar_rather_than_joining_it(self):
        """The bar cost about 70px of a phone screen permanently, for one link
        and a refresh button answering a question the five-second poll had
        usually already answered. Keeping both would be the worst of it: the
        cost stays and the gesture is redundant."""
        _head, body = self.get('/')
        page = body.decode('utf-8')
        self.assertNotIn('class="bar"', page)
        self.assertNotIn('id="refresh"', page)
        self.assertIn('id="pull"', page)
        # The one control that earned its place is still one tap away.
        self.assertIn('id="console"', page)
        self.assertIn('class="go"', page)

    def test_the_terminal_link_is_big_enough_to_hit(self):
        css = re.sub(r'/\*.*?\*/', '',
                     self.get('/')[1].decode('utf-8'), flags=re.S)
        rule = re.search(r'header \.go \{[^}]*\}', css)
        self.assertIsNotNone(rule)
        self.assertIn('min-width: 44px', rule.group(0))
        self.assertIn('min-height: 44px', rule.group(0))

    def test_the_page_ends_clear_of_the_home_indicator(self):
        """The bar used to carry that inset. Removing it without moving the
        inset leaves the queue flush against the bottom of the screen."""
        css = re.sub(r'/\*.*?\*/', '',
                     self.get('/')[1].decode('utf-8'), flags=re.S)
        rule = re.search(r'^\s*section \{[^}]*\}', css, re.M)
        self.assertIsNotNone(rule)
        self.assertIn('env(safe-area-inset-bottom)', rule.group(0))

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

    def test_the_debug_flag_survives_the_tap_into_a_session(self):
        """Tapping a row is the only navigation between the list and the
        terminal, and go() rebuilds the URL from scratch. Dropping the flag
        there leaves the readout reachable only by typing
        ?attach=NODE:SESSION&debug=1 by hand -- on a phone, which is the one
        device it exists for."""
        with open(os.path.join(web.ASSETS, 'dash.js'), encoding='utf-8') as f:
            js = f.read()
        go = _extract(js, 'go')
        self.assertIsNotNone(go)
        self.assertIn('debug', go)
        # Carried, never invented: a list opened without it stays clean.
        self.assertIn('/[?&]debug=1/.test(location.search)', go)

    def test_the_page_renders_no_html_from_the_wire(self):
        """Session names, node names and squeue output are all attacker-
        adjacent in the sense that they come from a cluster. textContent
        cannot execute; innerHTML can."""
        with open(os.path.join(web.ASSETS, 'dash.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertNotIn('innerHTML', js)
        self.assertIn('textContent', js)


@unittest.skipUnless(_node(), 'needs a javascript runtime')
class PullToRefreshTests(unittest.TestCase):
    """The gesture a phone reaches for first, and the page used to refuse it.

    `overscroll-behavior-y: contain` turns the browser's own off, so this one
    is drawn by hand -- which means the decision has to be right about the
    thing that matters most: a tap on a session row must never become a pull.
    Driven end to end in a real browser too, with synthesised touches; this is
    the part that can be reasoned about without one.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'dash.js'), encoding='utf-8') as f:
            cls.js = f.read()
        parts = []
        for head in ('var PULL_SLOP =', 'var TRIGGER =', 'var MAX_PULL =',
                     'var FOLLOW ='):
            match = re.search(re.escape(head) + r'[^;]*;', cls.js)
            assert match, head
            parts.append(match.group(0))
        parts.append(_extract(cls.js, 'pullState'))
        cls.harness = '\n'.join(parts)

    def state(self, dy):
        import subprocess
        out = subprocess.run(
            [_node(), '-e', self.harness +
             f'\nconsole.log(JSON.stringify(pullState({dy})));'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_a_tap_is_never_a_pull(self):
        """A thumb is not a stylus, and the rows underneath attach to a
        session on a single tap. Returning null rather than a zero-height
        state is the difference between leaving the page alone and taking
        the gesture and then doing nothing with it."""
        for dy in (0.5, 3, 7):
            with self.subTest(dy=dy):
                self.assertIsNone(self.state(dy))

    def test_a_short_pull_shows_the_strip_but_does_not_arm_it(self):
        page = self.state(40)
        self.assertEqual(page['state'], 'pull')
        self.assertGreater(page['height'], 0)

    def test_a_pull_past_the_trigger_arms(self):
        self.assertEqual(self.state(64)['state'], 'armed')
        self.assertEqual(self.state(200)['state'], 'armed')

    def test_the_strip_stops_following_before_it_eats_the_page(self):
        """A rubber band, so the end of the travel is felt rather than hit."""
        self.assertEqual(self.state(400)['height'], self.state(4000)['height'])
        self.assertLessEqual(self.state(4000)['height'], 96)

    def test_an_upward_drag_is_the_list_scrolling_and_nothing_else(self):
        for dy in (0, -1, -200):
            with self.subTest(dy=dy):
                self.assertEqual(self.state(dy), {'height': 0, 'state': ''})

    def test_the_strip_moves_less_than_the_finger(self):
        """Otherwise the list runs off the bottom of the screen before the
        gesture has said anything."""
        self.assertLess(self.state(64)['height'], 64)

    def test_the_page_no_longer_has_a_button_for_it(self):
        self.assertNotIn("getElementById('refresh')", self.js)
        self.assertIn('pullState', self.js)


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

    def test_a_machine_with_no_session_can_be_given_one(self):
        """A row with no session cannot be attached to -- there is nothing
        there. Starting a shell on that machine is the only thing you can
        want from it, and it is the row's own tap."""
        self.assertEqual(self.argv_for('/ws?shell=holygpu8a15504'),
                         list(self.COMMAND) + ['--shell=holygpu8a15504'])

    def test_a_node_verb_will_not_take_a_session_target(self):
        """Each verb checks the shape it actually means. --shell takes a
        machine, and NODE:SESSION is not one."""
        self.assertEqual(self.argv_for('/ws?shell=node:sess'),
                         list(self.COMMAND))
        for bad in ('--version', '-rf', '', 'a b'):
            with self.subTest(value=bad):
                path = '/ws?shell=' + urllib.parse.quote(bad, safe='')
                self.assertEqual(self.argv_for(path), list(self.COMMAND))

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

    def test_a_row_offers_the_verb_that_fits_what_it_is(self):
        """Attaching is what you want nine times in ten. A row with no
        session has nothing to attach to and wants a shell instead, and
        everything else lives on the row the dashboard is standing on."""
        self.assertIn("'attach' : 'shell'", self.dash)
        self.assertIn("go('select', row)", self.dash)
        self.assertIn("stopPropagation", self.dash)

    def test_a_row_with_no_session_offers_no_actions_menu(self):
        """There is nothing for the other verbs to act on."""
        body = _extract(self.dash, 'build')
        self.assertIn("row.kind === 'session'", body)

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
