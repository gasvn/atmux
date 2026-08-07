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
    return scheme + '://' + location.host + base + 'ws';
  }

  function connect() {
    ws = new WebSocket(socketURL());
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () { retry = 0; say('connected'); sendResize(); };
    ws.onmessage = function (event) { term.write(new Uint8Array(event.data)); };
    ws.onclose = function () {
      if (closed) return;
      // A phone drops the socket every time it locks or changes network, so
      // reconnecting is the normal case, not the exceptional one.
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

  term.onData(function (data) { sendText(data); });
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
  // keypad still has arrows and ⏎.
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
  // Not a terminal keyboard. These are the keys atmux actually uses, and a
  // generic Ctrl/Esc/Tab row was the wrong tool: on this screen you navigate
  // and you act, and both are single keys.
  var ESC = '\x1b', BS = '\x7f';
  // Labelled, not lettered. A row of bare letters is unreadable on a phone --
  // you cannot tell `x` (kill a session) from `z` (change the layout) without
  // opening the help -- and flex-wrap stretched whichever key landed alone on
  // the last line into a full-width button with no clue what it did. Fixed
  // rows, and every key says what it does.
  var PAGES = {
    nav: { rows: [
      [['↑', ESC + '[A', 'rep'], ['↓', ESC + '[B', 'rep'],
       ['⏎ attach', '\r', 'wide'], ['esc', ESC]],
      [['←', ESC + '[D', 'rep'], ['→', ESC + '[C', 'rep'],
       ['⌫', BS, 'rep'], ['tab', '\t']]
    ] },
    // Grouped the way the help screen groups them: connect, then session
    // lifecycle, then view.
    atmux: { rows: [
      [['ssh', 's'], ['window', 'o'], ['local', 't'], ['view', 'v']],
      [['note', 'e'], ['new', 'n'], ['kill', 'x'], ['renew', 'k']],
      [['jobs', 'j'], ['layout', 'z'], ['clusters', 'g'], ['web', 'w']],
      [['refresh', 'r'], ['help', '?']]
    ] },
    // Once you attach, the keys that matter are tmux's, and detach is the one
    // nobody can guess -- it is the whole reason the handover banner exists.
    tmux: { rows: [
      [['detach', '\x02d', 'wide'], ['^C', '\x03'], ['^D', '\x04']],
      [['prefix', '\x02'], ['^Z', '\x1a'],
       ['PgUp', ESC + '[5~', 'rep'], ['PgDn', ESC + '[6~', 'rep']]
    ] }
  };

  var keys = document.getElementById('keys');
  var page = 'nav';

  function haptic() {
    // Android only; iOS Safari ignores it. Cheap when it works, harmless when
    // it does not.
    if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
  }

  function press(seq) {
    sendText(seq);
    haptic();
  }

  function buildPage(name) {
    page = name;
    keys.textContent = '';
    PAGES[name].rows.forEach(function (row) {
      var line = document.createElement('div');
      line.className = 'krow';
      row.forEach(function (entry) {
        line.appendChild(buildKey(entry[0], entry[1], entry[2]));
      });
      keys.appendChild(line);
    });
    Array.prototype.forEach.call(
      document.querySelectorAll('#tabs [data-page]'), function (tab) {
        tab.classList.toggle('on', tab.dataset.page === name);
      });
    // The pad's height changes with the page -- the keyboard is four rows and
    // nav is one -- and the terminal has to be told, or tmux keeps drawing for
    // rows that are now behind the keys.
    refit();
  }

  function buildKey(label, seq, flag) {
    var button = document.createElement('button');
    button.className = 'key' + (flag === 'wide' ? ' wide' : '');
    button.textContent = label;
    button.setAttribute('aria-label', label);
    var timer = null, interval = null;
    function fire() { press(seq); }
    // pointerdown, not click: a key should fire the moment it is touched,
    // and holding an arrow should repeat rather than needing ten taps.
    function down(event) {
      event.preventDefault();
      fire();
      button.classList.add('down');
      if (flag === 'rep') {
        timer = setTimeout(function () {
          interval = setInterval(fire, 70);
        }, 400);
      }
    }
    function up() {
      button.classList.remove('down');
      clearTimeout(timer); clearInterval(interval);
      timer = interval = null;
    }
    button.addEventListener('pointerdown', down);
    button.addEventListener('pointerup', up);
    button.addEventListener('pointercancel', up);
    button.addEventListener('pointerleave', up);
    // Stop the browser turning a held key into a text selection or a context
    // menu.
    button.addEventListener('contextmenu', function (e) {
      e.preventDefault();
    });
    return button;
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('#tabs [data-page]'), function (tab) {
      keepFocus(tab);
      tab.addEventListener('click', function (event) {
        event.preventDefault();
        buildPage(tab.dataset.page);
      });
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

  // Pinch to zoom the font. The page itself must not zoom -- a zoomed viewport
  // makes a terminal unreadable and unscrollable at once -- so this is the
  // only zoom available, and it is the one that helps.
  var pinchStart = 0, pinchFont = 0;
  host.addEventListener('touchstart', function (event) {
    if (event.touches.length === 2) {
      pinchStart = spread(event.touches);
      pinchFont = term.options.fontSize;
    }
  }, { passive: true });
  host.addEventListener('touchmove', function (event) {
    if (event.touches.length === 2 && pinchStart > 0) {
      event.preventDefault();
      setFont(pinchFont * (spread(event.touches) / pinchStart));
    }
  }, { passive: false });
  host.addEventListener('touchend', function () { pinchStart = 0; });
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
    syncViewport(); refit();
  });
  window.addEventListener('orientationchange', function () {
    // The viewport metrics are wrong until the rotation animation finishes.
    setTimeout(function () { syncViewport(); refit(); }, 300);
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
    buildPage('nav');
    setKeyboard(false);
  }

  if (boot) boot.style.display = 'none';
  connect();
  term.focus();
})();
