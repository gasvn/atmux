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

  // Font size is the single biggest lever on a phone, and xterm.js has no
  // pinch-zoom of its own (issue #5377). Remember it: a size is a property of
  // the device, and re-picking it on every visit is the kind of small friction
  // that stops someone reaching for the phone at all.
  var MIN_FONT = 7, MAX_FONT = 28;
  function storedFont() {
    try {
      var v = parseFloat(localStorage.getItem('atmux.fontSize'));
      if (v >= MIN_FONT && v <= MAX_FONT) return v;
    } catch (e) {}
    return touch ? 11 : 13;
  }

  var term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    fontSize: storedFont(),
    scrollback: 5000,
    macOptionIsMeta: true,
    // A tap is a click, and atmux attaches on a single click. Without this the
    // browser waits ~300ms to see whether a second tap is coming.
    theme: { background: '#0b0b0c', foreground: '#d8d8dc' }
  });
  var fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  var host = document.getElementById('term');
  term.open(host);
  fit.fit();

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

  function connect() {
    var scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(scheme + '://' + location.host + '/ws');
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

  // ── the software keyboard is opt-in ───────────────────────────────────
  // atmux is a table you steer with arrows and act on with single letters, so
  // the on-screen keyboard is almost never what you want -- and it costs half
  // the screen. inputmode="none" keeps the textarea focused, so a *hardware*
  // keyboard still works and the cursor stays live, while the software one
  // stays down until it is asked for.
  var kbdOpen = false;
  function setKeyboard(open) {
    kbdOpen = open;
    if (textarea) textarea.setAttribute('inputmode', open ? 'text' : 'none');
    var button = document.getElementById('kbd');
    if (button) button.classList.toggle('on', open);
    if (open) {
      if (textarea) { textarea.blur(); term.focus(); }
    } else {
      term.focus();
    }
  }

  // ── the keypad ────────────────────────────────────────────────────────
  // Not a terminal keyboard. These are the keys atmux actually uses, and a
  // generic Ctrl/Esc/Tab row was the wrong tool: on this screen you navigate
  // and you act, and both are single keys.
  var ESC = '\x1b';
  var PAGES = {
    nav: [
      ['↑', ESC + '[A', 'rep'], ['↓', ESC + '[B', 'rep'],
      ['⏎ attach', '\r', 'wide'],
      ['←', ESC + '[D', 'rep'], ['→', ESC + '[C', 'rep'],
      ['esc', ESC], ['q', 'q']
    ],
    // Grouped the way the help screen groups them: connect, then session
    // lifecycle, then view.
    atmux: [
      ['s', 's'], ['o', 'o'], ['t', 't'],
      ['v', 'v'], ['e', 'e'], ['n', 'n'], ['x', 'x'],
      ['k', 'k'], ['j', 'j'], ['z', 'z'], ['g', 'g'],
      ['r', 'r'], ['?', '?']
    ],
    // Once you attach, the keys that matter are tmux's, and detach is the one
    // nobody can guess -- it is the whole reason the handover banner exists.
    tmux: [
      ['detach', '\x02d', 'wide'],
      ['^C', '\x03'], ['^D', '\x04'], ['^Z', '\x1a'],
      ['^B', '\x02'], ['tab', '\t'],
      ['PgUp', ESC + '[5~', 'rep'], ['PgDn', ESC + '[6~', 'rep']
    ]
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
    PAGES[name].forEach(function (entry) {
      var label = entry[0], seq = entry[1], flag = entry[2];
      var button = document.createElement('button');
      button.className = 'key' + (flag === 'wide' ? ' wide' : '');
      button.textContent = label;
      button.setAttribute('aria-label', label);
      // pointerdown, not click: a key should fire the moment it is touched,
      // and holding an arrow should repeat rather than needing ten taps.
      var timer = null, interval = null;
      function down(event) {
        event.preventDefault();
        press(seq);
        button.classList.add('down');
        if (flag === 'rep') {
          timer = setTimeout(function () {
            interval = setInterval(function () { press(seq); }, 70);
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
      // Stop the browser turning a held key into a text selection or a
      // context menu.
      button.addEventListener('contextmenu', function (e) {
        e.preventDefault();
      });
      keys.appendChild(button);
    });
    Array.prototype.forEach.call(
      document.querySelectorAll('#tabs [data-page]'), function (tab) {
        tab.classList.toggle('on', tab.dataset.page === name);
      });
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('#tabs [data-page]'), function (tab) {
      tab.addEventListener('click', function (event) {
        event.preventDefault();
        buildPage(tab.dataset.page);
        term.focus();
      });
    });

  // ── font size ─────────────────────────────────────────────────────────
  function setFont(size) {
    size = Math.max(MIN_FONT, Math.min(MAX_FONT, Math.round(size * 2) / 2));
    if (size === term.options.fontSize) return;
    term.options.fontSize = size;
    try { localStorage.setItem('atmux.fontSize', String(size)); } catch (e) {}
    refit();
    say(size + 'px · ' + term.cols + '×' + term.rows);
  }
  var minus = document.getElementById('fontminus');
  var plus = document.getElementById('fontplus');
  if (minus) minus.addEventListener('click', function (e) {
    e.preventDefault(); setFont(term.options.fontSize - 1);
  });
  if (plus) plus.addEventListener('click', function (e) {
    e.preventDefault(); setFont(term.options.fontSize + 1);
  });
  var kbd = document.getElementById('kbd');
  if (kbd) kbd.addEventListener('click', function (e) {
    e.preventDefault(); setKeyboard(!kbdOpen);
  });

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

  // ── sizing ────────────────────────────────────────────────────────────
  var refitTimer = null;
  function refit() {
    clearTimeout(refitTimer);
    refitTimer = setTimeout(function () {
      try { fit.fit(); } catch (e) { return; }
      sendResize();
    }, 60);
  }
  window.addEventListener('resize', refit);
  window.addEventListener('orientationchange', function () {
    setTimeout(refit, 300);
  });
  // The software keyboard resizes the visual viewport but not the layout
  // viewport, so without this the bottom rows sit behind the keyboard.
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', refit);
  }
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
