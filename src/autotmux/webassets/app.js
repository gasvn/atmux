(function () {
  'use strict';
  var boot = document.getElementById('boot');
  var booterr = document.getElementById('booterr');
  function fail(what) {
    if (!boot) return;
    boot.style.display = 'flex';
    if (booterr) booterr.textContent = String(what);
  }
  window.addEventListener('error', function (e) {
    fail((e && e.message) || 'script error');
  });
  if (typeof Terminal !== 'function') {
    fail('xterm.js did not load — check the browser console and the page CSP');
    return;
  }

  var touch = (navigator.maxTouchPoints || 0) > 0 ||
              window.matchMedia('(pointer: coarse)').matches;

  // Font size is derived, not chosen -- see the layout section below. What is
  // remembered here is only an override: someone who has pinched to read a
  // detail should find that size again next visit, and everyone else should
  // never have to think about it. null means "let the layout decide".
  var MIN_FONT = 7, MAX_FONT = 28;
  function storedFont() {
    try {
      var raw = localStorage.getItem('atmux.fontSize');
      if (raw === null || raw === 'auto') return null;
      var v = parseFloat(raw);
      if (v >= MIN_FONT && v <= MAX_FONT) return v;
    } catch (e) {}
    return null;
  }
  var manualFont = storedFont();

  var term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    // A seed for the first font measurement only; the layout replaces it
    // before the first frame unless there is a stored override.
    fontSize: manualFont || 13,
    scrollback: 5000,
    macOptionIsMeta: true,
    // #121212 is what Textual's textual-dark theme actually paints. Matching
    // it is worth doing, but it is not what keeps the edges clean: the app
    // paints its table rows lighter than its own background, so no single
    // colour can hide a strip that sits outside the canvas. Not leaving a
    // strip is the actual fix, and that is the layout section's job.
    theme: { background: '#121212', foreground: '#d8d8dc' }
  });
  var host = document.getElementById('term');
  term.open(host);
  var hist = document.getElementById('hist');
  var buildLine = document.getElementById('buildline');

  var textarea = host.querySelector('textarea');

  var status = document.getElementById('status');
  var hideTimer = null;
  function say(text, sticky) {
    status.textContent = text;
    // Nothing to say is not the same as saying nothing: an empty sticky box
    // is a grey rectangle sitting over the terminal for ever.
    status.style.display = text ? 'block' : 'none';
    clearTimeout(hideTimer);
    if (!text) return;
    if (!sticky) hideTimer = setTimeout(function () {
      status.style.display = 'none';
    }, 1600);
  }

  // ── connection ────────────────────────────────────────────────────────
  var ws = null, retry = 0, closed = false;

  // Relative to the page, not to the host root. `tailscale serve --set-path
  // /atmux` puts this page at /atmux/, and a hard-coded /ws would reach for a
  // socket that is not there -- which is how one hostname ends up able to
  // carry only one service.
  function socketURL() {
    var scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    var base = location.pathname.replace(/[^/]*$/, '');
    // Everything the pty needs to know goes here, because the pty is created
    // when this socket is upgraded and there is no channel before that.
    //
    //   touch   whether this client draws its own controls -- a property of
    //           the client, not the server: a phone and a laptop reach the
    //           same process
    //   attach  which session to land in, forwarded from the page's own URL
    //           so that tapping a row on the list goes to that session
    //           rather than to a second copy of the list
    //   select  land on that row, where every other action reaches it
    //   shell   there is no session yet -- start one, on that machine
    //
    // All three the server accepts, and every one it accepts. `shell` was
    // missing here and nowhere else: the dashboard wrote console/?shell=NODE,
    // this page dropped it on the way to the socket, and the server -- which
    // reads the query off the *socket* URL, because that is when the pty is
    // made -- saw nothing to open. So tapping a machine, and "Open a shell
    // here", both landed on a plain dashboard: the one screen that looks
    // enough like success to be mistaken for it.
    var query = [];
    if (touch) query.push('touch=1');
    var here = new URLSearchParams(location.search);
    ['attach', 'select', 'shell'].forEach(function (verb) {
      var target = here.get(verb);
      if (target) query.push(verb + '=' + encodeURIComponent(target));
    });
    return scheme + '://' + location.host + base + 'ws' +
           (query.length ? '?' + query.join('&') : '');
  }

  function connect() {
    ws = new WebSocket(socketURL());
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () { retry = 0; say('connected'); sendResize(); };
    ws.onmessage = function (event) { feed(new Uint8Array(event.data)); };
    ws.onclose = function (event) {
      if (closed) return;
      // The program finished -- you detached, or quit the dashboard. Going
      // back to the list is what happens next; reconnecting would put you on
      // a terminal reconnecting to nothing, forever.
      if (event && event.code === 1000 && event.reason === 'exit') {
        closed = true;
        location.href = new URL('../', location.href).toString();
        return;
      }
      // Otherwise the socket dropped, which a phone does every time it locks
      // or changes network -- the normal case, not the exceptional one.
      retry = Math.min(retry + 1, 6);
      say('reconnecting…', true);
      setTimeout(connect, Math.min(500 * Math.pow(2, retry - 1), 10000));
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  }
  function sendText(text) { send(new TextEncoder().encode(text)); }
  function sendResize() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ t: 'resize', cols: term.cols, rows: term.rows }));
    }
  }

  // Through the latch, so an armed ctrl applies to what you type on the
  // software keyboard and not only to what you tap on the pad. Anything else
  // would be a modifier that works on some keys and silently not on others.
  term.onData(function (data) { typed(data); });
  term.onBinary(function (data) {
    var bytes = new Uint8Array(data.length);
    for (var i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 255;
    send(bytes);
  });

  // ── the iPad Ctrl-C bug ───────────────────────────────────────────────
  // Safari on iPadOS/iOS/visionOS reports Ctrl-C from a hardware keyboard as
  // keyCode 13 (Enter). xterm.js merged a fix (PR #5742) into master in March
  // 2026 for the 7.0.0 milestone, but no published build carries it: latest is
  // 6.0.0 and even 6.1.0-beta has no such special case. Ten lines here does
  // not depend on an upstream release.
  //
  // iPadOS reports platform "MacIntel", so a Mac that also reports touch
  // points is the only reliable signal that this is really an iPad.
  var appleTouch = /Mac|iPad|iPhone|iPod/.test(navigator.platform || '')
                   && (navigator.maxTouchPoints || 0) > 1;
  term.attachCustomKeyEventHandler(function (event) {
    if (event.type !== 'keydown') return true;
    if (appleTouch && event.ctrlKey && !event.metaKey && !event.altKey
        && (event.keyCode === 13 || event.keyCode === 10)) {
      sendText('\x03');
      event.preventDefault();
      return false;
    }
    return true;
  });

  // ── the software keyboard ─────────────────────────────────────────────
  // Two attempts at coaxing iOS into presenting it failed, and both failed
  // silently. The reason is that we were calling focus() from JavaScript, and
  // Safari only reliably raises the keyboard when the *tap itself* lands on a
  // focusable element -- that is why every ordinary web form works and none of
  // this did.
  //
  // So stop calling focus(). Typing mode stretches xterm's own helper textarea
  // over the terminal, transparent: the next tap lands on a real text input
  // and the platform does what it always does. Tap-to-attach is off while it
  // is on, which is the right trade -- you are typing, not navigating, and the
  // published keys still cover every action on the screen.
  var kbdOpen = false;
  // ── typing on a software keyboard ───────────────────────────────────
  // Holding delete removed one character and stopped. iOS repeats the
  // software keyboard's delete key only while the field still has something
  // to delete, and xterm's helper textarea is kept empty -- measured,
  // value.length was 0 -- so the first press deleted nothing the platform
  // could see and the repeat never started.
  //
  // Padding xterm's own helper was the first fix and it was wrong: that
  // meant sharing one element with a listener registered before ours, and at
  // the target element listeners run in registration order whatever their
  // capture flag, so stopImmediatePropagation was always too late. Measured
  // with real keystrokes -- the last probe used a synthetic Event('input')
  // which xterm ignores, so it tested my handler alone and said all was
  // well -- a typed space arrived as two.
  //
  // So the keyboard types into a field of ours that xterm knows nothing
  // about, and the whole translation is ours. Off typing mode it is not even
  // displayed, and xterm has its own textarea back exactly as before.
  var typebox = document.getElementById('typebox');
  var PAD = ' '.repeat(64);
  var lastTyped = PAD;

  function repad() {
    if (!typebox) return;
    typebox.value = PAD;
    lastTyped = PAD;
    try { typebox.setSelectionRange(PAD.length, PAD.length); } catch (e) {}
  }

  // Compared against what was there, not filtered for padding. Filtering
  // spaces out of the difference is what made a typed space vanish -- the
  // padding is spaces, so a space is indistinguishable from padding by
  // content and only distinguishable by position.
  function onTypedInput(event) {
    if (!typebox || event.isComposing) return;
    var now = typebox.value;
    var was = lastTyped;
    var head = 0;
    while (head < now.length && head < was.length && now[head] === was[head]) {
      head += 1;
    }
    var tail = 0;
    while (tail < now.length - head && tail < was.length - head
           && now[now.length - 1 - tail] === was[was.length - 1 - tail]) {
      tail += 1;
    }
    var removed = was.length - head - tail;
    var inserted = now.slice(head, now.length - tail);
    if (removed > 0) typed('\x7f'.repeat(removed));
    if (inserted) typed(inserted);
    lastTyped = now;
    // Refilled only when it is running low, never after every keystroke.
    // A held delete is one long burst of platform-driven deletions, and
    // rewriting the field's value in the middle of one is how you stop it:
    // the repeat is the platform's, and it is chewing on this buffer. Half
    // the pad is thirty-two deletions of headroom, which outlasts any
    // realistic hold, and the refill lands between bursts.
    if (now.length < PAD.length / 2 || now.length > PAD.length * 2) repad();
  }

  function onTypedKey(event) {
    var send = {Enter: '\r', Tab: '\t', Escape: '\x1b',
                ArrowUp: '\x1b[A', ArrowDown: '\x1b[B',
                ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D'}[event.key];
    if (!send) return;                    // let the field have it
    event.preventDefault();
    typed(send);
  }

  if (typebox) {
    typebox.addEventListener('input', onTypedInput);
    typebox.addEventListener('keydown', onTypedKey);
    typebox.addEventListener('compositionend', function () { repad(); });
  }

  function setKeyboard(open) {
    if (!textarea) return;
    kbdOpen = open;
    var button = document.getElementById('kbd');
    if (button) button.classList.toggle('on', open);
    if (typebox) typebox.classList.toggle('on', open);
    if (open) {
      repad();
      // xterm's own field must not also be focused: two focused editables
      // is two places the keyboard could go.
      try { textarea.blur(); } catch (e) {}
      try { typebox.focus(); } catch (e) {}
      say('tap the screen to type');
    } else {
      if (typebox) { typebox.value = ''; typebox.blur(); }
      term.focus();
      say('keyboard off');
    }
  }

  // A button that takes focus first leaves nothing for the terminal, so every
  // control on the pad suppresses its default pointerdown.
  function keepFocus(element) {
    if (!element) return;
    element.addEventListener('pointerdown', function (e) { e.preventDefault(); });
  }

  // ── the keypad ────────────────────────────────────────────────────────
  // Not written here. The dashboard publishes the bindings that are live on
  // its current screen (keypad.py, OSC 7710) and this renders them.
  //
  // The list used to live in this file, copied by hand from the python. It
  // did not fit one screen, so it grew three pages, so the layout key ended
  // up two taps deep behind a tab nobody would think to open -- and a copy
  // is only correct on the day it is written. Now there is one list, it is
  // the app's own, and it changes when a modal opens because the app's
  // bindings do.
  //
  // Nothing is hard-coded as a fallback on purpose: if no bindings arrive,
  // whatever is running is not this dashboard, and inventing keys for it
  // would be guessing. The pad then offers only the keyboard.
  var KEYS_OSC = 7710;

  // Except these. There are two kinds of key and only one of them belongs to
  // the app: movement and escape are *terminal* primitives, true of every
  // program, and they have to work when nothing has published anything at
  // all -- during a reconnect, inside a program that is not this dashboard,
  // after a handover to tmux.
  //
  // They were briefly deleted on the theory that a tap on a row selects it.
  // It does not. Measured against the cursor colour rather than against "did
  // the screen change" -- the table re-sorts by idle time every few seconds,
  // which looks identical to a selection moving:
  //
  //     mouse click on a row : no move
  //     finger tap on a row  : no move
  //     arrow-down key       : MOVED
  //
  // xterm.js has no touch support (issue #5377, open), and the table attaches
  // on a single click, so making taps reach it would turn a mis-tap into an
  // attach. These are the only way to move, and the phone had none.
  //
  // For a while this held three of them -- ↑ ↓ esc -- which is a row chosen
  // for one screen of this dashboard rather than for a terminal. Once you
  // attach, which is the entire point, that leaves no tab (so no completion),
  // no ← → (so no editing a command line), no ctrl (so no ^C ^R ^A) and no way
  // to reach a single tmux binding.
  //
  // The shape is Termux's accessory row, which is also Blink's and Termius'
  // minus their platform keys: esc, the modifiers, tab, the arrows. Following
  // it is deliberate -- it is the row a phone user already has muscle memory
  // for -- but it is followed rather than copied, because those bars sit
  // *above* a software keyboard that is always up, and this pad stands in
  // place of one that is usually down. Enter is free for them and absent here,
  // and ↑ then Enter to re-run the last command is the single most common
  // thing anyone does on a phone terminal. So Enter earns a permanent key.
  //
  // Nine across a 390px screen is ~37px each. That is narrower than Apple's
  // 44pt guidance and wider than the iOS keyboard's own letter keys, which are
  // about 32px and which nobody has trouble hitting.
  // Two clusters, not one row of ten.
  //
  // They were laid out in array order -- esc ctrl alt pfx tab / ⏎ ← ↓ ↑ → --
  // which put `↑` to the *right* of `↓`, so every movement needed a look
  // first. An inverted T is what every keyboard since the Model M has used
  // for these four, and for the reason that matters here: you find it once by
  // shape and after that your thumb knows where it is. On a screen with no
  // edges to feel, that is the only way a key gets found without looking.
  //
  // The two clusters are laid out side by side and both are two rows deep, so
  // this costs exactly what the row of ten cost.
  var MOD_KEYS = [
    { l: 'esc', k: '\x1b' },
    { l: 'ctrl', m: 'ctrl' },
    { l: 'alt', m: 'alt' },
    // A prefix that latches is a different offer from the prefix button this
    // once had and dropped. That one sent C-b and then wanted a letter, which
    // meant raising the keyboard over the screen you were acting on. Latched,
    // it survives the keyboard, it survives being locked for a run of chords,
    // and -- unlike ctrl and alt, which rewrite a character -- it is a key
    // that comes *first*, so it composes with the arrows too: `prefix ←`
    // selects the pane to the left. Four verbs I chose become the whole
    // binding table.
    { l: 'pfx', m: 'prefix' },
    { l: 'tab', k: '\t' },
    { l: '⏎', k: '\r' }
  ];

  // Three across, two deep, corners empty. `null` is a hole rather than a
  // missing element: the shape is the point, and a cross with its corners
  // closed up is a row again.
  var DPAD_KEYS = [
    null, { l: '↑', k: '\x1b[A' }, null,
    { l: '←', k: '\x1b[D' },
    { l: '↓', k: '\x1b[B' },
    { l: '→', k: '\x1b[C' }
  ];

  var NAV_KEYS = MOD_KEYS.concat(DPAD_KEYS.filter(Boolean));

  // ── modifiers, which latch ────────────────────────────────────────────
  // A thumb cannot hold one key and press another, so ctrl and alt are taps:
  // once arms them for the next keystroke, twice locks them, a third time
  // clears. Termux, Blink and Termius all landed on this independently, which
  // is about as strong as evidence for an interaction gets.
  //
  // It is also what keeps the pad finite. ^R, ^W, ^K, M-b and every other
  // chord become reachable without a button each; enumerating them was the
  // old strategy and it is why the pad once had three pages nobody opened.
  var OFF = 0, ARMED = 1, LOCKED = 2;
  var latch = { ctrl: OFF, alt: OFF, prefix: OFF };
  var modButtons = {};

  function paintLatch() {
    Object.keys(modButtons).forEach(function (name) {
      var button = modButtons[name];
      button.classList.toggle('armed', latch[name] === ARMED);
      button.classList.toggle('on', latch[name] === LOCKED);
      button.setAttribute('aria-pressed', latch[name] === OFF ? 'false' : 'true');
    });
  }

  // What ctrl does to a character, per the ASCII table the terminal is built
  // on: the letters fold to 1..26 and the handful of symbols that also have
  // control codes are named. Anything else comes back unchanged rather than
  // being mangled into a neighbouring byte.
  function ctrlify(text) {
    var code = text.charCodeAt(0);
    if (code >= 97 && code <= 122) return String.fromCharCode(code - 96);
    if (code >= 65 && code <= 90) return String.fromCharCode(code - 64);
    var extra = { '@': '\x00', ' ': '\x00', '[': '\x1b', '\\': '\x1c',
                  ']': '\x1d', '^': '\x1e', '_': '\x1f', '?': '\x7f' };
    return Object.prototype.hasOwnProperty.call(extra, text)
      ? extra[text] : text;
  }

  // Only a single printable character can carry a modifier. An arrow is
  // already an escape sequence and a tmux chord is already two bytes: folding
  // either would produce a byte nobody asked for, so they pass through and
  // clear the latch instead, which is what a mis-tap deserves.
  function applyLatch(data) {
    if (latch.ctrl === OFF && latch.alt === OFF && latch.prefix === OFF) {
      return data;
    }
    var out = data;
    if (data.length === 1 && data.charCodeAt(0) >= 0x20) {
      if (latch.ctrl !== OFF) out = ctrlify(out);
      if (latch.alt !== OFF) out = '\x1b' + out;
    }
    // Outside that test on purpose: the prefix is not a transformation of the
    // key, it is a keystroke that precedes it, so it composes with sequences
    // ctrl and alt have to refuse. `prefix ←` is select-pane -L.
    if (latch.prefix !== OFF) out = prefixSeq + out;
    if (latch.ctrl === ARMED) latch.ctrl = OFF;
    if (latch.alt === ARMED) latch.alt = OFF;
    if (latch.prefix === ARMED) latch.prefix = OFF;
    paintLatch();
    return out;
  }

  // ── tmux, as verbs rather than as a prefix ────────────────────────────
  // `prefix` on its own was already offered and was very nearly useless: it
  // sends C-b and then wants a letter, and a letter means opening the software
  // keyboard, which covers the screen you were trying to act on. One button
  // per chord is what every ssh client that bothers with tmux does, and it
  // turns "detach" from four taps into one.
  //
  // C-b is tmux's default. The dashboard overrides it if it knows better --
  // see the OSC handler -- because `set -g prefix C-a` is a thing people do
  // and nothing on the wire announces it.
  var prefixSeq = '\x02';
  // The last mode the app published: 'app' (the dashboard is drawing) or
  // 'external' (it has handed the screen to tmux). Read by swipeKind, which
  // has to know which of the two is listening.
  var published = '';
  // Order matters: the first few are what the pad shows without being asked,
  // so they should be the things a gesture cannot already do. Switching
  // windows is a sideways swipe now, and two of the five visible buttons
  // were spent on it -- they are still here, further down, because a button
  // you can aim at is the right answer when the swipe is the wrong one.
  //
  // `detach` is marked. It is the only one on this pad that ends what you
  // are looking at, and it sat next to `new win` wearing the same grey.
  var TMUX_VERBS = [
    { l: 'detach', s: 'd', tone: 'leave' },
    { l: 'new win', s: 'c' },
    { l: 'zoom', s: 'z' },
    { l: 'scroll', s: '[' },
    { l: 'windows', s: 'w' },
    { l: '◀ win', s: 'p' },
    { l: 'win ▶', s: 'n' },
    { l: 'split ─', s: '"' },
    { l: 'split │', s: '%' },
    { l: 'pane ▶', s: 'o' },
    { l: 'rename', s: ',' },
    { l: 'prefix', s: '' }
  ];
  function tmuxKeys() {
    return TMUX_VERBS.map(function (verb) {
      return { l: verb.l, k: prefixSeq + verb.s, tone: verb.tone };
    });
  }

  // The chords common enough to deserve a button rather than an armed ctrl and
  // a hunt for a letter. ^C above all: it is what you want when something is
  // already going wrong, and that is the worst moment to ask for three taps.
  var CTRL_KEYS = [
    { l: '^C', k: '\x03' }, { l: '^D', k: '\x04' }, { l: '^Z', k: '\x1a' },
    { l: '^L', k: '\x0c' }, { l: '^R', k: '\x12' }, { l: '^A', k: '\x01' },
    { l: '^E', k: '\x05' }, { l: '^W', k: '\x17' }, { l: '^U', k: '\x15' },
    { l: '^K', k: '\x0b' }
  ];

  var MOVE_KEYS = [
    { l: 'home', k: '\x1b[H' }, { l: 'end', k: '\x1b[F' },
    { l: 'PgUp', k: '\x1b[5~' }, { l: 'PgDn', k: '\x1b[6~' },
    { l: '⌫', k: '\x7f' }, { l: 'del', k: '\x1b[3~' },
    { l: 'space', k: ' ' }, { l: '⇧tab', k: '\x1b[Z' }
  ];

  // The characters a shell needs constantly and iOS buries two keyboard layers
  // deep. `-` and `/` are in Termux's default row for exactly this reason.
  var TYPE_KEYS = [
    { l: '-', k: '-' }, { l: '_', k: '_' }, { l: '/', k: '/' },
    { l: '\\', k: '\\' }, { l: '|', k: '|' }, { l: '~', k: '~' },
    { l: '*', k: '*' }, { l: '$', k: '$' }, { l: '"', k: '"' },
    { l: "'", k: "'" }, { l: '`', k: '`' }, { l: '^', k: '^' }
  ];

  // ── the keys you keep ─────────────────────────────────────────────────
  // Forty-two keys is the right number to *have* and the wrong number to
  // look at. Which eight of them matter is a question only the person
  // holding the phone can answer -- someone living in tmux windows wants
  // `new win` and `win ▶`; someone debugging wants ^C and ^R -- so it is
  // answered by them and remembered, rather than guessed at here.
  //
  // Stored as labels, not as bytes. A pinned `detach` has to follow a
  // rebound prefix, and bytes frozen into localStorage would not.
  //
  // Only the client's own keys can be pinned. The app's are published, they
  // change with its screen, and freezing one would produce exactly the thing
  // this file keeps guarding against: a button whose label outlived what it
  // does.
  var PINS = 'atmux.pins', MAX_PINS = 8;
  // Four to start with, so the row exists on first run. It used to render
  // empty, which meant the whole feature's on-screen presence was an
  // unlabelled star -- and nobody discovers a row that is not there. These
  // four are the ones that are right before anything is known about the
  // person: stop what is running, find the command you ran before, read what
  // scrolled past, and the one character every path needs that iOS buries two
  // keyboard layers deep.
  //
  // Not `detach`: it is in the settings row now, and three ways to leave
  // stacked one above the other is not three times as useful.
  //
  // Only a starting point, and only on the very first run.
  var DEFAULT_PINS = ['^C', '^R', 'PgUp', '/'];
  var pins = loadPins(), editing = false;

  function loadPins() {
    var stored = null;
    try { stored = localStorage.getItem(PINS); } catch (e) {}
    // Never written: nobody has been here, so start them off with four.
    // Anything else -- including an entry that no longer parses -- means a
    // choice was made, and the answer to a choice may be none. Handing the
    // defaults back to someone who deliberately cleared the row would be the
    // pad refilling itself every time they opened it.
    if (stored === null) return DEFAULT_PINS.slice();
    try {
      var raw = JSON.parse(stored);
      if (!Array.isArray(raw)) return [];
      return raw.filter(function (label) {
        return typeof label === 'string' && label && label.length <= 24;
      }).slice(0, MAX_PINS);
    } catch (e) { return []; }
  }

  function savePins() {
    // Always writes, even when the list is empty: the key existing is what
    // records that a choice was made. See loadPins.
    try { localStorage.setItem(PINS, JSON.stringify(pins)); } catch (e) {}
  }

  function togglePin(label) {
    var at = pins.indexOf(label);
    if (at >= 0) pins.splice(at, 1);
    else if (pins.length < MAX_PINS) pins.push(label);
    else return say('that is as many as fit');
    savePins();
    renderKeys();
  }

  var pad = document.getElementById('pad');
  var keys = document.getElementById('keys');
  var sheet = document.getElementById('sheet');
  var sheetKeys = document.getElementById('sheetkeys');
  var nav = document.getElementById('nav');
  var pinRow = document.getElementById('pins');
  var expander = document.getElementById('grab');
  var grabCount = document.getElementById('grabcount');

  // ── how tall the drawer opens ─────────────────────────────────────────
  // It used to open to every pixel it was allowed, which on a 390x844 phone
  // left 96px of terminal -- about seven rows. Scrolling was already there as
  // a fallback and did nothing, because nothing ever needed to scroll: the
  // sheet simply took the screen.
  //
  // So it opens at four rows and scrolls, and the handle drags it to anything
  // between one row and the whole allowance. Measured in rows rather than
  // pixels because "four rows of keys" is what has to stay true when the key
  // size changes -- in landscape a key is 40px and here it is 44.
  var TERM_KEEP = 96;                  // the strip of terminal never covered
  var SHEET_PEEK_ROWS = 4;
  var sheetHeight = 0;                 // remembered across opens; 0 = ask

  // The first row of keys, wherever it is: the sections are boxed, so it is
  // no longer a direct child.
  function firstRow(node) {
    for (var i = 0; i < node.children.length; i++) {
      var child = node.children[i];
      if (String(child.className).indexOf('krow') >= 0) return child;
      var found = firstRow(child);
      if (found) return found;
    }
    return null;
  }

  function sheetRowHeight() {
    var row = firstRow(sheetKeys);
    var box = row ? row.getBoundingClientRect().height : 0;
    return box > 0 ? box + 5 : 49;       // + .krow margin-bottom
  }

  // ── the space the drawer gets for free ────────────────────────────────
  // The pad's own key rows. Every key in them is also in the list, so while
  // the drawer is open they are showing the reader something the drawer is
  // showing them again -- and the drawer was taking the terminal's rows to do
  // it. Measured on a 390x844 phone: 255px, against a drawer that cost the
  // terminal 252px to show four rows.
  //
  // So the drawer opens over these first. Only what it needs beyond them
  // comes out of the terminal.
  function padCover() {
    var total = 0;
    [nav, pinRow, keys].forEach(function (box) {
      if (box && box.style.display !== 'none') {
        total += box.getBoundingClientRect().height;
      }
    });
    return total;
  }

  // ── the blank end of the terminal ─────────────────────────────────────
  // The other space the drawer can have for nothing. tmux sizes a window to
  // the smallest client attached to it, so a session that is also open on a
  // laptop draws its status line well above this terminal's last row and
  // leaves everything below it empty. A phone can be showing forty rows of
  // which twelve are blank, and the drawer was opening to two rows above
  // them rather than using them.
  //
  // Counted from the buffer rather than guessed at, and re-read as output
  // arrives, because it shrinks: a bare shell starts with one prompt and
  // forty-five empty rows, and fills them.
  // The one row that has to stay out from under the drawer is the one being
  // written on. Not "the last row with anything in it": with a session also
  // open on a laptop, tmux sizes the window to the smaller client, fills the
  // rest of this one with its own dotted filler and pins its status line to
  // the very last row -- so "last row with anything in it" is the status
  // line, and holding *that* above the drawer keeps a screenful of filler on
  // display and pushes the shell off the top.
  //
  // The cursor is where you are. Everything below it is either blank, filler
  // or a status line, and all three are worth less than four more rows of
  // keys.
  var CURSOR_MARGIN = 1;                 // rows of air under it
  function keepVisibleRow() {
    try {
      var y = term.buffer.active.cursorY;
      if (typeof y === 'number' && y >= 0 && y < term.rows) {
        return Math.min(term.rows - 1, y + CURSOR_MARGIN);
      }
    } catch (e) { /* fall through */ }
    try {
      var buffer = term.buffer.active;
      for (var i = term.rows - 1; i >= 0; i--) {
        var line = buffer.getLine(buffer.viewportY + i);
        if (line && line.translateToString(true).trim()) return i;
      }
    } catch (e) { return term.rows - 1; }
    return -1;
  }

  // Everything the drawer may cover without the terminal losing a line it is
  // showing: the pad's own key rows, plus whatever is blank at the bottom.
  function freeSpace() {
    var row = keepVisibleRow();
    var below = row < 0 ? 0 : (term.rows - 1 - row) * lineHeight();
    return padCover() + Math.max(0, Math.round(below));
  }

  // Where the drawer's lower edge sits: the top of the handle, which is one
  // row above the settings row. Expressed as an offset from the pad's bottom,
  // because that is what `bottom` on an absolutely positioned box means here.
  function sheetBase() {
    if (!pad || !expander) return 0;
    return Math.max(0, Math.round(pad.getBoundingClientRect().bottom
                                  - expander.getBoundingClientRect().top));
  }

  // The most it may ever be. The terminal's share is not a percentage: #app
  // is sized to visualViewport, so the room actually available changes with
  // the software keyboard and with rotation, and the only honest source is
  // the terminal box itself.
  function sheetRoom() {
    return Math.max(120, padCover()
                    + host.getBoundingClientRect().height - TERM_KEEP);
  }

  // Open to everything that is free, so the default costs the terminal
  // nothing and uses all of what it is allowed. Four rows is the floor for a
  // screen where that is not much.
  // How tall the whole list actually is. The sheet is the scroller, so this
  // box keeps its natural height inside it.
  function sheetContent() {
    if (!sheetKeys) return 0;
    var rows = Math.ceil(sheetKeys.getBoundingClientRect().height);
    // Zero means the keys have not been laid out yet, which is not the same
    // as a drawer with nothing in it -- and the caller has to be able to tell
    // the difference, because capping the drawer at an unmeasured content
    // height opens it at nothing. Said here, rather than left for the caller
    // to infer from a suspiciously small number: it used to infer it, and
    // adding one fixed-height line below the keys was enough to carry an
    // empty drawer over the threshold and open it two rows tall.
    if (!rows) return 0;
    // + #sheet's own padding, 8 above and 6 below, and the build line under
    // the keys. Small, but it has to be counted: this number is what stops
    // the drawer opening taller than it has anything to show, so anything
    // left out of it lands just under the fold of a drawer that reports
    // itself as exactly full.
    var height = rows + 14;
    if (buildLine) {
      height += Math.ceil(buildLine.getBoundingClientRect().height);
    }
    return height;
  }

  function sheetPeek() {
    // One heading plus the rows. There is no toolbar to allow for any more:
    // the keep control rides the first heading instead of taking a row.
    var rows = 25 + SHEET_PEEK_ROWS * sheetRowHeight();
    // Never taller than there is list to put in it. A wide screen fits ten
    // keys to a row, so the same sixty keys are six rows there and ten on a
    // phone -- and opening to all the free space made an iPad show 390px of
    // keys in a 990px drawer covering most of the screen. Measured; the phone
    // was fine and the tablet was two thirds empty, which is the same rule
    // failing at a size it was never checked at.
    //
    // A content height of almost nothing means nothing has been rendered yet,
    // not that there is nothing to show.
    var content = sheetContent();
    var want = content > 40 ? Math.min(freeSpace(), content) : freeSpace();
    return Math.min(sheetRoom(), Math.max(rows, want));
  }

  // The smallest a drag may leave it. Below this the intent is closed rather
  // than tiny, which `up` on the handle reads it as.
  function sheetFloor() {
    return Math.min(sheetRoom(), 110);
  }

  // How far the terminal has to slide for its last written line to stay above
  // the drawer. Asked rather than assumed, and re-asked as output arrives:
  // the blank rows the drawer is sitting over are not permanently blank.
  var shiftedBy = 0;
  function neededShift() {
    if (!expanded || !sheet) return 0;
    var row = keepVisibleRow();
    if (row < 0) return 0;
    var box = host.getBoundingClientRect();
    var bottom = box.top + shiftedBy + (row + 1) * lineHeight();
    return Math.max(0, Math.round(bottom - sheet.getBoundingClientRect().top));
  }

  // ── what the drawer covers ────────────────────────────────────────────
  // It grows upward out of the pad, so what it covers is the bottom of the
  // terminal -- the prompt, the newest output, tmux's own status line. That
  // is the worst possible choice of rows to hide: you open the keys in order
  // to press one, and cannot see what pressing it did.
  //
  // The alternative is to make the terminal shorter, which resizes the pty
  // and makes tmux reflow and repaint everything -- the jolt this drawer was
  // made a sheet to avoid in the first place.
  //
  // So neither: the terminal keeps every row it has, and slides up by exactly
  // as much as the drawer covers. The rows that leave are the oldest ones,
  // off the top, and the prompt sits directly above the drawer where you can
  // watch it. A transform composites, so this costs no layout and no repaint
  // and follows the handle continuously while it is dragged.
  function shiftTerminal(px) {
    if (!host) return;
    shiftedBy = Math.max(0, Math.round(px));
    host.style.transform = shiftedBy ? 'translateY(' + -shiftedBy + 'px)' : '';
  }

  function sizeSheet(px) {
    if (!sheet) return;
    if (!expanded) {
      sheet.style.height = '';
      shiftTerminal(0);
      return;
    }
    // Opening is always the natural size, never a number kept from last
    // time. A remembered pixel height is a measurement of whatever the layout
    // was the day it was dragged: it survived a change of orientation, of
    // font, of how many keys the far end publishes, and of which phone it
    // was, and the symptom was a drawer opening at two rows over a screen
    // with room for ten. A height dragged smaller is still honoured -- it
    // just cannot outlive what the drawer is being asked to hold.
    var natural = sheetPeek();
    var want = px === undefined
      ? (sheetHeight ? Math.min(sheetHeight, natural) : natural)
      : px;
    sheetHeight = Math.max(sheetFloor(),
                           Math.min(sheetRoom(), Math.round(want)));
    sheet.style.bottom = sheetBase() + 'px';
    sheet.style.height = sheetHeight + 'px';
    // Whatever the drawer needs beyond the rows the terminal was not using is
    // taken by sliding the terminal rather than by shortening it -- so the
    // last written line stays visible and the pty is never resized.
    shiftTerminal(neededShift());
    markSheetEnd();
  }


  // The fade at the bottom edge says "there is more below". Once there is
  // not, it would be saying something false, so it comes off.
  function markSheetEnd() {
    if (!sheet) return;
    var end = sheet.scrollTop + sheet.clientHeight >= sheet.scrollHeight - 2;
    sheet.classList.toggle('atbottom', end);
  }
  if (sheet) sheet.addEventListener('scroll', markSheetEnd, { passive: true });
  // Two rows fit under a phone's thumb without eating the dashboard. The
  // rest are one tap away rather than one page away.
  var ROWS_COLLAPSED = 1;
  var current = [], expanded = false;

  // How many across is the screen's decision, not a constant -- the same rule
  // the font size follows, for the same reason. What should stay put is the
  // size of a key, not the number of them.
  //
  // One pitch for the whole panel, though, and only two values it may take.
  // The navigation row holds exactly ten keys and has to divide evenly, so
  // its only choices are two rows of five or one row of ten -- and whatever
  // it picks, the rows under it pick the same, because two grids at different
  // pitches sharing one panel is what stopped it reading as a single machined
  // face. It was 5 x 71px above 4 x 86px, and none of the seams lined up.
  //
  // Ten only when ten fit: at 390px that would be 71px keys in the nav row
  // and 33px ones would be a quarter under the minimum in the direction you
  // aim. At 844px landscape it is 84px, which is where the two rows of 162px
  // monsters used to be.
  var NAV_PER_ROW = 5, WIDE_PER_ROW = 10, WIDE_KEY_MIN = 62;
  function perRow(width) {
    if (!(width > 0)) return NAV_PER_ROW;
    return width / WIDE_PER_ROW >= WIDE_KEY_MIN ? WIDE_PER_ROW : NAV_PER_ROW;
  }

  function haptic() {
    // Android only; iOS Safari ignores it. Cheap when it works, harmless when
    // it does not.
    if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
  }

  function press(seq) {
    typed(seq);
    haptic();
  }

  // The single door every keystroke goes through, from the pad and from the
  // software keyboard alike. Typing while the pane is in copy-mode reaches
  // nothing -- measured -- so anything typed is taken as "I am done reading"
  // and leaves first. Without this the bar is the only way out, and a bar is
  // only a way out for someone who reads it.
  function typed(data) {
    if (scrolledBack) leaveHistory();
    sendText(applyLatch(data));
  }

  // Built once and then only when the width changes its mind. Two rows of
  // five on a phone -- ten across 390px is 33px wide, a quarter under the
  // minimum in the direction you actually aim -- and one row of ten wherever
  // ten fit, which is every landscape phone and every tablet. The split at
  // five falls where the row already divides: what modifies a key above,
  // what moves the cursor below.
  var navColumns = 0;
  function renderNav(columns) {
    if (!nav) return;
    if (navColumns === columns && nav.childElementCount) return;
    navColumns = columns;
    nav.textContent = '';
    modButtons = {};
    // Both clusters are three wide and two deep whatever the screen is, so
    // the shape holds when the phone turns -- which the old slicing did not:
    // at ten columns the whole thing became one row and the cross was gone
    // exactly when there was most room to draw it.
    var mods = document.createElement('div');
    mods.className = 'navmods';
    MOD_KEYS.forEach(function (entry) {
      mods.appendChild(buildKey(entry, true));
    });
    var cross = document.createElement('div');
    cross.className = 'dpad';
    DPAD_KEYS.forEach(function (entry) {
      if (!entry) {
        var hole = document.createElement('span');
        hole.className = 'key gap';
        cross.appendChild(hole);
        return;
      }
      cross.appendChild(buildKey(entry, true));
    });
    nav.appendChild(mods);
    nav.appendChild(cross);
    // The latch survives the rebuild; the buttons showing it do not, so it
    // has to be repainted onto the new ones or an armed ctrl goes invisible
    // and starts modifying keystrokes nobody asked it to.
    paintLatch();
  }

  // What the drawer holds when it is open. The app's own keys first and
  // unlabelled, because they are what you can do on the screen in front of
  // you; then the terminal's, which are true whatever is running.
  //
  // Sections, not tabs. Pages were tried and removed for a good reason -- the
  // layout key ended up two taps deep behind one nobody would think to open --
  // and a second axis of navigation would be that mistake again. Opening the
  // drawer reveals everything, in one column, scrolled.
  // Marked as ours, which is what makes a key pinnable: these mean the same
  // thing on every screen, so keeping one is keeping a key rather than
  // keeping a screenshot of one.
  function own(list) {
    return list.map(function (entry) {
      return { l: entry.l, k: entry.k, own: true };
    });
  }

  function groups() {
    var out = [];
    if (current.length) out.push({ name: '', keys: current });
    out.push({ name: 'tmux', keys: own(tmuxKeys()) });
    out.push({ name: 'ctrl', keys: own(CTRL_KEYS) });
    out.push({ name: 'move', keys: own(MOVE_KEYS) });
    out.push({ name: 'type', keys: own(TYPE_KEYS) });
    return out;
  }

  // Resolved fresh every render rather than stored: a pinned tmux verb is
  // rebuilt from whatever the prefix is now, and a label that no longer
  // names anything simply stops appearing instead of becoming a dead button.
  function pinnedKeys() {
    var all = [];
    groups().forEach(function (group) {
      if (group.name) all = all.concat(group.keys);
    });
    return pins.map(function (label) {
      for (var i = 0; i < all.length; i++) {
        if (all[i].l === label) return all[i];
      }
      return null;
    }).filter(Boolean);
  }

  function renderRows(into, list, columns, limit) {
    var rows = Math.ceil(list.length / columns);
    var take = limit ? Math.min(rows, limit) : rows;
    for (var r = 0; r < take; r++) {
      var line = document.createElement('div');
      line.className = 'krow';
      var slice = list.slice(r * columns, (r + 1) * columns);
      slice.forEach(function (entry) { line.appendChild(buildKey(entry)); });
      // A short last row would stretch its keys to full width and read as
      // something important rather than as the leftover it is.
      for (var i = slice.length; i < columns; i++) {
        var gap = document.createElement('span');
        gap.className = 'key gap';
        line.appendChild(gap);
      }
      into.appendChild(line);
    }
    return take * columns;                          // slots drawn, gaps and all
  }

  // What the last render decided, so a rotation can notice it has changed.
  var columnsUsed = 0;

  function recolumn() {
    if (perRow(keys.getBoundingClientRect().width) !== columnsUsed) {
      renderKeys();
    } else {
      // The width did not change the grid, but a rotation still changes how
      // much terminal there is for the sheet to cover.
      sizeSheet();
    }
  }

  // Yours, and therefore fixed: the whole point of keeping a key is that it
  // is in the same place every time, which a row inside the scrolling drawer
  // would not be.
  // The same label the drawer puts over its sections. These two rows were
  // pixel-for-pixel identical and meant entirely different things -- the keys
  // you kept, and what this screen can do -- so the pad read as an arbitrary
  // stack rather than as groups. Naming them in the drawer's own typography
  // is also what makes opening the drawer continue the panel instead of
  // starting a second one.
  function band(into, text) {
    var label = document.createElement('div');
    label.className = 'ghead';
    label.textContent = text;
    into.appendChild(label);
  }

  function renderPins(columns) {
    if (!pinRow) return;
    pinRow.textContent = '';
    var list = pinnedKeys();
    pinRow.style.display = list.length ? '' : 'none';
    if (!list.length) return;
    band(pinRow, 'kept');
    renderRows(pinRow, list, columns, 0);
  }

  function renderKeys() {
    // Measured rather than reasoned about. The pad grows when the pinned row
    // appears and when the width changes the column count, and does not when
    // the sheet opens or the app publishes a different set of keys -- and
    // getting that wrong in either direction is either a stale terminal or a
    // full tmux repaint. The box knows; ask it.
    var before = pad ? pad.getBoundingClientRect().height : 0;
    var columns = perRow(keys.getBoundingClientRect().width);
    columnsUsed = columns;
    renderNav(columns);
    renderPins(columns);

    // The row that is always there: what this screen can do, one row of it.
    // When nothing has published anything the screen belongs to tmux or a
    // shell, and the useful answer is tmux's own verbs -- detach first.
    // Showing nothing here would put the one key nobody can guess behind a
    // tap.
    keys.textContent = '';
    keys.classList.toggle('editing', editing);
    var head = current.length ? current : tmuxKeys();
    // Always `screen`, never the name of what happens to be in it. Labelling
    // it `tmux` when nothing had published put the word TMUX over this row and
    // over the drawer's first section at the same time, naming two different
    // things -- and the drawer's section held the very same five keys. What
    // this row means is "what the screen in front of you can do", which stays
    // true whether that screen belongs to the dashboard, to tmux or to a
    // shell.
    band(keys, 'screen');
    var drawn = Math.min(renderRows(keys, head, columns, ROWS_COLLAPSED),
                         head.length);
    var total = groups().reduce(function (n, group) {
      return n + group.keys.length;
    }, 0);

    // The sheet, which covers the terminal rather than displacing it.
    if (sheet) {
      sheet.classList.toggle('open', expanded);
      sheet.classList.toggle('editing', editing);
      sheetKeys.textContent = '';
      var placed = false;
      if (editButton) editButton.hidden = true;
      if (expanded) {
        groups().forEach(function (group) {
          if (!group.keys.length) return;
          // Each section in its own box. A sticky heading is held by its
          // parent's edges, so headings that are all siblings of one another
          // pin to the same spot and stack there -- which happens to paint
          // the right one on top, because the current section is the last one
          // in the document, and is wrong the moment anything about that
          // changes. Boxed, each heading is pushed out by the next one
          // arriving underneath it, which is also what makes the change of
          // section read as a change rather than as a flicker.
          var section = document.createElement('div');
          section.className = 'gsec';
          if (group.name) band(section, group.name);
          // The keep control rides the first heading. On a row of its own it
          // was 38px of every open -- most of a key row -- for something used
          // once when the pad is set up, and the heading was already there
          // with a hairline running to the right edge doing nothing.
          if (editButton && !placed && section.children.length) {
            section.children[0].appendChild(editButton);
            editButton.hidden = false;
            placed = true;
          }
          renderRows(section, group.keys, columns, 0);
          sheetKeys.appendChild(section);
        });
        sizeSheet();
        markSheetEnd();
      } else {
        sizeSheet();
      }
    }

    if (expander) {
      // Always offered. There is always more than fits: the terminal's own
      // vocabulary does not depend on anything having been published, and a
      // hidden expander is what left a bare shell with three keys.
      //
      // The count says what it counts. `⌄ 38` was a glyph and a bare number,
      // and a number with no noun beside it is something you have to be told.
      var more = Math.max(total - drawn, 0);
      if (grabCount) {
        grabCount.textContent = expanded ? 'close' : more + ' more keys';
      }
      expander.classList.toggle('open', expanded);
      expander.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      expander.setAttribute(
        'aria-label', expanded ? 'fewer keys' : more + ' more keys');
    }
    // Opening the sheet leaves this unchanged, which is the entire point of
    // it being a sheet: refit() resizes the pty, and tmux answers a resize by
    // reflowing and repainting the whole screen -- twice per toggle, when
    // nothing behind the keys had moved.
    var after = pad ? pad.getBoundingClientRect().height : 0;
    if (Math.abs(after - before) > 0.5) refit();
  }

  function setKeys(list) {
    var same = list.length === current.length && list.every(function (e, i) {
      return e.k === current[i].k && e.l === current[i].l;
    });
    if (same) return;
    current = list;
    renderKeys();
  }

  // ── scrolling the drawer ──────────────────────────────────────────────
  // This file does it, on every browser, the same way.
  //
  // It was briefly handed to the browser instead: `touch-action: pan-y` on a
  // key, on the reasoning that preventDefault stops the focus while
  // touch-action alone decides the pan, so the two never conflicted and a
  // hundred lines here were working around nothing. That reasoning is
  // correct, and it works in Chromium -- measured through the real gesture
  // pipeline: a 120px drag scrolled 159px, a flick reached the end, a tap
  // still sent its key.
  //
  // It was still the wrong call. Chromium is not the target. On the phone
  // this is for, the moment the browser claims the pan it decides on its own
  // whether a pointercancel follows -- and if it does not, the key sees a
  // clean pointerdown/pointerup and types whatever you dragged across. That
  // is a control character into a live shell, and it cannot be checked from
  // here.
  //
  // So the whole drawer is ours (#sheet is touch-action: none, not just the
  // keys), which is deterministic everywhere and testable here. What that
  // costs is momentum, and momentum is cheap to write.
  var glideTimer = 0, glideAt = 0, glideV = 0, glideSamples = [];
  function clock() { return Date.now(); }
  function frame(fn) {
    return typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame(fn) : setTimeout(fn, 16);
  }
  function stopGlide() {
    if (!glideTimer) return;
    if (typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(glideTimer);
    } else {
      clearTimeout(glideTimer);
    }
    glideTimer = 0; glideV = 0;
  }

  // One scrolling surface, one thing that scrolls it. This used to hang off
  // each key, which is why the drawer had holes in it: with the browser told
  // not to pan (touch-action: none) and a handler only on the keys, every
  // part of the box that is not a key -- the gaps between them, the row
  // margins, the section headings, the toolbar, the padding -- was a place
  // where a drag did nothing at all. Measured: a finger landing on a key
  // scrolled 169px, a finger landing 55px lower scrolled 0.
  //
  // The listener belongs on the box that scrolls. Events from the keys bubble
  // to it, so a drag behaves the same wherever it starts.
  function dragSheet(dy) {
    if (!sheet || !sheet.classList.contains('open')) return;
    sheet.scrollTop += dy;
    glideSamples.push({ at: clock(), dy: dy });
    // Bounded, so a long drag does not grow an array all the way down. The
    // window that actually decides the throw is applied at release.
    if (glideSamples.length > 40) glideSamples.shift();
    markSheetEnd();
  }

  // Only the last breath of the gesture counts, and the trim happens at
  // release rather than while sampling: a drag that spends a second creeping
  // and then flicks is a flick, and averaging the whole thing turns it back
  // into a creep.
  var GLIDE_WINDOW = 90;                 // ms
  // px per millisecond below which a release is a stop, not a throw.
  var GLIDE_MIN = 0.22;
  // What the speed is multiplied by every millisecond. 0.995 lands a hard
  // flick in a little under a second, which is about what the platform does.
  var GLIDE_DECAY = 0.995;

  function releaseDrawer() {
    var samples = glideSamples;
    glideSamples = [];
    if (!sheet || samples.length < 2) return;
    var last = samples[samples.length - 1].at;
    while (samples.length > 2 && last - samples[0].at > GLIDE_WINDOW) {
      samples.shift();
    }
    var span = samples[samples.length - 1].at - samples[0].at;
    if (span <= 0) return;
    var moved = 0;
    for (var i = 1; i < samples.length; i++) moved += samples[i].dy;
    var speed = moved / span;
    if (Math.abs(speed) < GLIDE_MIN) return;
    glideV = speed;
    glideAt = clock();
    glideTimer = frame(glideStep);
  }

  // How far a finger travels before the drawer treats it as a scroll rather
  // than a press. Same number the keys use, so the two agree about what just
  // happened.
  var SHEET_SLOP = 10;
  // True while a drag is in progress, so the controls inside the drawer can
  // tell a tap from having been scrolled past.
  var sheetDragged = false;

  if (sheet) (function () {
    var live = false, startY = 0, lastY = 0, moving = false;
    sheet.addEventListener('pointerdown', function (event) {
      stopGlide();                     // a finger down stops a glide, always
      live = true; moving = false; sheetDragged = false;
      startY = lastY = event.clientY;
      glideSamples = [];
    });
    sheet.addEventListener('pointermove', function (event) {
      if (!live) return;
      if (!moving) {
        if (Math.abs(event.clientY - startY) < SHEET_SLOP) return;
        moving = true; sheetDragged = true;
      }
      dragSheet(lastY - event.clientY);
      lastY = event.clientY;
    });
    sheet.addEventListener('pointerup', function () {
      if (live && moving) releaseDrawer();
      live = false;
    });
    sheet.addEventListener('pointercancel', function () {
      live = false; moving = false;
    });
  })();

  function glideStep() {
    if (!sheet || !sheet.classList.contains('open')) { stopGlide(); return; }
    var at = clock();
    // Clamped: a backgrounded tab resumes with a huge delta, and one frame
    // worth thirty would jump the list to an end nobody threw it at.
    var dt = Math.min(32, Math.max(1, at - glideAt));
    glideAt = at;
    var before = sheet.scrollTop;
    sheet.scrollTop = before + glideV * dt;
    markSheetEnd();
    // Hit the top or the bottom: nowhere left to go, so stop rather than spin
    // down against the edge.
    if (Math.abs(sheet.scrollTop - before) < 0.5) { stopGlide(); return; }
    glideV *= Math.pow(GLIDE_DECAY, dt);
    if (Math.abs(glideV) < 0.02) { stopGlide(); return; }
    glideTimer = frame(glideStep);
  }

  function buildModifier(entry) {
    var button = document.createElement('button');
    button.className = 'key mod';
    button.textContent = entry.l;
    button.setAttribute('aria-label', entry.l);
    modButtons[entry.m] = button;
    button.addEventListener('pointerdown', function (event) {
      event.preventDefault();
      latch[entry.m] = (latch[entry.m] + 1) % 3;
      paintLatch();
      haptic();
    });
    button.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    return button;
  }

  // `instant` marks a key in a row that cannot scroll -- the fixed rows. There
  // waiting for the finger to lift is pure latency, and a real key acts on the
  // way down. The drawer keeps release, because a drag through it is how it
  // scrolls and typing what you dragged past would be worse than the wait.
  function buildKey(entry, instant) {
    if (entry.m) return buildModifier(entry);   // already on the way down
    var label = entry.l, seq = entry.k;
    var button = document.createElement('button');
    button.className = 'key';
    // Derived rather than flagged, same as `repeats` below: a label that is a
    // single symbol is a glyph and wants the room a word does not.
    if (label.length === 1 && !/\w/.test(label)) button.classList.add('glyph');
    if (entry.tone) button.classList.add(entry.tone);
    // While editing, a key is a thing you are choosing rather than pressing,
    // and it has to look like one -- a row that types when you meant to keep
    // is worse than no editing at all.
    //
    // Nothing at all fires while editing, not just the keys you can keep. The
    // app's own are in this drawer too, they are not keepable, and one of them
    // is `Kill session`: a tap that fell through to it because it happened not
    // to be pinnable would be the worst bug in this file.
    var pinnable = entry.own === true;
    if (editing) {
      button.classList.add(pinnable ? 'choose' : 'locked');
      if (pinnable && pins.indexOf(label) >= 0) button.classList.add('kept');
    }
    button.textContent = label;
    button.setAttribute('aria-label', label);
    var timer = null, interval = null;
    var live = false, repeated = false, dragging = false;
    var originX = 0, originY = 0;
    // Derived, not flagged: the keys worth repeating are the ones that move or
    // remove something a step at a time -- the CSI sequences (arrows, page
    // up/down, delete) and backspace. Nothing has to remember to mark them.
    var repeats = /^\x1b\[/.test(seq) || seq === '\x7f';
    var DRAG = 10;                        // px before a touch is a scroll
    function fire() { press(seq); }
    function stop() {
      live = false;
      button.classList.remove('down');
      clearTimeout(timer); clearInterval(interval);
      timer = interval = null;
    }
    function down(event) {
      // This is what stops the button taking the terminal's focus, and focus
      // is what the software keyboard is attached to. It does not stop the
      // drawer panning: a pan is touch-action's decision and nothing else's,
      // which is why the key can refuse focus and still be scrolled through.
      event.preventDefault();
      live = true; repeated = false; dragging = false;
      originX = event.clientX; originY = event.clientY;
      button.classList.add('down');
      // Editing is choosing, not pressing: a fixed key that typed on the way
      // down while you were picking what to keep would be the same bug the
      // whole editing mode exists to avoid.
      if (instant && !editing) { fire(); repeated = true; }
      // Nothing repeats while you are choosing. A held key that pinned and
      // unpinned itself ten times a second is the obvious way to get this
      // wrong.
      if (repeats && !editing) {
        timer = setTimeout(function () {
          if (!instant) { repeated = true; fire(); }
          interval = setInterval(fire, 70);
        }, 400);
      }
    }
    // On release rather than on touch. Firing on the way down is what a key
    // should do -- and it is also what would type something every time you
    // dragged through the drawer looking for a different key. A finger that
    // has not moved has still not scrolled anything, so a tap is a tap.
    function up(event) {
      // Two ways to know this was not a press, and both are needed. `dragging`
      // is set by pointermove, which is the normal route. The coordinates on
      // the release are the backstop: a browser that claims a gesture may
      // stop delivering moves and still deliver the up, and a key that typed
      // because of that would be sending a control character into a live
      // shell for the crime of being scrolled past.
      var far = event && (Math.abs(event.clientX - originX) > DRAG
                          || Math.abs(event.clientY - originY) > DRAG);
      if (live && !repeated && !dragging && !far) {
        if (!editing) fire();
        else if (pinnable) togglePin(label);
      }
      stop();
    }
    // Past a few pixels this is a scroll, not a key. The drawer scrolls
    // itself -- the listener is on the box, and this event is on its way
    // there -- so all this has to do is stop the key thinking it was pressed.
    function moved(event) {
      if (!live || dragging) return;
      if (Math.abs(event.clientX - originX) > DRAG
          || Math.abs(event.clientY - originY) > DRAG) {
        dragging = true;
        button.classList.remove('down');
        clearTimeout(timer); clearInterval(interval);
        timer = interval = null;
      }
    }
    button.addEventListener('pointerdown', down);
    button.addEventListener('pointermove', moved);
    button.addEventListener('pointerup', up);
    button.addEventListener('pointercancel', stop);
    button.addEventListener('pointerleave', stop);
    // Stop the browser turning a held key into a text selection or a context
    // menu.
    button.addEventListener('contextmenu', function (e) {
      e.preventDefault();
    });
    return button;
  }

  var editButton = document.getElementById('edit');

  // Editing lives and dies with the open drawer. It is a mode in which keys
  // do not do what they say, so it must not be possible to be in it while
  // looking at a screen that gives no sign of it -- a lit star in the corner
  // is not enough to explain why `detach` just unkept itself instead.
  function setEditing(on) {
    editing = !!on;
    if (editing) expanded = true;
    if (editButton) editButton.classList.toggle('on', editing);
  }

  function toggleDrawer() {
    expanded = !expanded;
    if (!expanded && editing) setEditing(false);
    renderKeys();
    sizeSheet();
  }

  // ── the handle ────────────────────────────────────────────────────────
  // Tap toggles, drag resizes. One control for both because they are the same
  // question asked at different resolutions -- "show me more keys" and "show
  // me this many more keys" -- and because a drawer that can only be all or
  // nothing is what left seven rows of terminal on screen.
  //
  // Not pointerdown-to-fire like the fixed rows: this one has to know whether
  // the finger moved before it can know what was meant.
  var GRAB_SLOP = 6;
  keepFocus(expander);
  if (expander) (function () {
    var from = 0, startedAt = 0, moving = false, live = false;

    function down(event) {
      event.preventDefault();
      live = true; moving = false;
      startedAt = event.clientY;
      // Nothing is changed yet, on purpose: a tap and a drag start
      // identically, and opening here would make the tap path start from the
      // drag's own floor rather than from the height a tap should give.
      from = expanded ? (sheetHeight || sheetPeek()) : 0;
    }

    function moved(event) {
      if (!live) return;
      var dy = startedAt - event.clientY;        // up is taller
      if (!moving) {
        if (Math.abs(dy) < GRAB_SLOP) return;
        moving = true;
        expander.classList.add('dragging');
        // Dragging up out of a closed pad opens it and grows from nothing, so
        // the sheet comes out from under the finger.
        if (!expanded) { expanded = true; renderKeys(); }
      }
      sizeSheet(from + dy);
    }

    function up(event) {
      if (!live) return;
      live = false;
      expander.classList.remove('dragging');
      if (!moving) { toggleDrawer(); haptic(); return; }
      // Dragged shut: below the smallest useful drawer, the intent is closed
      // rather than tiny.
      if (sheetHeight <= sheetFloor() && startedAt < event.clientY) {
        // Restored, not kept: the floor is where the gesture passed through
        // on its way to closed, not a height anyone chose. Reopening should
        // give back the one they did choose.
        sheetHeight = from;
        expanded = false;
        if (editing) setEditing(false);
        renderKeys();
        sizeSheet();
      }
    }

    function cancel() {
      if (!live) return;
      live = false; moving = false;
      expander.classList.remove('dragging');
    }

    expander.addEventListener('pointerdown', down);
    expander.addEventListener('pointermove', moved);
    expander.addEventListener('pointerup', up);
    expander.addEventListener('pointercancel', cancel);
    expander.addEventListener('pointerleave', cancel);
    expander.addEventListener('contextmenu', function (e) {
      e.preventDefault();
    });
  })();

  keepFocus(editButton);
  if (editButton) editButton.addEventListener('click', function (event) {
    event.preventDefault();
    // It sits inside the drawer, so a drag that began on it scrolled the
    // drawer -- and the click that follows must not also change a mode.
    if (sheetDragged) return;
    setEditing(!editing);
    say(editing ? 'tap keys to keep them' : 'kept');
    renderKeys();
  });

  // ── the way out ───────────────────────────────────────────────────────
  // `prefix d`, which is what the drawer's `detach` key sends. Detaching ends
  // the pty, the socket closes with reason "exit", and onclose navigates back
  // to the list -- so this button does not have to know where the list is.
  //
  // It exists because there was no visible way back at all: `detach` is
  // seventh into the sheet under `tmux`, and this page is built to be
  // installed to the home screen, where there is no browser chrome to fall
  // back on.
  // Two elements, one meaning, and never both on screen: `#back` is in the
  // settings row inside the pad, `#exit` is on the strip that is all there
  // is when the pad is hidden. One handler, so they cannot drift into doing
  // different things -- which is what "one affordance" is actually about.
  ['back', 'exit'].forEach(function (id) {
    var button = document.getElementById(id);
    if (!button) return;
    keepFocus(button);
    button.addEventListener('click', function (event) {
      event.preventDefault();
      // Through typed(), so that reading history is left first: sending the
      // prefix into a pane still in copy-mode reaches nothing at all.
      typed(prefixSeq + 'd');
      haptic();
    });
  });

  // ── getting out of the way ────────────────────────────────────────────
  // The pad is two hundred pixels of a phone screen, and there are stretches
  // -- reading a log, watching a job -- where you want none of it. Hidden
  // means hidden: not a smaller pad, no row of controls left behind. What
  // stays is a strip at the very bottom edge, which is a large target for a
  // thumb however few pixels tall it is.
  var PAD_STATE = 'atmux.pad';
  var grip = document.getElementById('grip');
  var hideButton = document.getElementById('hide');

  function setPad(shown) {
    document.body.classList.toggle('nopad', !shown);
    // Putting the pad away ends editing with it, and closes the sheet. Coming
    // back to a pad whose keys silently keep instead of press, because of
    // something you did before you hid it, is the same trap the expander
    // closes -- and a sheet that reappears over the terminal because it was
    // open when you hid the pad is that trap with a bigger footprint.
    if (!shown && (editing || expanded)) {
      setEditing(false);
      expanded = false;
      renderKeys();
    }
    try {
      localStorage.setItem(PAD_STATE, shown ? 'shown' : 'hidden');
    } catch (e) {}
    refit();
  }
  keepFocus(hideButton); keepFocus(grip);
  if (hideButton) hideButton.addEventListener('click', function (event) {
    event.preventDefault();
    setPad(false);
  });
  if (grip) grip.addEventListener('click', function (event) {
    event.preventDefault();
    setPad(true);
  });

  // The dashboard's side of this is keypad.encode(). Everything is validated
  // before it reaches a button: this arrives over the same pty as the screen
  // contents, so a program that is not atmux could emit anything at all, and
  // a button whose label does not match what it types is worse than no
  // button. Returning true tells xterm the sequence was handled.
  term.parser.registerOscHandler(KEYS_OSC, function (payload) {
    var data;
    try { data = JSON.parse(payload); } catch (e) { return true; }
    if (!data || !Array.isArray(data.keys)) return true;
    // The prefix byte, when the app has an opinion about it. Validated like
    // everything else here, and bounded to what a prefix can be: one byte, or
    // two for an M- binding. A long string here would build twelve buttons
    // that each type a sentence.
    if (typeof data.prefix === 'string' && data.prefix.length >= 1
        && data.prefix.length <= 2 && data.prefix !== prefixSeq) {
      prefixSeq = data.prefix;
      renderKeys();
    }
    // Who is on the other end. Not used for drawing -- used for deciding what
    // a swipe means, because the dashboard and tmux want different keys and
    // sending one the other's is worse than sending nothing. See swipeKind.
    if (data.mode === 'app' || data.mode === 'external') published = data.mode;
    // The app is drawing again: whatever pane we put into copy-mode is not
    // the screen any more, so the bar would be pointing at nothing.
    if (data.mode === 'app' && scrolledBack) {
      scrolledBack = false;
      paintHistory('');
    }
    setKeys(data.keys.filter(function (entry) {
      return entry && typeof entry.k === 'string' && entry.k
          && typeof entry.l === 'string' && entry.l
          && entry.k.length <= 8 && entry.l.length <= 24;
    }).slice(0, 24));
    return true;
  });

  // ── font size ─────────────────────────────────────────────────────────
  // An override, not the mechanism. Auto is the default and the thing to
  // return to; these exist for reading one cramped detail, and for the screen
  // the rules get wrong.
  function remember(value) {
    try {
      if (value === null) localStorage.removeItem('atmux.fontSize');
      else localStorage.setItem('atmux.fontSize', String(value));
    } catch (e) {}
  }
  function setFont(size) {
    size = Math.max(MIN_FONT, Math.min(MAX_FONT, Math.round(size * 2) / 2));
    if (manualFont === size) return;
    manualFont = size;
    remember(size);
    term.options.fontSize = size;
    markAuto();
    announce = true;
    refit();
  }
  function setAutoFont() {
    manualFont = null;
    remember(null);
    markAuto();
    announce = true;
    refit();
  }
  var minus = document.getElementById('fontminus');
  var plus = document.getElementById('fontplus');
  var autoButton = document.getElementById('fontauto');
  function markAuto() {
    if (autoButton) autoButton.classList.toggle('on', manualFont === null);
  }
  keepFocus(minus); keepFocus(plus); keepFocus(autoButton);
  if (minus) minus.addEventListener('click', function (e) {
    e.preventDefault(); setFont(term.options.fontSize - 1);
  });
  if (plus) plus.addEventListener('click', function (e) {
    e.preventDefault(); setFont(term.options.fontSize + 1);
  });
  if (autoButton) autoButton.addEventListener('click', function (e) {
    e.preventDefault(); setAutoFont();
  });
  markAuto();
  // ── selecting text ────────────────────────────────────────────────────
  // A drag over the terminal can mean read the scrollback or it can mean
  // select this text, and it cannot mean both. Which one is a mode rather
  // than something inferred from how long a finger rested: guessing costs a
  // page of scrollback every time it guesses wrong, and there is no way to
  // tell the reader what it guessed.
  //
  // Nothing here is clever. The two rules that stop a selection are xterm's
  // own `user-select: none` and this page's `touch-action: none`, both of
  // them load-bearing the rest of the time, and this turns off exactly those
  // two and stands the terminal's own handlers down while it does.
  var selecting = false;
  var selButton = document.getElementById('sel');
  var selView = document.getElementById('selview');
  var selText = document.getElementById('seltext');
  var selHint = document.getElementById('selhint');
  var selCopy = document.getElementById('selcopy');

  // Everything the terminal is holding, scrollback first. buffer.active runs
  // from the oldest retained line to the bottom of the screen, so this is
  // strictly more than the terminal can show -- and being able to select a
  // line that has already scrolled past is the part people actually want.
  function bufferLines() {
    var buf = term.buffer.active;
    var out = [];
    for (var i = 0; i < buf.length; i++) {
      var line = buf.getLine(i);
      out.push(line ? line.translateToString(true) : '');
    }
    // The unwritten tail of the screen is not content; it is just the part
    // of the grid nothing has been printed on.
    while (out.length && !out[out.length - 1]) out.pop();
    return out;
  }

  // ── picking lines ──────────────────────────────────────────────────────
  // Long press is the gesture everyone reaches for, and twice now it has not
  // worked on the device that matters while working in every measurement I
  // could take. A harness that cannot reproduce a failure cannot confirm a
  // fix either, so this stops being the only way in.
  //
  // Tapping a line picks it. That is an ordinary click on an ordinary
  // element -- no gesture recognition, nothing for a platform to disagree
  // about -- and `copy` writes the picked lines with the clipboard API
  // rather than waiting for the system menu to be offered. A line is also
  // the unit people actually copy out of a terminal: a path, a job id, a
  // command to run again.
  //
  // The native long press still works on top of this, and when it has
  // produced a selection that selection wins: someone who went to the
  // trouble of dragging out half a line means that half line.
  function pickedText() {
    try {
      var native = window.getSelection && window.getSelection().toString();
      if (native && native.trim()) return native;
    } catch (e) {}
    if (!selText) return '';
    var out = [];
    var rows = selText.querySelectorAll('.sline.picked');
    for (var i = 0; i < rows.length; i++) out.push(rows[i].textContent);
    return out.join('\n');
  }

  // Clearing the picks after a copy drops the selection, which fires
  // selectionchange, which repaints the bar -- so the word "copied" was
  // written and then overwritten within the same tick, and the one action
  // with nothing on screen to confirm it was the one whose whole result is
  // invisible. It outranks the count until it has been read.
  var copiedUntil = 0;
  function paintPicked() {
    if (!selHint) return;
    if (clock() < copiedUntil) return;
    var picked = selText ? selText.querySelectorAll('.sline.picked').length : 0;
    var native = '';
    try {
      native = (window.getSelection && window.getSelection().toString()) || '';
    } catch (e) {}
    if (native.trim()) {
      // Says what to do with it, not that it exists -- the highlight already
      // says that, and the handles are the part nobody expects to be there.
      selHint.textContent = 'drag the handles · then copy';
    } else if (picked) {
      selHint.textContent = picked === 1 ? '1 line' : picked + ' lines';
    } else {
      selHint.textContent = 'tap lines to copy';
    }
    if (selCopy) selCopy.disabled = !picked && !native.trim();
  }

  function renderPickable(lines) {
    selText.textContent = '';
    // The terminal's own row height, so this layer lands line for line on
    // what it is standing in front of.
    var unit = lineHeight();
    var frag = document.createDocumentFragment();
    for (var i = 0; i < lines.length; i++) {
      var row = document.createElement('div');
      row.className = 'sline';
      row.style.height = unit + 'px';
      row.style.lineHeight = unit + 'px';
      // A blank line still has to be tappable, or the gaps in a log are dead
      // spots. A non-breaking space keeps the box without adding a character
      // to what gets copied -- textContent would.
      row.textContent = lines[i];
      if (!lines[i]) row.innerHTML = '&nbsp;';
      frag.appendChild(row);
    }
    selText.appendChild(frag);
  }

  // Put the line that was pressed where it already was on screen, and select
  // it. iOS draws its handles on the selection it finds, so what a press
  // lands you in is the same text, in the same place, already selected --
  // which is what the system does natively, and is as close to it as a page
  // that cannot use the system's own selection can get.
  function revealLine(index, screenY) {
    var rows = selText.querySelectorAll('.sline');
    var row = rows[index];
    if (!row) return;
    // Same position on screen as the row it is replacing, so nothing appears
    // to jump at the moment of the press.
    var frame = selText.getBoundingClientRect();
    selText.scrollTop = row.offsetTop - (screenY - frame.top);
    try {
      var range = document.createRange();
      range.selectNodeContents(row);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) {}
  }

  // A tap, not the end of a drag: dragging across these lines is how the
  // native selection is made, and toggling a line under it would fight the
  // very gesture this exists to back up.
  var pickFrom = null;
  if (selText) {
    selText.addEventListener('pointerdown', function (event) {
      pickFrom = {x: event.clientX, y: event.clientY};
    });
    selText.addEventListener('pointerup', function (event) {
      var row = event.target.closest && event.target.closest('.sline');
      var from = pickFrom;
      pickFrom = null;
      if (!row || !from) return;
      if (Math.abs(event.clientX - from.x) > 8 ||
          Math.abs(event.clientY - from.y) > 8) return;
      // A tap after a native selection means "start over with lines", not
      // "add a line to the text I already dragged out".
      try {
        var sel = window.getSelection && window.getSelection();
        if (sel && sel.toString().trim()) sel.removeAllRanges();
      } catch (e) {}
      row.classList.toggle('picked');
      paintPicked();
    });
    // The count has to follow a native selection too, or the button says
    // nothing to copy while the screen is showing a highlight.
    document.addEventListener('selectionchange', function () {
      if (selecting) paintPicked();
    });
  }

  function copyPicked() {
    var text = pickedText();
    if (!text) return;
    var done = function () {
      var lines = text.split('\n').length;
      if (selHint) {
        selHint.textContent = lines > 1 ? 'copied ' + lines + ' lines'
                                        : 'copied';
      }
      copiedUntil = clock() + 1600;
      setTimeout(function () { copiedUntil = 0; paintPicked(); }, 1600);
      // The picks have served their purpose; leaving them lit invites a
      // second copy of the same thing.
      if (selText) {
        var lit = selText.querySelectorAll('.sline.picked');
        for (var i = 0; i < lit.length; i++) lit[i].classList.remove('picked');
      }
      try {
        var sel = window.getSelection && window.getSelection();
        if (sel) sel.removeAllRanges();
      } catch (e) {}
      if (selCopy) selCopy.disabled = true;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        if (!legacyCopy(text)) {
          if (selHint) selHint.textContent = 'the browser refused the clipboard';
          return;
        }
        done();
      });
      return;
    }
    if (legacyCopy(text)) done();
    else if (selHint) selHint.textContent = 'this browser has no clipboard';
  }

  // execCommand is deprecated and still the only thing that works without a
  // secure context or a permission -- which is what an http:// tailnet
  // address or an older iOS falls back to.
  function legacyCopy(text) {
    var box = null;
    try {
      box = document.createElement('textarea');
      box.value = text;
      // Not readonly, and selected through a Range rather than .select().
      // iOS refuses to put a caret in a readonly field, so the usual recipe
      // -- readonly + select() -- selects nothing there and copies nothing,
      // silently. contentEditable is what makes it selectable; the Range is
      // what actually selects it; setSelectionRange is what iOS honours.
      box.contentEditable = 'true';
      box.readOnly = false;
      box.style.position = 'fixed';
      box.style.top = '0';
      box.style.left = '0';
      box.style.opacity = '0';
      // Under 16px iOS zooms the page to meet a focused field, which on a
      // terminal is a visible lurch for a field nobody can see.
      box.style.fontSize = '16px';
      document.body.appendChild(box);
      var range = document.createRange();
      range.selectNodeContents(box);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      box.setSelectionRange(0, text.length);
      var ok = document.execCommand('copy');
      sel.removeAllRanges();
      return ok;
    } catch (e) {
      return false;
    } finally {
      if (box && box.parentNode) box.parentNode.removeChild(box);
    }
  }

  // `at` is set when a press put us here: {index, screenY} of the line that
  // was under the finger.
  function setSelecting(on, at) {
    selecting = !!on;
    document.body.classList.toggle('selecting', selecting);
    if (selButton) selButton.classList.toggle('on', selecting);
    if (!selView) return;
    if (selecting) {
      // Read the screen FIRST, and do not leave the history to do it.
      //
      // This used to call leaveHistory() here, from when selecting was a
      // mode you entered from a button and the worry was being left on a
      // screen that cannot be typed into. Under a press it is exactly
      // backwards: scrolling up to find the thing you want to copy is the
      // whole workflow, and leaving the history first sent tmux back to the
      // live screen and took the snapshot mid-repaint. Measured: pressing a
      // line reached by scrolling back opened a copy view with no lines in
      // it at all, because every row was momentarily blank and the trailing
      // blanks are trimmed.
      //
      // Where the reader is is where the snapshot comes from. The history
      // bar is still up underneath, and still the way back to live.
      var lines = bufferLines();
      if (!lines.length) {
        // Nothing to show is not a view worth opening.
        selecting = false;
        document.body.classList.remove('selecting');
        if (selButton) selButton.classList.remove('on');
        say('nothing on screen to copy');
        return;
      }
      renderPickable(lines);
      // Same size as the terminal it is standing in for, so the columns line
      // up and a selection looks like the thing being selected.
      selText.style.fontSize = term.options.fontSize + 'px';
      selView.hidden = false;
      // The caret must be nowhere -- xterm parks a focused textarea, and a
      // focused editable is what a long press reaches for before it reaches
      // for the text under the finger.
      //
      // Except while the software keyboard is up, where blurring dismisses
      // it: that changes visualViewport, which resizes #app, which moves the
      // line out from under the finger that just pressed it. The view is
      // stacked above the field instead, so there is nothing to dodge.
      if (!kbdOpen) {
        try {
          var box = host.querySelector('textarea');
          if (box) box.blur();
        } catch (e) {}
      }
      // Where you were: the line you pressed, in the place you pressed it,
      // or the live end when nothing in particular was asked for.
      if (at) {
        revealLine(at.index, at.screenY);
      } else {
        selText.scrollTop = selText.scrollHeight;
      }
      // Once, and after the selection exists: painted before it, this says
      // there is nothing to copy and then has to be corrected.
      paintPicked();
    } else {
      selView.hidden = true;
      try {
        var sel = window.getSelection && window.getSelection();
        if (sel) sel.removeAllRanges();
      } catch (e) {}
      say('');
      term.focus();
    }
  }

  // Paste, which the terminal could never offer at all: there is nothing on
  // this page a long press can paste *into*, because the only editable is
  // offscreen and one cell wide. Reading the clipboard is the only way in,
  // and it is a deliberate act -- Safari puts its own confirmation in front
  // of it, which is the right amount of friction for something that types
  // into a shell.
  function pasteFromClipboard() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      if (selHint) selHint.textContent = 'this browser will not hand over '
                                       + 'the clipboard';
      return;
    }
    navigator.clipboard.readText().then(function (text) {
      if (!text) {
        if (selHint) selHint.textContent = 'the clipboard is empty';
        return;
      }
      setSelecting(false);
      sendText(text);
    }).catch(function () {
      if (selHint) selHint.textContent = 'paste was refused — tap paste '
                                       + 'again and allow it';
    });
  }

  var selPaste = document.getElementById('selpaste');
  var selDone = document.getElementById('seldone');
  if (selCopy) selCopy.addEventListener('click', function (event) {
    event.preventDefault();
    copyPicked();
  });
  if (selPaste) selPaste.addEventListener('click', function (event) {
    event.preventDefault();
    pasteFromClipboard();
  });
  if (selDone) selDone.addEventListener('click', function (event) {
    event.preventDefault();
    setSelecting(false);
  });
  keepFocus(selButton);
  if (selButton) selButton.addEventListener('click', function (event) {
    event.preventDefault();
    setSelecting(!selecting);
  });

  var kbd = document.getElementById('kbd');
  keepFocus(kbd);
  if (kbd) kbd.addEventListener('click', function (e) {
    e.preventDefault();
    setKeyboard(!kbdOpen);
  });

  // Leaving typing mode when the keyboard goes away on its own keeps the two
  // in step: otherwise the invisible textarea stays over the terminal and
  // swallows the taps that should be attaching to a session.
  if (textarea) {
    textarea.addEventListener('blur', function () {
      if (kbdOpen) setTimeout(function () {
        if (kbdOpen && document.activeElement !== textarea) setKeyboard(false);
      }, 250);
    });
  }

  // ── swiping the terminal ──────────────────────────────────────────────
  // Three different things own the scrollback, so one gesture has to mean
  // three different things -- and sending the wrong one is much worse than
  // sending nothing. Measured, all three:
  //
  //   normal buffer   xterm holds it. Shift+PageUp moved the top row from
  //                   245 to 77, so it is still rendered in xterm 6 even
  //                   though the scrollable <div> is gone.
  //   tmux            xterm holds *nothing* -- the alternate buffer is
  //                   repainted whole every frame. tmux holds it, in
  //                   history-limit, and `prefix PageUp` is tmux's own
  //                   default binding for reaching it (copy-mode -u). With
  //                   mouse off, 400 lines of history: 346 -> 292 [54/346]
  //                   -> 238 [108/346], PageDown back, q returns to live.
  //   the dashboard   Textual, not tmux. PageUp/PageDown scroll it; \x02
  //                   would mean something else entirely.
  //
  // The tempting fourth option is a wheel event, and it is a trap. In the
  // alternate buffer with mouse reporting off -- which is tmux's default and
  // this user's setting -- xterm translates a wheel into ARROW KEYS: measured
  // \x1bOA / \x1bOB going out and nothing scrolling. That walks the shell's
  // command history, one Enter away from re-running something.
  // Which programs want the mouse for themselves. There is no public API for
  // this in xterm 6 -- term.modes was removed and reading it throws -- so the
  // sequence that turns it on is watched directly. 1000 is click tracking,
  // 1002 adds drag, 1003 any motion; 1005/1006/1015 only pick an encoding and
  // say nothing about whether the program wants events.
  var mouseOn = false, mouseSgr = false;
  [['h', true], ['l', false]].forEach(function (pair) {
    try {
      term.parser.registerCsiHandler({ prefix: '?', final: pair[0] },
        function (params) {
          for (var i = 0; i < params.length; i++) {
            var mode = params[i];
            if (mode === 1000 || mode === 1002 || mode === 1003) {
              mouseOn = pair[1];
            }
            // 1006 does not say the program wants events, only how it wants
            // them spelled. Tracked separately for exactly that reason.
            if (mode === 1006) mouseSgr = pair[1];
          }
          return false;              // xterm still has to act on it
        });
    } catch (e) {}
  });

  function bufferType() {
    try {
      var type = term.buffer.active.type;
      return typeof type === 'string' ? type : '';
    } catch (e) { return ''; }
  }

  // '' means send nothing. Everything unrecognised lands here on purpose: an
  // unknown program, a renamed API, a mode nobody published. The failure mode
  // of this check has to be "behaves like it did yesterday", never a guess.
  function swipeKind() {
    // A program that asked for the mouse scrolls itself, and tmux cannot do
    // it for them: an alternate-screen program's lines never enter the pane's
    // history at all, so copy-mode would be scrolling back through whatever
    // was on screen before the program started. Claude Code, vim, htop, less
    // and tmux-with-`mouse on` are all in this class, and all of them already
    // answer the wheel -- so send the wheel.
    if (mouseOn) return 'wheel';
    var type = bufferType();
    if (type === 'normal') return 'local';
    if (type !== 'alternate') return '';
    if (published === 'external') return 'tmux';
    if (published === 'app') return 'keys';
    return '';
  }

  // ── reading history is a state, and states need a door ────────────────
  // `prefix PageUp` is copy-mode, and tmux never leaves it on its own.
  // Measured: after swiping up and back down to the bottom the pane still
  // reported pane_in_mode=1, and a typed `echoXYZ` produced nothing at all
  // -- the screen looked live, the keystrokes went into copy-mode commands
  // and vanished. It also survives detach and reattach.
  //
  // Two ways out, because one of them has to be the one you happen to try:
  // the bar says where you are and is itself the button, and typing anything
  // at all leaves first. There is no sequence of taps that strands you.
  var scrolledBack = false, histText = null;
  function paintHistory(during) {
    if (!hist) return;
    if (!histText) hist.appendChild(histText = document.createTextNode(''));
    var show = during || (scrolledBack ? 'history · tap to go live' : '');
    // nodeValue, never textContent: replacing children is a structural change
    // and this one updates mid-gesture, which is when that ends the gesture.
    if (histText.nodeValue !== show) histText.nodeValue = show;
    // Shown and hidden without touching the layout, and emphatically without
    // refit(): resizing the terminal mid-gesture makes tmux reflow and
    // repaint the whole screen, which is a jolt on every single swipe. It
    // overlays a row instead. A row is cheaper than the screen jumping.
    hist.hidden = !show;
  }
  if (hist) {
    hist.addEventListener('click', function () { leaveHistory(); });
    keepFocus(hist);
  }
  // The label a gesture in progress owns, so that entering history halfway
  // through one does not replace the running count with the standing bar.
  function duringLabel() {
    if (!scrolled) return '';
    return (scrolled > 0 ? '▲ ' : '▼ ') + Math.abs(scrolled) + ' lines';
  }
  function enterHistory() {
    if (scrolledBack) return;
    scrolledBack = true;
    paintHistory(duringLabel());
  }
  // q is copy-mode's cancel in both key tables, and harmless outside one.
  function leaveHistory() {
    if (!scrolledBack) return;
    scrolledBack = false;
    sendText('q');
    paintHistory('');
  }

  // One argument, signed, in LINES -- the unit the content moves in. It used
  // to be pages, because `prefix PageUp` enters copy-mode and scrolls in a
  // single keystroke, and that convenience is the whole reason scrolling back
  // felt like jumping rather than scrolling.
  function swipeBy(lines) {
    var kind = swipeKind();
    if (!lines || !kind) return !!kind;
    // Content moves further than the finger, the way a wheel does. One row of
    // thumb for one row of text is honest and reads as slow: a thumb crosses
    // about 27 rows before it runs out of screen, while a wheel notch is a
    // flick of one finger joint and moves three. Three is that ratio, and it
    // is also what makes NOTCH below mean what it says -- one finger row
    // becomes one notch, which is what a notch is worth in most programs.
    lines *= GAIN;
    scrollSoon(lines);
    haptic();
    scrolled += lines;
    paintHistory(duringLabel());
    return true;
  }

  // What actually goes out, once a cadence rather than once an event. The
  // kind is re-read here because a program can turn the mouse on or off
  // between the finger moving and this running.
  function emitScroll(lines) {
    var kind = swipeKind();
    if (!lines || !kind) return;
    var up = lines > 0, n = Math.abs(lines);
    if (kind === 'wheel') {
      // A wheel notch, not a line: that is the unit the program was built
      // around, and every one of them turns a notch into however many lines
      // it thinks a notch is worth (tmux's own binding is five). Sending one
      // report per line would scroll several times too fast for the same
      // finger travel.
      wheelDebt += lines;
      while (wheelDebt >= NOTCH) { wheelDebt -= NOTCH; sendText(wheelReport(true)); }
      while (wheelDebt <= -NOTCH) { wheelDebt += NOTCH; sendText(wheelReport(false)); }
    } else if (kind === 'local') {
      // Stays in the browser: nothing reaches the program at all.
      try { term.scrollLines(-lines); } catch (e) {}
    } else if (kind === 'tmux') {
      //   prefix [   copy-mode, entered where you are rather than a page up
      //   C-Up       send -X scroll-up, exactly one line -- measured through
      //              #{scroll_position}: five presses gave 5, three back gave
      //              2, while one PageUp jumped 41
      //
      // Cursor Up is NOT the primitive and looks like it: it moves the cursor
      // within the screen and only scrolls once that reaches the top edge.
      // Both of these are tmux defaults, so this needs no configuration.
      // Down while already live is a swipe against the bottom of the
      // scrollback: there is nothing below, and entering copy-mode to look
      // for it would put you in a mode you did not ask for to show you
      // nothing.
      if (!scrolledBack && !up) return false;
      if (!scrolledBack) sendText(prefixSeq + '[');
      enterHistory();
      sendText(new Array(n + 1).join(up ? '\x1b[1;5A' : '\x1b[1;5B'));
    } else if (kind === 'keys') {
      // Textual, not tmux. Its arrows move a selection rather than a
      // viewport, and the selection here decides what Enter attaches to, so
      // this one stays on pages: a whole screen per screen of travel.
      pageDebt += lines;
      var rows = term.rows || 24;
      while (pageDebt >= rows) { pageDebt -= rows; sendText('\x1b[5~'); }
      while (pageDebt <= -rows) { pageDebt += rows; sendText('\x1b[6~'); }
    }
    // Let what this drag asked for reach the screen, and nothing else.
    // Holding every byte for a whole gesture was the safe answer to the iOS
    // cascade and it made a drag feel dead; holding everything *except* the
    // drag's own answer is the same protection with the feedback back.
    flushSoon();
  }

  // Far enough above tap slop (~10px) that no tap scrolls, near enough that a
  // deliberate drag answers at once. Only the *first* line of a gesture pays
  // it; after that the content tracks the finger row for row.
  var SLOP = 16;

  // Where the finger is, in cells, because a mouse report carries a position
  // and some programs scroll the pane under it rather than the focused one.
  var lastX = 0, lastY = 0;
  function cellAt() {
    var box = host.getBoundingClientRect();
    var cols = term.cols || 80, rows = term.rows || 24;
    var col = Math.floor((lastX - box.left) / (box.width / cols)) + 1;
    var row = Math.floor((lastY - box.top) / (box.height / rows)) + 1;
    return [Math.min(Math.max(col, 1), cols), Math.min(Math.max(row, 1), rows)];
  }

  // 64 is wheel up and 65 wheel down, in both encodings. SGR is unbounded and
  // is what anything modern asks for with 1006; the original is three bytes
  // biased by 32, which cannot express a coordinate past 223 -- so a report
  // that would lie is not sent at all rather than pointing somewhere else.
  // Every scroll the far end is asked for costs a full-screen repaint coming
  // back, and touchmove fires sixty times a second. Sent one per event, that
  // is sixty screens a second over the socket for one flick -- which is what
  // "slow to pull" was. Batched on a cadence instead: the same total travel,
  // a handful of repaints.
  var owed = 0, owedTimer = 0;
  function scrollSoon(lines) {
    owed += lines;
    if (owedTimer) return;
    owedTimer = setTimeout(payScroll, 45);
  }
  function payScroll() {
    owedTimer = 0;
    var lines = owed;
    owed = 0;
    if (lines) emitScroll(lines);
  }

  var GAIN = 3, NOTCH = 3, wheelDebt = 0;
  function wheelReport(up) {
    var at = cellAt(), button = up ? 64 : 65;
    if (mouseSgr) {
      return '\x1b[<' + button + ';' + at[0] + ';' + at[1] + 'M';
    }
    if (at[0] > 223 || at[1] > 223) return '';
    return '\x1b[M' + String.fromCharCode(32 + button, 32 + at[0], 32 + at[1]);
  }

  // A row's height in CSS pixels, which is what turns finger travel into
  // lines. Derived rather than read out of xterm: the renderer's own cell
  // metrics are not a public API, and rows into height is the same number.
  function lineHeight() {
    var box = host.getBoundingClientRect().height;
    var rows = term.rows || 24;
    return Math.max(6, box / rows);
  }

  // ── iOS: a repaint during a drag kills the rest of the gesture ────────
  // "During the touch event cascade, Safari iOS stops firing events when a
  // DOM change takes place. Only DOM methods such as appendChild() count --
  // innerHTML does not." (quirksmode, The iOS event cascade and innerHTML.)
  //
  // xterm's DOM renderer rebuilds its row elements exactly that way. So
  // nothing reaches the renderer while a finger is down except the answer to
  // what that finger asked for -- unrelated output, a status-bar clock, a
  // reconnect are all held and played out in order on release.
  var held = [], holding = false, holdTimer = 0, flushTimer = 0;
  // Output that arrives while the drawer is open can fill the rows it was
  // sitting over, and the line you are watching would go under it. Rechecked
  // on a cadence rather than per write: a build log writes hundreds of times
  // a second and the answer only changes when the last line does.
  var reshiftTimer = 0;
  function reshiftSoon() {
    if (!expanded || reshiftTimer) return;
    reshiftTimer = setTimeout(function () {
      reshiftTimer = 0;
      if (expanded) shiftTerminal(neededShift());
    }, 120);
  }

  function feed(data) {
    if (holding) held.push(data);
    else term.write(data);
    reshiftSoon();
  }
  // Everything queued goes to the screen, in order. The hold itself stays on:
  // this is used mid-gesture to let one scroll through.
  function flushHeld() {
    if (flushTimer) { clearTimeout(flushTimer); flushTimer = 0; }
    var pending = held;
    held = [];
    pending.forEach(function (data) { term.write(data); });
  }
  // tmux answers over the socket, so there is nothing to flush at the moment
  // the keys go out; this waits for the reply rather than guessing.
  // A cadence, not a delay after the last call. Rescheduling on every call
  // was the bug: touchmove fires every ~16ms and each one asked again, so the
  // timer never expired and nothing was drawn until the finger came up.
  function flushSoon() {
    if (flushTimer) return;
    flushTimer = setTimeout(flushHeld, 50);
  }
  function holdWrites(on) {
    if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; }
    holding = on;
    if (on) {
      // A backstop, not the mechanism: touchend and touchcancel release
      // this. A gesture that somehow ends without either must not leave the
      // terminal frozen, and no swipe lasts three seconds.
      holdTimer = setTimeout(function () { holdWrites(false); }, 3000);
      return;
    }
    flushHeld();
  }

  // ── a readout for a device you cannot attach a debugger to ────────────
  // Opt-in with ?debug=1, created only then. "Nothing happens" on someone
  // else's phone is otherwise indistinguishable between four different
  // faults: the events never arrived, the buffer is not what we think,
  // nothing published, or the drag never reached a step. This shows all
  // four, so the device answers instead of the next guess.
  var debugBox = null, debugText = null;
  if (/[?&]debug=1/.test(location.search)) {
    debugBox = document.createElement('div');
    debugBox.id = 'debug';
    // Written through a text node's nodeValue, never textContent: assigning
    // textContent replaces the element's children, and a structural change
    // is precisely what ends a gesture on iOS. A readout that killed the
    // thing it exists to measure would be worse than no readout.
    debugText = document.createTextNode('');
    debugBox.appendChild(debugText);
    document.getElementById('app').appendChild(debugBox);
  }
  var moves = 0, scrolled = 0, pageDebt = 0;
  function showDebug(dy) {
    if (!debugText) return;
    debugText.nodeValue =
      'mv ' + moves + '  dy ' + Math.round(dy) + '/' + Math.round(lineHeight())
      + '  buf ' + (bufferType() || '?') + '  pub ' + (published || '?')
      + '  kind ' + (swipeKind() || '-');
  }

  // Pinch to zoom the font. The page itself must not zoom -- a zoomed viewport
  // makes a terminal unreadable and unscrollable at once -- so this is the
  // only zoom available, and it is the one that helps.
  // ── press and hold on the terminal ─────────────────────────────────────
  // What iOS does natively, done here instead, because natively it does not
  // happen at all: touch selection over xterm does not work on iOS with any
  // renderer (xtermjs/xterm.js#3727, open since 2022), and no engine on this
  // machine reproduces the iOS long press either -- headless Chrome and
  // WebKit both decline to select a bare <pre> under one. So a gesture built
  // on their recogniser could be neither made to work nor shown to work.
  //
  // This one is a timer of ours: finger down, 500ms, no movement. That is
  // measurable here, exactly as measurable on the phone, and it puts the
  // line straight on the clipboard -- which is what the system menu would
  // have been used for anyway, minus the menu.
  //
  // A line, because a line is what gets copied out of a terminal: a path, a
  // job id, a command worth running again. Anything smaller or spanning
  // several is what the copy view is still there for.
  var LONG_PRESS_MS = 500, LONG_PRESS_SLOP = 8;
  var pressTimer = 0, pressed = null;
  // Where the finger went down. Kept out here because touchmove needs it
  // while the timer is still pending -- it used to fall back to the current
  // point when `pressed` was not yet set, which made the comparison
  // `|now - now| > slop` and cancelled nothing. Measured: every gesture
  // tried, including a plain fast scroll, ended in the copy view.
  var pressFrom = null;

  // Said once, ever. A gesture nobody knows about is a gesture that does not
  // exist, and there is nothing on a terminal to hint at one -- but a tip
  // repeated on every attach is noise from the second time onwards.
  var PRESS_HINT = 'atmux.presshint';
  function offerPressHint() {
    if (!touch) return;
    try {
      if (localStorage.getItem(PRESS_HINT)) return;
      localStorage.setItem(PRESS_HINT, '1');
    } catch (e) { return; }
    // What the gesture does, not what it used to do: it hands you a
    // selection to adjust, it does not copy a line behind your back.
    setTimeout(function () { say('press and hold a line to select it'); }, 1200);
  }

  function rowUnder() {
    // The rendered rows, so this lands on what is actually on screen rather
    // than on arithmetic that has to agree with the renderer about padding.
    var rows = host.querySelector('.xterm-rows');
    if (!rows) return -1;
    for (var i = 0; i < rows.children.length; i++) {
      var box = rows.children[i].getBoundingClientRect();
      if (lastY >= box.top && lastY <= box.bottom) return i;
    }
    return -1;
  }

  // The whole logical line, not the row that happened to be under the
  // finger. A path long enough to be worth copying is a path long enough to
  // wrap, and half of one is worse than none: it looks like it worked.
  // xterm marks a continuation row isWrapped, so the run is found by walking
  // out in both directions from wherever the finger landed.
  function lineSpan(row) {
    var buf = term.buffer.active;
    var base = (buf.viewportY || 0) + row;
    var first = base, last = base;
    var line = buf.getLine(first);
    while (line && line.isWrapped && first > 0) {
      first -= 1;
      line = buf.getLine(first);
    }
    var next = buf.getLine(last + 1);
    while (next && next.isWrapped) {
      last += 1;
      next = buf.getLine(last + 1);
    }
    var out = [];
    for (var i = first; i <= last; i++) {
      var each = buf.getLine(i);
      out.push(each ? each.translateToString(true) : '');
    }
    return {
      text: out.join(''),          // joined, not newline-separated: one line
      first: first - (buf.viewportY || 0),
      last: last - (buf.viewportY || 0)
    };
  }

  function cancelPress() {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = 0; }
    pressed = null;
    pressFrom = null;
  }

  var pinchStart = 0, pinchFont = 0;
  var dragFrom = 0, swiped = false;
  // ── swiping between tmux windows ──────────────────────────────────────
  // The vertical drag has always been the scrollback; sideways was doing
  // nothing at all. `◀ win` and `win ▶` are two of the five buttons the pad
  // spends its front row on, for the tmux action people take most after
  // detaching -- and switching windows by aiming at a 72px target is the
  // part of this that never felt like a phone.
  //
  // Left goes forward, the way every set of pages and tabs on this device
  // already works.
  var dragFromX = 0, switched = false;
  var SWITCH_TRAVEL = 60;      // far enough that a crooked scroll is not one
  var SWITCH_BIAS = 1.6;       // and decisively sideways rather than merely
  var EDGE_GUARD = 24;         // iOS owns the first stripe: that is Back

  function swipeWindow(dx) {
    if (published !== 'external' || !prefixSeq) return false;
    typed(prefixSeq + (dx < 0 ? 'n' : 'p'));
    haptic();
    say(dx < 0 ? 'window ▶' : '◀ window');
    return true;
  }
  host.addEventListener('touchstart', function (event) {
    // Selecting is the browser's gesture, all of it. Not even holding the
    // writes: a repaint mid-selection is the browser's business now.
    if (selecting) return;
    // Before anything else, and for a pinch as much as a swipe: both are
    // gestures iOS will abandon the moment a row element is rebuilt.
    holdWrites(true);
    if (event.touches.length === 2) {
      pinchStart = spread(event.touches);
      pinchFont = term.options.fontSize;
      swiped = false;
    } else if (event.touches.length === 1) {
      dragFrom = event.touches[0].clientY;
      dragFromX = event.touches[0].clientX;
      swiped = false;
      switched = false;
      moves = 0;
      scrolled = 0;
      showDebug(0);
      // Where the finger is, now, rather than only once it has moved: a
      // press that never moves never reaches touchmove, and this is the
      // gesture that never moves.
      lastX = event.touches[0].clientX;
      lastY = event.touches[0].clientY;
      cancelPress();
      var from = {x: lastX, y: lastY};
      pressFrom = from;
      pressTimer = setTimeout(function () {
        pressTimer = 0;
        var row = rowUnder();
        if (row < 0) return;
        var span = lineSpan(row);
        // A blank row is not something anyone means to copy, and marking one
        // would be the gesture appearing to work and doing nothing.
        if (!span.text.trim()) return;
        // Armed, not fired. The view opens on the lift, and any movement
        // before then puts it back -- so a finger that rests a moment and
        // then scrolls is a scroll, which is what it was always meant to be.
        //
        // Opening here instead was right while the hold copied to the
        // clipboard: that needs a user gesture and cannot wait for a
        // touchend iOS may replace with touchcancel. It opens a view now,
        // and a view needs no permission -- so the irrevocable half of the
        // gesture went with the clipboard call.
        pressed = {from: from, row: row, span: span};
        haptic();
      }, LONG_PRESS_MS);
    } else {
      cancelPress();
    }
  }, { passive: true });
  host.addEventListener('touchmove', function (event) {
    if (selecting) return;
    if (event.touches.length === 2 && pinchStart > 0) {
      event.preventDefault();
      setFont(pinchFont * (spread(event.touches) / pinchStart));
      return;
    }
    if (event.touches.length !== 1 || pinchStart > 0) return;
    moves += 1;
    lastX = event.touches[0].clientX;
    lastY = event.touches[0].clientY;
    // Moved, so it is a drag and not a hold. Checked against where the
    // finger started rather than against the last frame: a slow drag never
    // moves far in one frame and would hold its way into a copy.
    if (pressFrom) {
      var start = pressFrom;
      if (pressTimer && (Math.abs(lastX - start.x) > LONG_PRESS_SLOP ||
                         Math.abs(lastY - start.y) > LONG_PRESS_SLOP)) {
        cancelPress();
      } else if (pressed &&
                 (Math.abs(lastX - start.x) > LONG_PRESS_SLOP ||
                  Math.abs(lastY - start.y) > LONG_PRESS_SLOP)) {
        // Already marked, and now the finger is travelling: they changed
        // their mind into a scroll, and the mark has to go with it.
        cancelPress();
      }
    }
    var dy = event.touches[0].clientY - dragFrom;
    var dx = event.touches[0].clientX - dragFromX;
    // Sideways first, and only while nothing vertical has already been
    // claimed: a gesture is one thing or the other, and a scroll that drifts
    // must not turn into a window switch half way down.
    if (!swiped && !switched &&
        Math.abs(dx) > SWITCH_TRAVEL &&
        Math.abs(dx) > SWITCH_BIAS * Math.abs(dy) &&
        dragFromX > EDGE_GUARD &&
        dragFromX < window.innerWidth - EDGE_GUARD) {
      cancelPress();
      if (swipeWindow(dx)) {
        switched = true;
        event.preventDefault();
      }
      return;
    }
    if (switched) return;      // the rest of this finger belongs to that
    showDebug(dy);
    // Slop first, so a tap that trembles is still a tap; after that the
    // content follows the finger one row per row-height, which is what makes
    // this scrolling rather than paging.
    if (!swiped && Math.abs(dy) < SLOP) return;
    var unit = lineHeight();
    var lines = dy > 0 ? Math.floor(dy / unit) : Math.ceil(dy / unit);
    if (!lines) return;
    dragFrom += lines * unit;
    if (!swipeBy(lines)) return;
    swiped = true;
    // Only once the gesture is ours. Claiming it before then would take the
    // one the browser might still want.
    if (swiped) event.preventDefault();
  }, { passive: false });
  // touchcancel as well as touchend: iOS fires it when the system takes a
  // gesture away mid-drag, and a run of state left over from a drag that
  // never ended seeds the next one wrong -- a pinch that reads as a swipe.
  ['touchend', 'touchcancel'].forEach(function (name) {
    host.addEventListener(name, function (event) {
      if (selecting) return;
      // On the lift, not at the 500ms mark: Safari wants a clipboard write to
      // sit inside a user gesture, and the end of a touch is one where the
      // middle of a hold is arguable. The mark appears at 500ms so the wait
      // is visible; the copy lands when the finger goes.
      // The copy already happened, at the 500ms mark. All that is left is to
      // stop this being read as a tap or a flick as well -- and this runs for
      // touchcancel too, which is the whole point.
      if (pressed) {
        // The gesture completed without moving. Both endings count: iOS
        // hands a stationary press back as touchcancel as often as touchend
        // once it starts thinking about its own long press.
        var span = pressed.span;
        var here = host.querySelector('.xterm-rows');
        var mark = here && here.children[Math.max(0, span.first)];
        cancelPress();
        setSelecting(true, {
          index: (term.buffer.active.viewportY || 0) + span.first,
          screenY: mark ? mark.getBoundingClientRect().top : lastY
        });
        pinchStart = 0; swiped = false;
        holdWrites(false);
        scrolled = 0; pageDebt = 0; wheelDebt = 0;
        if (owedTimer) { clearTimeout(owedTimer); owedTimer = 0; }
        payScroll();
        paintHistory('');
        return;
      }
      cancelPress();
      pinchStart = 0; swiped = false;
      holdWrites(false);
      // The count belonged to the gesture; what remains is where you are.
      scrolled = 0; pageDebt = 0; wheelDebt = 0;
      // Anything still owed belongs to the gesture that just ended.
      if (owedTimer) { clearTimeout(owedTimer); owedTimer = 0; }
      payScroll();
      paintHistory('');
    });
  });
  function spread(touches) {
    var dx = touches[0].clientX - touches[1].clientX;
    var dy = touches[0].clientY - touches[1].clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  // ── layout ────────────────────────────────────────────────────────────
  // The screen decides the grid and the grid decides the font size -- not the
  // other way round. Getting this backwards is what produced every symptom on
  // a phone at once: a hard-coded 11px font gave 56 columns, which is too few
  // for either of the dashboard's layouts, so the table truncated STATUS away
  // and grew a scrollbar of its own; and FitAddon reserved a flat 14px for a
  // scrollbar this app can never show, which at that font size is four
  // characters of dead width down the right-hand side. Four characters is not
  // a rounding error and no background colour hides it, because the strip is
  // outside the canvas and the canvas is not one colour.
  //
  // So: own the arithmetic, and take the target width from the app itself.

  // Widths the dashboard has layouts for, widest first, served by the page
  // (config.LAYOUT_WIDTHS). Hard-coding them here would be the same mistake
  // in a new place: a breakpoint one side does not know about is a breakpoint
  // the other side lands just short of.
  function layoutWidths() {
    var meta = document.querySelector('meta[name="atmux-layout"]');
    var out = String(meta && meta.content || '').split(',')
      .map(function (n) { return parseInt(n, 10); })
      .filter(function (n) { return n >= 20 && n <= 500; })
      .sort(function (a, b) { return b - a; });
    return out.length ? out : [118, 65, 50];
  }

  // What one cell costs, in CSS pixels. xterm measures this from the rendered
  // font; the painted screen is the fallback and the ground truth once a
  // frame exists.
  function cellSize() {
    var d = null;
    try { d = term._core._renderService.dimensions.css.cell; } catch (e) {}
    if (d && d.width > 0 && d.height > 0) return { w: d.width, h: d.height };
    var screen = host.querySelector('.xterm-screen');
    var rect = screen && screen.getBoundingClientRect();
    if (rect && rect.width > 0 && term.cols > 0 && term.rows > 0) {
      return { w: rect.width / term.cols, h: rect.height / term.rows };
    }
    return null;
  }

  // The two ends of what the font may be.
  //
  // The floor was 9, and a phone sat on it: 393 CSS pixels over the 65-column
  // layout is 9.5px type, which is what "字太小看不清" was about. It is high
  // enough now that a phone reaches for the 50-column layout instead and gets
  // ~12.5px, and low enough that an iPad and a phone on its side still afford
  // the 118-column split -- both land at ~11, measured.
  //
  // The ceiling is not one number, because a size in CSS pixels means
  // different things on different screens. A Retina display draws two device
  // pixels per CSS pixel, so 13px type there is laid down with as many dots
  // as 26px type on a 1x monitor: it stays sharp at a density that would be
  // mush on the other one.
  //
  // Measured on the machine this was reported from -- a 14" MacBook Pro,
  // 3024x1964 behind 1512 CSS pixels. A native terminal on it shows around
  // 200 columns. Stretching the font until exactly the 118-column layout
  // fitted gave 122, and 118 is the *minimum* for the split view, not a
  // target: past it the extra columns go to the table and the preview in
  // the same 56/44 proportion, which is the thing a big screen is for.
  var MIN_AUTO = 11;

  function maxAuto() {
    return (window.devicePixelRatio || 1) >= 2 ? 13 : 16;
  }

  // The widest layout this screen can afford at a legible size. Cell width is
  // exactly proportional to font size -- checked across 7px to 16px, the
  // ratio held to four decimals -- so one measurement fixes the constant and
  // the rest is division.
  function autoFont(width) {
    var cell = cellSize();
    if (!cell || !term.options.fontSize) return term.options.fontSize;
    var perPoint = cell.w / term.options.fontSize;
    var widths = layoutWidths();
    var size = 0;
    for (var i = 0; i < widths.length; i++) {
      // Round down: rounding up lands one column short of the target, which
      // is the one place it must not land.
      size = Math.floor(width / widths[i] / perPoint * 2) / 2;
      if (size >= MIN_AUTO) return Math.min(size, maxAuto());
    }
    // No layout reaches the floor -- a 320px phone is the case. Take the
    // narrowest layout at whatever it costs rather than the floor at
    // whatever width it happens to land on: `return MIN_AUTO` gave a 320px
    // screen 48 columns, which is not a width the dashboard has a layout
    // for, and landing between two of them is the entire failure this list
    // exists to prevent. It is how a phone once got 56 columns and grew a
    // scrollbar inside a full-screen app.
    return Math.max(size, 6);
  }

  function applyGrid() {
    var box = host.getBoundingClientRect();
    var cell = cellSize();
    if (!cell || box.width < 1 || box.height < 1) return;
    var cols = Math.max(2, Math.floor(box.width / cell.w));
    var rows = Math.max(1, Math.floor(box.height / cell.h));
    if (cols !== term.cols || rows !== term.rows) {
      try { term.resize(cols, rows); } catch (e) { return; }
    }
    // Whatever is left over is now under one character wide and under one row
    // tall, by construction. Sizing the terminal to its own grid hands that
    // remainder to the page, which centres it: a hairline on each side rather
    // than a band down one.
    if (term.element) {
      term.element.style.width = (cols * cell.w) + 'px';
      term.element.style.height = (rows * cell.h) + 'px';
    }
    sendResize();
    if (announce) {
      announce = false;
      say(term.options.fontSize + 'px · ' + cols + '×' + rows +
          (manualFont === null ? ' · auto' : ''));
    }
  }
  // Only say something when the size was asked for. Rotating a phone or
  // raising the keyboard relays out too, and a toast on every one of those is
  // noise over the thing you were reading.
  var announce = false;

  function relayout() {
    if (manualFont === null) {
      var box = host.getBoundingClientRect();
      var size = box.width > 0 ? autoFont(box.width) : term.options.fontSize;
      if (size && size !== term.options.fontSize) {
        term.options.fontSize = size;
        // The cell is re-measured from the DOM; give the browser the frame it
        // needs before asking what a cell is worth now.
        requestAnimationFrame(applyGrid);
        return;
      }
    }
    applyGrid();
  }

  var refitTimer = null;
  function refit() {
    clearTimeout(refitTimer);
    refitTimer = setTimeout(relayout, 60);
  }

  // The software keyboard shrinks the *visual* viewport and leaves the layout
  // viewport alone, so a page sized in vh/dvh keeps its full height and its
  // last rows -- the ones with the cursor in them -- sit behind the keyboard.
  // Sizing the app to visualViewport instead is the only way to see what you
  // are typing.
  //
  // iOS also scrolls the layout viewport to bring the focused element into
  // view, which slides the top of the terminal off screen; offsetTop is how
  // much, and pinning the page back to 0 undoes it.
  var app = document.getElementById('app');
  var vv = window.visualViewport;
  function syncViewport() {
    if (!vv) return;
    app.style.height = vv.height + 'px';
    app.style.transform = vv.offsetTop
      ? 'translateY(' + vv.offsetTop + 'px)' : '';
    refit();
  }
  if (vv) {
    vv.addEventListener('resize', syncViewport);
    vv.addEventListener('scroll', syncViewport);
    syncViewport();
  }
  window.addEventListener('resize', function () {
    syncViewport(); recolumn(); refit();
  });
  window.addEventListener('orientationchange', function () {
    // The viewport metrics are wrong until the rotation animation finishes.
    setTimeout(function () { syncViewport(); recolumn(); refit(); }, 300);
  });
  // Keep the layout viewport pinned: iOS scrolls it under the keyboard and
  // nothing scrolls it back, leaving a permanent gap above the terminal.
  window.addEventListener('scroll', function () {
    if (vv && vv.offsetTop === 0 && window.scrollY !== 0) window.scrollTo(0, 0);
  });
  // ── which build this is ─────────────────────────────────────────────────
  // This page declares apple-mobile-web-app-capable, so on a phone it is a
  // home-screen app, and iOS resumes one of those from a snapshot rather than
  // fetching it again. A deploy can land, be served correctly, and never
  // reach the screen -- and nothing on the page said which of the two had
  // happened, so "did you deploy it?" and "yes, I checked the bytes on the
  // wire" were both true and neither was an answer.
  var buildMeta = document.querySelector('meta[name="atmux-build"]');
  var MY_BUILD = buildMeta ? buildMeta.content : '';
  var newBuild = document.getElementById('newbuild');

  if (buildLine) buildLine.textContent = MY_BUILD ? 'build ' + MY_BUILD : '';

  function checkBuild() {
    if (!MY_BUILD || !newBuild) return;
    // `../api/build`, not `api/build`: this page is at /console/ and the API
    // is beside it, not under it. Relative either way -- whatever prefix
    // `tailscale serve --set-path` adds is not ours to know.
    fetch(new URL('../api/build', location.href).toString(),
          { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.build || data.build === MY_BUILD) return;
        newBuild.hidden = false;
        newBuild.textContent = 'newer build — tap to load  ('
                             + MY_BUILD + ' → ' + data.build + ')';
        if (buildLine) {
          buildLine.textContent = 'build ' + MY_BUILD + ' · server '
                                + data.build;
        }
      })
      // Offline is not news here: the socket already says so, louder.
      .catch(function () {});
  }

  if (newBuild) {
    newBuild.addEventListener('click', function () {
      newBuild.textContent = 'loading…';
      // Reconnecting to a tmux session is what this page does on every load,
      // so a reload costs nothing that was not already reattachable.
      location.reload();
    });
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) {
      refit();
      if (ws && ws.readyState > 1) connect();
      // Coming back to the foreground is exactly when a stale snapshot
      // returns, so it is exactly when this is worth asking.
      checkBuild();
    }
  });
  window.addEventListener('beforeunload', function () { closed = true; });

  if (touch) {
    document.body.classList.add('touch');
    // The published keys start empty; movement and escape do not, because
    // they have to work before anything has published and after everything
    // has stopped. renderKeys builds the navigation row too, so that both
    // grids are decided by one measurement of one width.
    renderKeys();
    setKeyboard(false);
    var stored = '';
    try { stored = localStorage.getItem(PAD_STATE) || ''; } catch (e) {}
    // Collapsed on arrival, not expanded. Three rows of keys is 41% of a
    // phone, and the reason you opened a session is on the other 59% -- the
    // keys are for acting on what you read, so they can wait to be asked
    // for. `⌃` at the bottom edge brings them back and the choice sticks,
    // so this is a default rather than a decision taken away.
    if (stored !== 'shown') setPad(false);
  }

  if (boot) boot.style.display = 'none';
  connect();
  checkBuild();
  offerPressHint();
  term.focus();
})();
