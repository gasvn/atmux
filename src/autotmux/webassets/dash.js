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
    button.addEventListener('click', function () {
      go(row.kind === 'session' ? 'attach' : 'shell', row);
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

  function go(verb, row) {
    var url = new URL('console/', location.href);
    // The routing name, not the label: login:zgx is for reading.
    if (verb === 'shell') {
      if (row.node) url.searchParams.set('shell', row.node);
    } else if (row.kind === 'session' && row.node && row.session) {
      url.searchParams.set(verb, row.node + ':' + row.session);
    }
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
  }

  function poll() {
    if (inflight) return;
    inflight = true;
    fetch(api('state'), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (error) {
        err.hidden = false;
        err.textContent = 'cannot reach the server — ' + error;
        age.classList.add('stale');
      })
      .then(function () { inflight = false; });
  }

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
  document.getElementById('refresh').addEventListener('click', poll);
  start();
})();
