(function () {
  'use strict';
  // The reading half of atmux, as a page rather than as a terminal.
  //
  // Everything the console spent effort on is absent here rather than solved:
  // there is no character grid, so nothing quantises and nothing is left over
  // at the edge; there is no column budget, so no font arithmetic and no
  // truncated STATUS; rows are elements, so a tap selects one without xterm
  // needing touch support it does not have. What the terminal is still for is
  // attaching, and that is one tap away.

  var list = document.getElementById('list');
  var age = document.getElementById('age');
  var err = document.getElementById('err');
  var queue = document.getElementById('queue');
  var queueBody = document.getElementById('queue-body');
  var queueTitle = document.getElementById('queue-title');

  // Matches the daemon's own cadence. Faster would ask the clusters for a
  // state they have not recomputed; slower would show a phone something the
  // laptop beside it has already moved past.
  var POLL_MS = 5000;
  var timer = null, inflight = false;

  function api(name) {
    // Relative, so the page works under whatever path it is mounted at --
    // `tailscale serve --set-path /term` is the reason the console needed
    // this and the reason this needs it too.
    return new URL('api/' + name, location.href).toString();
  }

  function fmtAge(seconds) {
    if (seconds === null || seconds === undefined) return 'connecting…';
    if (seconds < 10) return 'just now';
    if (seconds < 90) return Math.round(seconds) + 's ago';
    return Math.round(seconds / 60) + 'm ago';
  }

  // The tiers the model actually names: '' (busy), 'idle', 'stale'. Written
  // against the values rather than against what they might be called -- a
  // client guessing at an enum shows every session the same colour and does
  // it silently.
  var TIERS = { '': 'live', idle: 'hint', stale: 'stale' };

  function tierClass(row) {
    if (row.kind !== 'session') return 'none';
    return TIERS[row.tier] || 'live';
  }

  function build(row) {
    var item = document.createElement('li');
    var button = document.createElement('button');
    button.className = 'row';
    button.type = 'button';

    var dot = document.createElement('span');
    dot.className = 'dot ' + tierClass(row);
    button.appendChild(dot);

    var name = document.createElement('div');
    name.className = 'name';
    if (row.kind === 'session') {
      name.textContent = row.label;
    } else {
      var span = document.createElement('span');
      span.className = 'placeholder';
      span.textContent = row.label;
      name.appendChild(span);
    }
    if (row.keepalive) {
      var tag = document.createElement('span');
      tag.className = 'tag' + (row.keepalive === 'healthy' ? ''
                               : ' ' + row.keepalive);
      tag.textContent = row.keepalive === 'renewing' ? 'renewing'
                      : row.keepalive === 'paused' ? 'renew paused'
                      : 'auto-renew';
      name.appendChild(tag);
    }
    button.appendChild(name);

    var meta = document.createElement('div');
    meta.className = 'meta';
    // The node first: on a phone it is the thing that tells two identically
    // named sessions apart.
    meta.textContent = [row.node_label, row.idle_label, row.status]
      .filter(Boolean).join(' · ');
    button.appendChild(meta);

    var right = document.createElement('div');
    right.className = 'right';
    if (row.left && row.left !== '-') {
      var left = document.createElement('b');
      left.textContent = row.left;
      right.appendChild(left);
    }
    if (row.load) right.appendChild(document.createTextNode(
      row.load + (row.cpu ? '/' + row.cpu : '')));
    button.appendChild(right);

    // Land *on* the session, not on a second copy of this list. A tap that
    // opens the dashboard is a screen that costs a tap and answers nothing,
    // which is exactly what it did before the target rode along.
    // A row with no session yet cannot be attached to -- there is nothing
    // there. Starting one on that machine is what you actually want, and it
    // is the same one tap.
    button.addEventListener('click', function (event) {
      // A hold has already acted; the click the browser sends afterwards is
      // not a second instruction.
      if (Date.now() - actedAt < 700) { event.preventDefault(); return; }
      go(row.kind === 'session' ? 'attach' : 'shell', row);
    });
    // Hold to act on this row without leaving the list.
    button.addEventListener('touchstart', function (event) {
      armHold(event, row);
    }, {passive: true});
    button.addEventListener('touchmove', moveHold, {passive: true});
    button.addEventListener('touchend', endHold);
    button.addEventListener('touchcancel', cancelHold);
    // And a mouse, so the same actions exist on a laptop.
    button.addEventListener('contextmenu', function (event) {
      event.preventDefault();
      openSheet(row);
    });
    item.appendChild(button);

    // The other verb. Everything the dashboard can do -- renew, kill, note,
    // view output, ssh, new window -- acts on the highlighted row, so opening
    // it *on* this row makes all of them reachable rather than adding a
    // button here for each and a flag over there for each.
    // ⋯ only where there is a session to act on. A machine with none has
    // exactly one thing you can do to it, and the row itself does that.
    if (row.kind === 'session') {
      var more = document.createElement('button');
      more.className = 'more';
      more.type = 'button';
      more.textContent = '⋯';
      more.setAttribute('aria-label', 'actions for ' + row.label);
      more.addEventListener('click', function (event) {
        event.stopPropagation();
        go('select', row);
      });
      item.appendChild(more);
    }
    return item;
  }

  // ── acting on a row without leaving the list ──────────────────────────
  // Everything used to route through the console: `⋯` opened the terminal
  // standing on the row, and you did the thing there. That is the right
  // answer for the twenty actions the TUI has and the wrong one for the two
  // or three you reach for on a phone, where it costs a page load, a shell,
  // and the way back.
  //
  // The gesture is the one the console settled on, for the same reasons:
  // our own timer rather than the platform's recogniser, movement measured
  // from where the finger went down, and the sheet opening on the lift so
  // that a scroll starting late still cancels it.
  var HOLD_MS = 500, HOLD_SLOP = 8;
  var holdTimer = 0, holdFrom = null, holdArmed = null;
  // Set when a hold has just acted. preventDefault on touchend suppresses the
  // synthetic click on every browser that honours it, and this is the belt:
  // `holdArmed` is cleared by the time the click would arrive, so the guard
  // cannot read it.
  var actedAt = 0;
  var sheet = document.getElementById('sheet');
  var sheetTitle = document.getElementById('sheettitle');
  var sheetActs = document.getElementById('sheetacts');

  function cancelHold() {
    if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; }
    holdFrom = null; holdArmed = null;
  }

  function armHold(event, row) {
    if (event.touches && event.touches.length !== 1) return cancelHold();
    var touch = event.touches ? event.touches[0] : event;
    cancelHold();
    holdFrom = {x: touch.clientX, y: touch.clientY};
    holdTimer = setTimeout(function () {
      holdTimer = 0;
      holdArmed = row;
      if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
    }, HOLD_MS);
  }

  function moveHold(event) {
    if (!holdFrom) return;
    var touch = event.touches ? event.touches[0] : event;
    if (Math.abs(touch.clientX - holdFrom.x) > HOLD_SLOP ||
        Math.abs(touch.clientY - holdFrom.y) > HOLD_SLOP) {
      cancelHold();
    }
  }

  function endHold(event) {
    var row = holdArmed;
    cancelHold();
    if (!row) return false;
    // The tap that would otherwise follow belongs to the hold.
    if (event && event.cancelable) event.preventDefault();
    actedAt = Date.now();
    openSheet(row);
    return true;
  }

  function openSheet(row) {
    var where = row.node_label || row.node;
    sheetTitle.textContent = row.kind === 'session'
      ? row.label + '  ·  ' + where : where;
    sheetActs.textContent = '';
    var acts = row.kind === 'session'
      ? [['attach', 'Open in terminal', ''],
         ['window', 'New window in this session', ''],
         ['new', 'New session on ' + where, ''],
         ['kill', 'Kill ' + row.label, 'danger']]
      : [['shell', 'Open a shell here', ''],
         ['new', 'New session on ' + where, '']];
    acts.forEach(function (spec) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'act' + (spec[2] ? ' ' + spec[2] : '');
      button.textContent = spec[1];
      button.addEventListener('click', function () { runAct(spec[0], row); });
      sheetActs.appendChild(button);
    });
    sheet.hidden = false;
  }

  function closeSheet() { sheet.hidden = true; }

  function runAct(act, row) {
    if (act === 'attach' || act === 'shell') {
      closeSheet();
      go(act, row);
      return;
    }
    if (act === 'kill') {
      // Asked once, in the sheet rather than through a confirm() the page
      // cannot style and iOS renders as a system dialog over everything.
      // Nothing else here is irreversible, so nothing else asks.
      sheetTitle.textContent = 'Kill ' + row.label + ' on '
                             + (row.node_label || row.node) + '?'
                             + '  Anything running in it stops.';
      sheetActs.textContent = '';
      var yes = document.createElement('button');
      yes.type = 'button';
      yes.className = 'act danger';
      yes.textContent = 'Yes, kill it';
      yes.addEventListener('click', function () { send('kill', row); });
      sheetActs.appendChild(yes);
      return;
    }
    if (act === 'new') {
      var name = window.prompt('Name for the new session on '
                               + (row.node_label || row.node));
      if (!name) return;
      send('new', row, name.trim());
      return;
    }
    send(act, row);
  }

  function send(verb, row, session) {
    var target = session || row.session;
    var buttons = sheetActs.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) buttons[i].disabled = true;
    sheetTitle.textContent = 'working…';
    fetch(api('session'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node: row.node, verb: verb, session: target}),
    }).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (answer) {
        closeSheet();
        if (!answer || !answer.ok) {
          err.hidden = false;
          err.textContent = (answer && answer.reason)
            || 'the ' + verb + ' did not go through';
          return;
        }
        err.hidden = true;
        // Straight away rather than on the next tick of the poll: the
        // reason you pressed it was to change this list.
        poll();
      })
      .catch(function (error) {
        closeSheet();
        err.hidden = false;
        err.textContent = 'could not reach the server — ' + error;
      });
  }

  sheet.addEventListener('click', function (event) {
    // The backdrop and Cancel both dismiss. A sheet with only one way out is
    // a sheet people back out of with the browser, which leaves the page.
    if (event.target === sheet ||
        (event.target.dataset && event.target.dataset.act === 'cancel')) {
      closeSheet();
    }
  });

  function go(verb, row) {
    var url = new URL('console/', location.href);
    // The routing name, not the label: login:zgx is for reading.
    if (verb === 'shell') {
      if (row.node) url.searchParams.set('shell', row.node);
    } else if (row.kind === 'session' && row.node && row.session) {
      url.searchParams.set(verb, row.node + ':' + row.session);
    }
    // Carried, because this is the one navigation between the list and the
    // terminal: a readout you can only reach by typing ?attach=NODE:SESSION
    // by hand on a phone is a readout for the window nobody is debugging.
    if (/[?&]debug=1/.test(location.search)) url.searchParams.set('debug', '1');
    location.href = url.toString();
  }

  function render(data) {
    err.hidden = !data.error;
    if (data.error) err.textContent = data.error;

    age.textContent = fmtAge(data.age);
    age.classList.toggle('stale', !!data.stale);

    var rows = data.sessions || [];
    list.textContent = '';
    if (!rows.length) {
      var empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = data.age === null ? 'connecting…' : 'no sessions';
      list.appendChild(empty);
    } else {
      rows.forEach(function (row) { list.appendChild(build(row)); });
    }

    var q = data.queue || {};
    var text = (q.long || '').trim();
    queue.hidden = !text;
    if (text) {
      queueBody.textContent = text;
      queueTitle.textContent = q.updated ? 'queue · ' + q.updated : 'queue';
    }

    checkBuild(data.build);
  }

  // ── which build this is ─────────────────────────────────────────────────
  // Both pages declare apple-mobile-web-app-capable, so on a phone they are
  // home-screen apps, and iOS resumes one of those from a snapshot rather
  // than fetching it again. A deploy can land, be served correctly, and never
  // reach the screen -- with nothing on the page to say which of the two it
  // is. That is the failure this reports: not an error, just an age.
  var buildBar = document.getElementById('build');
  var updateBtn = document.getElementById('update');
  var meta = document.querySelector('meta[name="atmux-build"]');
  var MINE = meta ? meta.content : '';

  if (buildBar) buildBar.textContent = MINE ? 'build ' + MINE : '';

  function checkBuild(theirs) {
    if (!updateBtn || !MINE || !theirs) return;
    var stale = theirs !== MINE;
    updateBtn.hidden = !stale;
    if (stale) {
      updateBtn.textContent = 'a newer build is on the server — tap to load '
                            + 'it  (' + MINE + ' → ' + theirs + ')';
      if (buildBar) buildBar.textContent = 'build ' + MINE + ' · server '
                                         + theirs;
    }
  }

  if (updateBtn) {
    updateBtn.addEventListener('click', function () {
      updateBtn.textContent = 'loading…';
      // Plain reload, deliberately: our own assets go out `no-store`, so
      // there is no cache entry to defeat -- the copy being replaced is the
      // running document, not something a header could have prevented.
      location.reload();
    });
  }

  function poll() {
    if (inflight) return Promise.resolve();
    inflight = true;
    return fetch(api('state'), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (error) {
        err.hidden = false;
        err.textContent = 'cannot reach the server — ' + error;
        age.classList.add('stale');
      })
      .then(function () { inflight = false; });
  }

  // ── pull to refresh ─────────────────────────────────────────────────────
  // The gesture a phone reaches for first. The page had a button for it in a
  // sticky bar instead -- 70px of screen, permanently, for something the
  // five-second poll usually did before you asked -- and actively refused the
  // gesture, because overscroll-behavior-y: contain turns the browser's own
  // off. So it is ours to draw.
  //
  // Only from the very top, and only downward: anywhere else a vertical drag
  // is the list scrolling, and stealing that would be far worse than not
  // having this at all.
  var pull = document.getElementById('pull');
  var PULL_SLOP = 8;         // below this a touch is still a tap
  var TRIGGER = 64;          // finger travel before letting go means anything
  var MAX_PULL = 96;         // past here the strip stops following
  var FOLLOW = 0.5;          // how far the strip moves per pixel of finger
  var pullStart = -1, pulling = false, refreshing = false;

  // What a downward drag of `dy` from the top of the page means. Pure, and
  // separate from the listeners, because this is the whole of the decision
  // and everything around it is plumbing: null means "not a pull, leave the
  // page alone", which is what keeps a tap on a session row a tap.
  function pullState(dy) {
    if (dy <= 0) return { height: 0, state: '' };
    if (dy < PULL_SLOP) return null;
    var shown = Math.min(MAX_PULL, dy * FOLLOW);
    return { height: shown, state: dy >= TRIGGER ? 'armed' : 'pull' };
  }

  function setPull(height, state) {
    if (!pull) return;
    pull.style.height = Math.round(height) + 'px';
    pull.textContent = state === 'busy' ? 'refreshing…'
                     : state === 'armed' ? 'release to refresh'
                     : state === 'pull' ? 'pull to refresh' : '';
    pull.classList.toggle('armed', state === 'armed');
    pull.classList.toggle('busy', state === 'busy');
  }

  function endPull() {
    pulling = false;
    pullStart = -1;
    if (pull) pull.classList.remove('dragging');
  }

  document.addEventListener('touchstart', function (event) {
    if (refreshing || event.touches.length !== 1) return;
    // scrollY, not the list's own scrollTop: the whole page scrolls here.
    pullStart = window.scrollY <= 0 ? event.touches[0].clientY : -1;
  }, { passive: true });

  document.addEventListener('touchmove', function (event) {
    if (pullStart < 0 || refreshing) return;
    var next = pullState(event.touches[0].clientY - pullStart);
    if (next === null) return;            // still inside the slop: a tap
    if (!next.state) {
      // Turned into an upward scroll: hand it back rather than keep a strip
      // open above a list the reader is trying to move.
      if (pulling) { setPull(0, ''); endPull(); }
      pullStart = -1;
      return;
    }
    if (!pulling) {
      pulling = true;
      if (pull) pull.classList.add('dragging');
    }
    setPull(next.height, next.state);
  }, { passive: true });

  document.addEventListener('touchend', function () {
    if (!pulling) { pullStart = -1; return; }
    var armed = pull && pull.classList.contains('armed');
    endPull();
    if (!armed) { setPull(0, ''); return; }
    refreshing = true;
    setPull(36, 'busy');
    var settled = Date.now();
    poll().then(function () {
      // Held briefly if the answer came back instantly. A strip that appears
      // and vanishes in the same frame reads as nothing having happened.
      var wait = Math.max(0, 350 - (Date.now() - settled));
      setTimeout(function () {
        refreshing = false;
        setPull(0, '');
      }, wait);
    });
  }, { passive: true });

  document.addEventListener('touchcancel', function () {
    if (pulling) setPull(0, '');
    endPull();
  }, { passive: true });

  function start() {
    poll();
    clearInterval(timer);
    timer = setInterval(poll, POLL_MS);
  }

  // A phone spends most of its time with the screen off. Polling a server
  // nobody is looking at is the kind of background traffic that gets an app
  // uninstalled; polling only while visible costs one fetch on wake.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) clearInterval(timer);
    else start();
  });
  start();
})();
