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

  var textarea = host.querySelector('textarea');

  var status = document.getElementById('status');
  var hideTimer = null;
  function say(text, sticky) {
    status.textContent = text;
    status.style.display = 'block';
    clearTimeout(hideTimer);
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
    var query = [];
    if (touch) query.push('touch=1');
    var here = new URLSearchParams(location.search);
    ['attach', 'select'].forEach(function (verb) {
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
  function setKeyboard(open) {
    if (!textarea) return;
    kbdOpen = open;
    var button = document.getElementById('kbd');
    if (button) button.classList.toggle('on', open);
    textarea.classList.toggle('atmux-typing', open);
    if (open) {
      textarea.removeAttribute('inputmode');
      // Worth a try: on a platform that does honour it this saves the tap.
      try { textarea.focus(); } catch (e) {}
      say('tap the screen to type');
    } else {
      textarea.setAttribute('inputmode', 'none');
      textarea.blur();
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
  var NAV_KEYS = [
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
    { l: '⏎', k: '\r' },
    { l: '←', k: '\x1b[D' },
    { l: '↓', k: '\x1b[B' },
    { l: '↑', k: '\x1b[A' },
    { l: '→', k: '\x1b[C' }
  ];

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
  var TMUX_VERBS = [
    { l: 'detach', s: 'd' },
    { l: 'new win', s: 'c' },
    { l: '◀ win', s: 'p' },
    { l: 'win ▶', s: 'n' },
    { l: 'zoom', s: 'z' },
    { l: 'scroll', s: '[' },
    { l: 'split ─', s: '"' },
    { l: 'split │', s: '%' },
    { l: 'windows', s: 'w' },
    { l: 'pane ▶', s: 'o' },
    { l: 'rename', s: ',' },
    { l: 'prefix', s: '' }
  ];
  function tmuxKeys() {
    return TMUX_VERBS.map(function (verb) {
      return { l: verb.l, k: prefixSeq + verb.s };
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
  var pins = loadPins(), editing = false;

  function loadPins() {
    try {
      var raw = JSON.parse(localStorage.getItem(PINS) || '[]');
      if (!Array.isArray(raw)) return [];
      return raw.filter(function (label) {
        return typeof label === 'string' && label && label.length <= 24;
      }).slice(0, MAX_PINS);
    } catch (e) { return []; }
  }

  function savePins() {
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

  var keys = document.getElementById('keys');
  var nav = document.getElementById('nav');
  var pinRow = document.getElementById('pins');
  var expander = document.getElementById('more');
  // Two rows fit under a phone's thumb without eating the dashboard. The
  // rest are one tap away rather than one page away.
  var ROWS_COLLAPSED = 2;
  var current = [], expanded = false;

  // How many across is the screen's decision, not a constant -- the same rule
  // the font size follows, for the same reason. Four is right on a phone and
  // wrong on everything else: measured in landscape at 844px it made `|` a
  // 204px button and spread forty-two keys over eleven rows on a screen 390px
  // tall. What should stay put is the size of a key, not the number of them.
  var KEY_TARGET = 86, PER_ROW_MIN = 4, PER_ROW_MAX = 10;
  function perRow(width) {
    if (!(width > 0)) return PER_ROW_MIN;
    return Math.max(PER_ROW_MIN,
                    Math.min(PER_ROW_MAX, Math.floor(width / KEY_TARGET)));
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

  function renderNav() {
    if (!nav || nav.childElementCount) return;      // fixed; built once
    var line = document.createElement('div');
    line.className = 'krow';
    NAV_KEYS.forEach(function (entry) {
      line.appendChild(buildKey(entry));
    });
    nav.appendChild(line);
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
    }
  }

  // Yours, and therefore fixed: the whole point of keeping a key is that it
  // is in the same place every time, which a row inside the scrolling drawer
  // would not be.
  function renderPins(columns) {
    if (!pinRow) return;
    pinRow.textContent = '';
    var list = pinnedKeys();
    pinRow.style.display = list.length ? '' : 'none';
    if (list.length) renderRows(pinRow, list, columns, 0);
  }

  function renderKeys() {
    var columns = perRow(keys.getBoundingClientRect().width);
    columnsUsed = columns;
    renderPins(columns);
    keys.textContent = '';
    keys.classList.toggle('open', expanded);
    keys.classList.toggle('editing', editing);
    var hidden = 0;
    if (expanded) {
      groups().forEach(function (group) {
        if (!group.keys.length) return;
        if (group.name) {
          var head = document.createElement('div');
          head.className = 'ghead';
          head.textContent = group.name;
          keys.appendChild(head);
        }
        renderRows(keys, group.keys, columns, 0);
      });
    } else {
      // Closed, the drawer shows what this screen can do. When nothing has
      // published anything the screen belongs to tmux or a shell, and the
      // useful answer is tmux's own verbs -- detach first. Showing nothing
      // there would put the one key nobody can guess behind a tap.
      var head = current.length ? current : tmuxKeys();
      var drawn = Math.min(renderRows(keys, head, columns, ROWS_COLLAPSED),
                           head.length);
      hidden = groups().reduce(function (n, group) {
        return n + group.keys.length;
      }, 0) - drawn;
    }
    if (expander) {
      // Always offered. There is always more than fits: the terminal's own
      // vocabulary does not depend on anything having been published, and a
      // hidden expander is what left a bare shell with three keys.
      expander.textContent = expanded ? '⌃' : '⌄ ' + Math.max(hidden, 0);
      expander.setAttribute(
        'aria-label', expanded ? 'fewer keys' : 'more keys');
    }
    // The pad's height just changed, and the terminal has to be told or the
    // app keeps drawing rows that are now behind the keys.
    refit();
  }

  function setKeys(list) {
    var same = list.length === current.length && list.every(function (e, i) {
      return e.k === current[i].k && e.l === current[i].l;
    });
    if (same) return;
    current = list;
    renderKeys();
  }

  // Only the drawer scrolls, and only a key that lives in it may scroll it:
  // the row above is fixed on purpose, and dragging a fixed key to move
  // something else is a gesture nobody asked for.
  function scrollDrawer(button, dy) {
    if (!keys.classList.contains('open')) return;
    if (!button.parentNode || button.parentNode.parentNode !== keys) return;
    keys.scrollTop += dy;
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

  function buildKey(entry) {
    if (entry.m) return buildModifier(entry);
    var label = entry.l, seq = entry.k;
    var button = document.createElement('button');
    button.className = 'key';
    // Derived rather than flagged, same as `repeats` below: a label that is a
    // single symbol is a glyph and wants the room a word does not.
    if (label.length === 1 && !/\w/.test(label)) button.classList.add('glyph');
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
    var originX = 0, originY = 0, lastY = 0;
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
      // The gesture stays ours -- this is what stops the button taking the
      // terminal's focus, and focus is what the software keyboard is attached
      // to. The cost is that the drawer will not pan by itself, which `moved`
      // below pays.
      event.preventDefault();
      live = true; repeated = false; dragging = false;
      originX = event.clientX; originY = event.clientY;
      lastY = event.clientY;
      button.classList.add('down');
      // Nothing repeats while you are choosing. A held key that pinned and
      // unpinned itself ten times a second is the obvious way to get this
      // wrong.
      if (repeats && !editing) {
        timer = setTimeout(function () {
          repeated = true;
          fire();
          interval = setInterval(fire, 70);
        }, 400);
      }
    }
    // On release rather than on touch. Firing on the way down is what a key
    // should do -- and it is also what would type something every time you
    // dragged through the drawer looking for a different key. A finger that
    // has not moved has still not scrolled anything, so a tap is a tap.
    function up() {
      if (live && !repeated && !dragging) {
        if (!editing) fire();
        else if (pinnable) togglePin(label);
      }
      stop();
    }
    // Past a few pixels this is a scroll, not a key. The drawer is nearly all
    // keys, so a finger dragging through it lands here rather than on any
    // scrollable gap -- which is why nothing moved at all, and why the keys
    // themselves have to do the scrolling.
    function moved(event) {
      if (!live) return;
      if (!dragging
          && (Math.abs(event.clientX - originX) > DRAG
              || Math.abs(event.clientY - originY) > DRAG)) {
        dragging = true;
        button.classList.remove('down');
        clearTimeout(timer); clearInterval(interval);
        timer = interval = null;
      }
      if (dragging) {
        scrollDrawer(button, lastY - event.clientY);
        lastY = event.clientY;
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
  }

  keepFocus(expander);
  if (expander) expander.addEventListener('click', function (event) {
    event.preventDefault();
    toggleDrawer();
  });

  keepFocus(editButton);
  if (editButton) editButton.addEventListener('click', function (event) {
    event.preventDefault();
    setEditing(!editing);
    say(editing ? 'tap keys to keep them' : 'kept');
    renderKeys();
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
    // Putting the pad away ends editing with it. Coming back to a pad whose
    // keys silently keep instead of press, because of something you did
    // before you hid it, is the same trap the expander closes.
    if (!shown && editing) { setEditing(false); renderKeys(); }
    try {
      localStorage.setItem(PAD_STATE, shown ? '' : 'hidden');
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
    var show = during || (scrolledBack ? '历史 · 点此回到实时' : '');
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
    return (scrolled > 0 ? '▲ ' : '▼ ') + Math.abs(scrolled) + ' 行';
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
  function feed(data) {
    if (holding) held.push(data);
    else term.write(data);
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
  var pinchStart = 0, pinchFont = 0;
  var dragFrom = 0, swiped = false;
  host.addEventListener('touchstart', function (event) {
    // Before anything else, and for a pinch as much as a swipe: both are
    // gestures iOS will abandon the moment a row element is rebuilt.
    holdWrites(true);
    if (event.touches.length === 2) {
      pinchStart = spread(event.touches);
      pinchFont = term.options.fontSize;
      swiped = false;
    } else if (event.touches.length === 1) {
      dragFrom = event.touches[0].clientY;
      swiped = false;
      moves = 0;
      scrolled = 0;
      showDebug(0);
    }
  }, { passive: true });
  host.addEventListener('touchmove', function (event) {
    if (event.touches.length === 2 && pinchStart > 0) {
      event.preventDefault();
      setFont(pinchFont * (spread(event.touches) / pinchStart));
      return;
    }
    if (event.touches.length !== 1 || pinchStart > 0) return;
    moves += 1;
    lastX = event.touches[0].clientX;
    lastY = event.touches[0].clientY;
    var dy = event.touches[0].clientY - dragFrom;
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
    host.addEventListener(name, function () {
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
    return out.length ? out : [118, 65];
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

  // Below this a phone is unreadable; above it a desktop is just wasteful,
  // and the extra width is better spent on columns than on letter height.
  var MIN_AUTO = 9, MAX_AUTO = 16;

  // The widest layout this screen can afford at a legible size. Cell width is
  // exactly proportional to font size -- checked across 7px to 16px, the
  // ratio held to four decimals -- so one measurement fixes the constant and
  // the rest is division.
  function autoFont(width) {
    var cell = cellSize();
    if (!cell || !term.options.fontSize) return term.options.fontSize;
    var perPoint = cell.w / term.options.fontSize;
    var widths = layoutWidths();
    for (var i = 0; i < widths.length; i++) {
      // Round down: rounding up lands one column short of the target, which
      // is the one place it must not land.
      var size = Math.floor(width / widths[i] / perPoint * 2) / 2;
      if (size >= MIN_AUTO) return Math.min(size, MAX_AUTO);
    }
    return MIN_AUTO;
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
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { refit(); if (ws && ws.readyState > 1) connect(); }
  });
  window.addEventListener('beforeunload', function () { closed = true; });

  if (touch) {
    document.body.classList.add('touch');
    // The published keys start empty; movement and escape do not, because
    // they have to work before anything has published and after everything
    // has stopped.
    renderNav();
    renderKeys();
    setKeyboard(false);
    var stored = '';
    try { stored = localStorage.getItem(PAD_STATE) || ''; } catch (e) {}
    if (stored === 'hidden') setPad(false);
  }

  if (boot) boot.style.display = 'none';
  connect();
  term.focus();
})();
