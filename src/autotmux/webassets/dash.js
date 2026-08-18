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
    // A machine nothing can reach is not the same grey as a machine you
    // could start something on. It was, and the two sat side by side.
    if (row.kind === 'offline') return 'stale';
    if (row.kind !== 'session') return 'none';
    return TIERS[row.tier] || 'live';
  }

  // ── how busy the machine is ────────────────────────────────────────────
  // These two numbers used to read `38.42/64`: a load average over a core
  // count. True, and nothing anyone can read at a glance -- which is what
  // "后面那些数字是什么，看不懂" was about. The table beside this page says
  // `cpu 60%`, so this says `cpu 60%`.

  function share(load, cpu) {
    var used = parseFloat(load), have = parseFloat(cpu);
    if (!isFinite(used) || !isFinite(have) || have <= 0) return null;
    // A load average counts runnable processes, so it can exceed the cores
    // by a lot on a wedged node. Clamped for width, not for truth: past
    // 999% the exact figure has stopped being the point.
    return Math.max(0, Math.min(999, Math.round(used / have * 100)));
  }

  // "mean-util used total count" -- the daemon's own field order. Only the
  // first is a percentage; the memory figures are megabytes.
  function gpuShare(gpu) {
    var util = parseFloat(String(gpu || '').trim().split(/\s+/)[0]);
    if (!isFinite(util)) return null;
    return Math.max(0, Math.min(100, Math.round(util)));
  }

  // One colour, and only where it is a problem. The first draft lit both
  // numbers amber above 60% and the result was a list whose loudest thing
  // was the hardware: a GPU at 87% is a job running *well*, and painting it
  // the colour of a warning says the opposite. Past 100% the load exceeds
  // the cores, which is the one state here that costs the reader something.
  function level(pct) { return pct >= 100 ? ' hot' : ''; }

  function paintRail(rail, row) {
    rail.textContent = '';
    // The GPU has been in the payload since the walltime came back, and the
    // model's own comment says the browser list draws the same rail the
    // table does. It did not: the phone was the one screen with no GPU on it.
    var cpu = share(row.load, row.cpu);
    [['cpu', cpu, level(cpu)],
     ['gpu', gpuShare(row.gpu), '']].forEach(function (pair) {
      if (pair[1] === null) return;
      var chip = document.createElement('span');
      chip.className = 'chip' + pair[2];
      chip.textContent = pair[0] + ' ' + pair[1] + '%';
      rail.appendChild(chip);
    });
  }

  // ── the list, updated rather than rebuilt ──────────────────────────────
  // It used to be `list.textContent = ''` and a fresh <li> per row, every
  // five seconds. Measured in WebKit at an iPhone's size: every row element
  // replaced, every tick. A finger already down was then holding something
  // that had left the document -- the hold's highlight never arrived and the
  // sheet opened over an unlit list -- and a tap whose click had not yet
  // fired was lost outright. A 500ms hold against a 5s poll is one hold in
  // ten; a tap is one in fifty.
  //
  // So a row is keyed by what it is about and kept. The poll writes the
  // changed fields into the element that is already there, which is also
  // what makes the press state, the focus ring and the scroll position
  // survive a refresh.
  var kept = new Map();

  function keyOf(row) {
    // A machine's offer row and a session of the same name on it are
    // different rows; the kind is what tells them apart. A space joins them
    // safely because only the last field can contain one: a tmux session
    // may be called "my run", a hostname and a kind may not.
    return [row.node, row.kind, row.session || ''].join(' ');
  }

  // Distinct from every row key for the same reason: `kind` is one of
  // session/empty/offline, so no row key's second word is ever a band's
  // first.
  function bandKey(title) { return 'band ' + title; }

  // What the list should be, in order, headings included -- a heading that
  // is not part of the plan is a heading nothing keeps in place.
  function planList(rows) {
    var plan = [], band = null;
    rows.forEach(function (row) {
      var title = row.band || '';
      if (title && title !== band) {
        band = title;
        plan.push({key: bandKey(title), band: title});
      }
      plan.push({key: keyOf(row), row: row});
    });
    return plan;
  }

  function makeBand(title) {
    var item = document.createElement('li');
    item.className = 'band';
    item.textContent = title;
    return {li: item};
  }

  function makeRow() {
    var entry = {li: document.createElement('li'), row: null};
    var button = document.createElement('button');
    button.className = 'row';
    button.type = 'button';

    entry.dot = document.createElement('span');
    button.appendChild(entry.dot);

    entry.name = document.createElement('div');
    entry.name.className = 'name';
    button.appendChild(entry.name);

    entry.meta = document.createElement('div');
    entry.meta.className = 'meta';
    button.appendChild(entry.meta);

    entry.right = document.createElement('div');
    entry.right.className = 'right';
    entry.wall = document.createElement('b');
    entry.rail = document.createElement('span');
    entry.rail.className = 'rail';
    entry.right.appendChild(entry.wall);
    entry.right.appendChild(entry.rail);
    button.appendChild(entry.right);

    // Every handler reads entry.row rather than a row captured when the
    // element was made. The element now outlives any single poll, so a
    // closure over the row it was built from would act on a session that
    // has since gone quiet, moved band, or gone away.
    //
    // Land *on* the session, not on a second copy of this list. A row with
    // no session yet cannot be attached to -- starting one there is what you
    // actually want, and it is the same one tap.
    button.addEventListener('click', function (event) {
      // A hold has already acted; the click the browser sends afterwards is
      // not a second instruction.
      if (Date.now() - actedAt < 700) { event.preventDefault(); return; }
      go(entry.row.kind === 'session' ? 'attach' : 'shell', entry.row);
    });
    // Hold to act on this row without leaving the list.
    button.addEventListener('touchstart', function (event) {
      armHold(event, entry.row);
    }, {passive: true});
    button.addEventListener('touchmove', moveHold, {passive: true});
    button.addEventListener('touchend', function (event) {
      endHold(event);
      flush();
    });
    button.addEventListener('touchcancel', function () {
      cancelHold();
      flush();
    });
    // And a mouse, so the same actions exist on a laptop.
    button.addEventListener('contextmenu', function (event) {
      event.preventDefault();
      openSheet(entry.row);
    });
    entry.li.appendChild(button);

    // The other verb. Everything the dashboard can do -- renew, kill, note,
    // view output, ssh, new window -- acts on the highlighted row, so
    // opening it *on* this row makes all of them reachable.
    //
    // On every row, not only on sessions. The sheet has three things to
    // offer a machine with nothing running on it, and a column that exists
    // on some rows and not others left the numbers down the right of the
    // list ending 56px apart.
    entry.more = document.createElement('button');
    entry.more.className = 'more';
    entry.more.type = 'button';
    entry.more.textContent = '⋯';
    entry.more.setAttribute('aria-haspopup', 'dialog');
    entry.more.addEventListener('click', function (event) {
      event.stopPropagation();
      openSheet(entry.row);
    });
    entry.li.appendChild(entry.more);
    return entry;
  }

  function paint(entry, row) {
    entry.row = row;
    entry.dot.className = 'dot ' + tierClass(row);

    // A session is named by its name; a machine you could start something on
    // is named by the machine. `<shell>` is the model's sentinel for the
    // second, and four rows of it was this page showing an internal name to
    // the reader and calling it a list.
    var title = row.kind === 'session' ? row.label : row.node_label;
    entry.name.textContent = title;
    if (row.keepalive) {
      var tag = document.createElement('span');
      tag.className = 'tag' + (row.keepalive === 'healthy' ? ''
                               : ' ' + row.keepalive);
      tag.textContent = row.keepalive === 'renewing' ? 'renewing'
                      : row.keepalive === 'paused' ? 'renew paused'
                      : 'auto-renew';
      entry.name.appendChild(tag);
    }

    // The node first: on a phone it is the thing that tells two identically
    // named sessions apart. On a row named for the machine it would be the
    // name twice.
    // 'Active' is what a session's status says when nothing is wrong, which
    // is almost always -- a word on every row that never varies, wrapping
    // the line it is on. The green dot already says it. What is left is the
    // statuses that mean something: DEGRADED, OFFLINE, No sessions.
    var status = row.status === 'Active' ? '' : row.status;
    entry.meta.textContent = (row.kind === 'session'
      ? [row.node_label, row.idle_label, status]
      : [status]).filter(Boolean).join(' · ');

    entry.wall.textContent = (row.left && row.left !== '-') ? row.left : '';
    paintRail(entry.rail, row);
    entry.more.setAttribute('aria-label', 'actions for ' + title);
  }

  function syncList(rows, emptyText) {
    if (!rows.length) {
      kept.clear();
      list.textContent = '';
      var blank = document.createElement('li');
      blank.className = 'empty';
      blank.textContent = emptyText;
      list.appendChild(blank);
      return;
    }
    var want = {};
    var at = list.firstChild;
    planList(rows).forEach(function (item) {
      want[item.key] = true;
      var entry = kept.get(item.key);
      if (!entry) {
        entry = item.band ? makeBand(item.band) : makeRow();
        kept.set(item.key, entry);
      }
      if (item.row) paint(entry, item.row);
      // Already in the right place, or moved there. insertBefore on an
      // element that is already in the document moves it and keeps it --
      // its listeners, its classes, and any gesture in progress on it.
      if (entry.li === at) at = at.nextSibling;
      else list.insertBefore(entry.li, at);
    });
    // Everything the plan did not claim now sits after it. Swept before the
    // map is pruned, so nothing is asked to remove a node twice.
    while (at) {
      var next = at.nextSibling;
      list.removeChild(at);
      at = next;
    }
    kept.forEach(function (entry, key) {
      if (!want[key]) kept.delete(key);
    });
  }

  // A finger is down on a row, or the sheet is standing on one. Nothing in
  // the list may move until neither is true: a row that reorders under a
  // thumb is a row you meant to press and did not.
  function busy() {
    return holdEl !== null || holdArmed !== null || !sheet.hidden;
  }

  var pending = null;

  function flush() {
    if (!pending || busy()) return;
    var held = pending;
    pending = null;
    syncList(held.rows, held.empty);
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
  var holdEl = null, liftedEl = null;
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
    if (holdEl) { holdEl.classList.remove('holding'); holdEl = null; }
    holdFrom = null; holdArmed = null;
  }

  function armHold(event, row) {
    if (event.touches && event.touches.length !== 1) return cancelHold();
    var touch = event.touches ? event.touches[0] : event;
    cancelHold();
    holdFrom = {x: touch.clientX, y: touch.clientY};
    // Immediately, so the half second is visibly a hold rather than a tap
    // that missed. Cheap to undo: a scroll clears it on the first move.
    holdEl = event.currentTarget;
    if (holdEl) holdEl.classList.add('holding');
    holdTimer = setTimeout(function () {
      holdTimer = 0;
      holdArmed = row;
      // The buzz and the change of state on the same frame. Apple's own
      // guidance is that latency between the two destroys the illusion, and
      // it is right: felt before it is seen, it reads as a glitch.
      if (holdEl) {
        holdEl.classList.remove('holding');
        holdEl.classList.add('lifted');
        liftedEl = holdEl;
      }
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

  // Where the focus was before the sheet took it, so it can be put back.
  var sheetOpener = null;
  var sheetCard = document.getElementById('sheetcard');

  function focusFirst() {
    var first = sheetActs.querySelector(
      'input, button:not([disabled])');
    if (first) { try { first.focus(); } catch (e) {} }
  }

  function openSheet(row) {
    sheetOpener = document.activeElement;
    var where = row.node_label || row.node;
    sheetTitle.textContent = row.kind === 'session'
      ? row.label + '  ·  ' + where : where;
    sheetActs.textContent = '';
    var acts = row.kind === 'session'
      ? [['attach', 'Open in terminal', ''],
         ['window', 'New window in this session', ''],
         ['new', 'New session on ' + where, ''],
         ['kill', 'Kill ' + row.label, 'danger'],
         // Everything else the dashboard can do -- note, auto-renew, view
         // output, ssh -- lives in the TUI and acts on the highlighted row,
         // so this opens it standing here rather than listing twenty verbs
         // twice.
         ['select', 'More actions…', '']]
      : [['shell', 'Open a shell here', ''],
         ['new', 'New session on ' + where, ''],
         ['select', 'More actions…', '']];
    acts.forEach(function (spec) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'act' + (spec[2] ? ' ' + spec[2] : '');
      button.textContent = spec[1];
      button.addEventListener('click', function () { runAct(spec[0], row); });
      sheetActs.appendChild(button);
    });
    sheet.hidden = false;
    // Two frames, not one: the browser has to lay the card out at
    // translateY(100%) before the class that moves it can be a transition
    // rather than a jump.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        sheet.classList.add('in');
        // After it has started arriving, not before: focusing an element in
        // a card still sitting at translateY(100%) asks the browser to
        // scroll to somewhere off the bottom of the screen.
        focusFirst();
      });
    });
  }

  function closeSheet() {
    if (liftedEl) { liftedEl.classList.remove('lifted'); liftedEl = null; }
    if (sheet.hidden) return;
    sheet.classList.remove('in');
    // Hidden after it has left, or it vanishes instead of leaving. The
    // timeout matches the longest transition on the card and is a backstop
    // for the reduced-motion case, where there is no transitionend at all.
    var done = false;
    var finish = function () {
      if (done) return;
      done = true;
      sheet.hidden = true;
      // Back where it came from. A dialog that drops the focus on the floor
      // leaves the next Tab starting from the top of the document, which on
      // a list this long is a long way from the row you were just on.
      if (sheetOpener && sheetOpener.focus && document.contains(sheetOpener)) {
        try { sheetOpener.focus(); } catch (e) {}
      }
      sheetOpener = null;
      // Whatever the poll had to hold back while this was open.
      flush();
    };
    sheetCard.addEventListener('transitionend', finish, {once: true});
    setTimeout(finish, 400);
  }

  // ── the keyboard half of the sheet ────────────────────────────────────
  // There were no key listeners on this page at all, while the card
  // declared aria-modal="true" -- a claim that the rest of the document is
  // inert, made to a screen reader, and honoured by nothing. On a laptop the
  // sheet opens with a right-click and could not be closed with Escape.
  document.addEventListener('keydown', function (event) {
    if (sheet.hidden) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeSheet();
      return;
    }
    if (event.key !== 'Tab') return;
    var stops = sheetCard.querySelectorAll('input, button:not([disabled])');
    if (!stops.length) return;
    // Every Tab while the card is open, not only the two at the ends.
    //
    // Catching the ends is the usual recipe and it does not hold here: Safari
    // does not put buttons in the tab order at all unless the reader has
    // turned that on, so the focus never *reaches* the last stop to be caught
    // there -- measured, the first Tab left the card entirely. Taking the key
    // over is deterministic on every browser, and costs nothing: the card's
    // DOM order is its reading order, so this walks the same ring the
    // browser would have.
    var here = Array.prototype.indexOf.call(stops, document.activeElement);
    var next = nextStop(stops.length, here, event.shiftKey);
    if (next < 0) return;
    event.preventDefault();
    stops[next].focus();
  });

  // Which stop a Tab moves to. Pure, and separate, because the wrap is the
  // part that can be wrong: from outside the card it enters at the near end,
  // and from either end it comes round rather than leaving.
  function nextStop(count, here, back) {
    if (count < 1) return -1;
    if (here < 0) return back ? count - 1 : 0;
    return (here + (back ? -1 : 1) + count) % count;
  }

  // The keyboard covers the bottom of the screen and this card lives at the
  // bottom of the screen. `position: fixed` is laid out against the layout
  // viewport, which iOS does not shrink when the keyboard comes up, so the
  // name field would open underneath it. The visual viewport is the one that
  // knows.
  (function trackKeyboard() {
    var view = window.visualViewport;
    if (!view) return;
    var lift = function () {
      var covered = Math.max(
        0, window.innerHeight - view.height - view.offsetTop);
      sheet.style.paddingBottom = Math.round(covered) + 'px';
    };
    view.addEventListener('resize', lift);
    view.addEventListener('scroll', lift);
    lift();
  })();

  function runAct(act, row) {
    if (act === 'attach' || act === 'shell' || act === 'select') {
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
      askName(row);
      return;
    }
    send(act, row);
  }

  // In the sheet, for the reason the kill confirm above is in the sheet:
  // iOS draws a prompt() as a system dialog over everything, in a typeface
  // and a wording this page does not choose, and it arrives while the card
  // is still animating. `new` reached straight for one anyway.
  function askName(row) {
    var where = row.node_label || row.node;
    sheetTitle.textContent = 'New session on ' + where;
    sheetActs.textContent = '';

    var field = document.createElement('input');
    field.type = 'text';
    field.className = 'field';
    field.placeholder = 'name';
    // A phone's keyboard will otherwise capitalise the first letter and
    // offer to correct a session name to an English word.
    field.autocapitalize = 'none';
    field.autocomplete = 'off';
    field.spellcheck = false;
    field.enterKeyHint = 'go';
    field.setAttribute('aria-label', 'name for the new session');

    var create = document.createElement('button');
    create.type = 'button';
    create.className = 'act';
    create.textContent = 'Create';
    create.disabled = true;

    // What is allowed is the daemon's decision -- it is the one that has to
    // put this on a command line, and a second opinion here would be a
    // second place to keep right. This only refuses the empty name, which
    // is the one the daemon would answer with an error nobody needs to see.
    var submit = function () {
      var name = field.value.trim();
      if (name) send('new', row, name);
    };
    field.addEventListener('input', function () {
      create.disabled = !field.value.trim();
    });
    field.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      submit();
    });
    create.addEventListener('click', submit);

    sheetActs.appendChild(field);
    sheetActs.appendChild(create);
    field.focus();
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
          showError((answer && answer.reason)
                    || 'the ' + verb + ' did not go through', true);
          return;
        }
        clearError();
        // Straight away rather than on the next tick of the poll: the
        // reason you pressed it was to change this list.
        poll();
      })
      .catch(function (error) {
        closeSheet();
        showError('could not reach the server — ' + error, true);
      });
  }

  // ── what the banner is saying, and for how long ───────────────────────
  // render() used to clear it unconditionally, so a kill that came back
  // "no such session" said why for at most five seconds -- and for none at
  // all when a poll was already in flight when the answer landed. That is
  // indistinguishable from the button having done nothing.
  //
  // A message about the server's own state still comes and goes with that
  // state. A message about something *you* pressed stays until you have
  // acknowledged it or replaced it.
  var errHeld = false;

  function showError(text, held) {
    err.textContent = text;
    if (held) {
      var hint = document.createElement('span');
      hint.className = 'dismiss';
      hint.textContent = 'tap to dismiss';
      err.appendChild(hint);
    }
    err.hidden = false;
    errHeld = !!held;
  }

  function clearError() {
    err.hidden = true;
    err.textContent = '';
    errHeld = false;
  }

  err.addEventListener('click', clearError);

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
    if (data.error) showError(data.error, false);
    else if (!errHeld) clearError();

    age.textContent = fmtAge(data.age);
    age.classList.toggle('stale', !!data.stale);

    var rows = data.sessions || [];
    var empty = data.age === null ? 'connecting…' : 'no sessions';
    // The age and any error still land: they are not under anyone's finger.
    // The list waits, and flush() applies it the moment the gesture is over.
    if (busy()) pending = {rows: rows, empty: empty};
    else syncList(rows, empty);

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
        // Not held: this one is about the connection, and it goes away by
        // itself the moment the next poll gets through.
        showError('cannot reach the server — ' + error, false);
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
