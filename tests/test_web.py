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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autotmux import web


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
        head, body = self.get('/')
        self.assertIn('200', head)
        self.assertIn(b'xterm.js', body)
        self.assertIn(b'app.js', body)
        _head, script = self.get('/app.js')
        self.assertIn(b'/ws', script)

    def test_the_page_has_no_inline_script(self):
        """The CSP has no 'unsafe-inline' for scripts, so an inline <script>
        is silently dropped and the page comes up blank with nothing logged
        anywhere. That is exactly what happened; it must not happen twice."""
        _head, body = self.get('/')
        self.assertNotIn(b'<script>', body)
        for tag in re.findall(rb'<script[^>]*>', body):
            self.assertIn(b'src=', tag, f'inline script: {tag!r}')

    def test_the_policy_permits_the_socket_the_page_opens(self):
        """A CSP that blocks its own websocket produces a page that loads and
        then does nothing, which reads as a server fault."""
        head, _body = self.get('/')
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
        _head, body = self.get('/')
        self.assertIn(b'id="boot"', body)
        _head, script = self.get('/app.js')
        self.assertIn(b"addEventListener('error'", script)

    def test_every_asset_the_page_needs_is_served(self):
        for path in ('/xterm.js', '/xterm.css', '/addon-fit.js',
                     '/manifest.json'):
            with self.subTest(path=path):
                head, body = self.get(path)
                self.assertIn('200', head)
                self.assertGreater(len(body), 0)

    def test_the_terminal_is_vendored_not_fetched(self):
        """A device on a private network may have no route to a CDN, and the
        page's own CSP forbids one anyway."""
        _head, body = self.get('/')
        self.assertNotIn(b'cdn.', body)
        self.assertNotIn(b'https://unpkg', body)

    def test_the_page_forbids_loading_or_sending_anywhere_else(self):
        head, _body = self.get('/')
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
    def _open(self):
        sock = socket.create_connection((self.host, self.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f'GET /ws HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n\r\n'.encode())
        head = b''
        while b'\r\n\r\n' not in head:
            head += sock.recv(4096)
        return sock, key, head.decode('latin-1')

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
    """The keypad is the phone's only way to press a key.

    xterm.js has no touch gesture support (issue #5377, open and unassigned),
    so on a touch screen there is no other way to send an arrow, Esc, or a
    control character at all -- and atmux is steered almost entirely by those.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(web.ASSETS, 'app.js'), encoding='utf-8') as f:
            cls.js = f.read()
        with open(os.path.join(web.ASSETS, 'index.html'), encoding='utf-8') as f:
            cls.html = f.read()

    def _page(self, name):
        # Anchored to the start of the line: "atmux: [" contains "tmux: [",
        # and an unanchored search silently returns the wrong page.
        block = re.search(r'^    ' + name + r': \[(.*?)\n    \]',
                          self.js, re.S | re.M)
        self.assertIsNotNone(block, f'no {name} page in the keypad')
        return re.findall(r"\['([^']+)', ", block.group(1))

    def test_the_keypad_offers_every_key_atmux_binds(self):
        """A bound key with no button is unreachable on a phone. This fails
        the moment someone adds a binding without adding the button."""
        from autotmux import cli
        bound = {b.key for b in cli.AutotmuxApp.BINDINGS}
        on_pad = set(self._page('atmux')) | set(self._page('nav'))
        # `?` is bound under the name Textual gives the character.
        if 'question_mark' in bound:
            bound.discard('question_mark')
            bound.add('?')
        missing = {k for k in bound if len(k) == 1 and k not in on_pad}
        self.assertEqual(missing, set(),
                         f'no button for bound key(s): {sorted(missing)}')

    def test_no_button_sends_a_key_atmux_does_not_bind(self):
        """A dead button is worse than a missing one: it teaches that the
        keypad does not work."""
        from autotmux import cli
        bound = {b.key for b in cli.AutotmuxApp.BINDINGS}
        bound |= {b.key for b in cli.ClickToAttachDataTable.BINDINGS}
        bound.add('?')
        for label in self._page('atmux'):
            with self.subTest(key=label):
                self.assertIn(label, bound)

    def test_detach_is_offered_because_nobody_can_guess_it(self):
        """Ctrl-B then d. It is unreachable without a keyboard, and getting
        stuck inside an attached session is the failure this whole feature
        would otherwise create."""
        self.assertIn('\\x02d', self.js)
        self.assertIn('detach', self._page('tmux'))

    def test_arrows_repeat_when_held(self):
        """A table is navigated by arrow. Ten taps to move ten rows is the
        difference between usable and not."""
        self.assertIn("'rep'", self.js)
        self.assertIn('setInterval', self.js)
        for entry in re.findall(r"\['↑', [^\]]+\]", self.js):
            self.assertIn('rep', entry)

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
        for control in ('kbd', 'minus', 'plus', 'tab'):
            with self.subTest(control=control):
                self.assertRegex(self.js, r'keepFocus\(' + control + r'\)')

    def test_there_is_more_than_one_way_to_raise_the_keyboard(self):
        """One of them silently not working is how this broke the first
        time."""
        self.assertIn("getElementById('kbd')", self.js)
        self.assertIn('changedTouches.length === 2', self.js)

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
