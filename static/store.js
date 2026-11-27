// The page store: blocks from the SSE stream, pending commands, UI state. No library, no build step.
// store.subscribe(keys, fn): keys are "block:<name>", "block:*", "commands", "log", "pending", "ui", "conn";
// fn runs at most once per animation frame, in subscription order.
const store = (() => {
  const blocks = {};     // name -> {name, rev, data, updated_at, period, error}
  const pending = {};    // id -> {id, route, body, key, value, sentAt, deadlineAt, seenRunning}
  const commands = {};   // id -> last command event
  const logs = [];       // {rev, at, text}
  const ui = { harness: 'all', search: '', selected: new Set(), expanded: new Set(), collapsed: new Set(), collapsedGroups: new Set() };
  const conn = { state: 'connecting', boot: null, lastAt: 0 };
  const subs = [];
  const dirty = new Set();
  let scheduled = false;
  let logRev = 0;
  let snapRunning = new Set();  // ids the current stream connection has reported as running

  function flush() {
    scheduled = false;
    const keys = [...dirty];
    dirty.clear();
    subs.forEach(s => {
      if (!keys.some(k => s.keys.has(k) || (k.startsWith('block:') && s.keys.has('block:*')))) return;
      try { s.fn(); } catch (e) { console.error(e); }
    });
  }

  function touch(key) {
    dirty.add(key);
    if (!scheduled) { scheduled = true; requestAnimationFrame(flush); }
  }

  function subscribe(keys, fn) {
    const s = { keys: new Set(keys), fn };
    subs.push(s);
    return () => { const i = subs.indexOf(s); if (i >= 0) subs.splice(i, 1); };
  }

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();  // only in secure contexts; plain http over the tailnet is not one
    const b = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  }

  function fail(id, message) {
    if (pending[id]) { delete pending[id]; touch('pending'); }
    toast(message);
  }

  // The page shows the new value and a loader at click time and clears it on the terminal event of this id.
  function command(route, body, opts) {
    const id = uuid();
    const now = Date.now();
    const o = opts || {};
    const ui = o.key ? { key: o.key, value: o.value } : undefined;  // lets a reloaded page rebuild this pending entry
    pending[id] = { id, route, body: body || {}, key: o.key || null, value: o.value, sentAt: now, deadlineAt: now + 60000, seenRunning: false };
    touch('pending');
    fetch(route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
      body: JSON.stringify({ ...(body || {}), id, ui })
    }).then(async res => {
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        fail(id, `${route} error: ${err.error || res.status}`);
      }
    }).catch(e => fail(id, `Request failed: ${e}`));
    return id;
  }

  function pendingFor(key) {
    return Object.values(pending).filter(p => p.key === key).sort((a, b) => b.sentAt - a.sentAt)[0];
  }

  function setUI(patch) {
    Object.assign(ui, patch);
    touch('ui');
  }

  function onCommand(d) {
    commands[d.id] = d;
    if (d.state === 'running') snapRunning.add(d.id);
    touch('commands');
    let p = pending[d.id];
    if (!p && d.state === 'running' && d.body && d.body.ui) {
      // A page that was reloaded while the command runs has no local entry: rebuild it from what the click sent.
      const { key, value } = d.body.ui;
      p = pending[d.id] = { id: d.id, route: d.route, body: d.body, key, value, sentAt: Date.now(),
                            deadlineAt: d.deadline_at * 1000 + 5000, seenRunning: true };
      touch('pending');
      return;
    }
    if (!p) return;
    if (d.state === 'running') {
      p.seenRunning = true;
      p.deadlineAt = Math.max(p.sentAt + 60000, d.deadline_at * 1000 + 5000);
    } else {
      delete pending[d.id];
      if (d.state === 'failed') toast(d.message || `${d.route} failed`);
    }
    touch('pending');
  }

  // The full send of a (re)connect holds the running commands and only the last terminal ones. A pending id that
  // is not running in it finished (or was pruned) while the stream was down: unpend it so the block shows its value.
  // A command sent in the last few seconds may still be on its way to the server, so it stays.
  function reconcilePending() {
    const now = Date.now();
    Object.values(pending).forEach(p => {
      if (!snapRunning.has(p.id) && now - p.sentAt > 5000) delete pending[p.id];
    });
    touch('pending');
  }

  function setConn(state) {
    if (conn.state === state) return;
    conn.state = state;
    touch('conn');
  }

  function seen() {
    conn.lastAt = Date.now();
    setConn('open');
  }

  function on(es, name, fn) {
    es.addEventListener(name, e => { seen(); fn(JSON.parse(e.data)); });
  }

  // On every connect the server sends everything in full; a hello with another boot id means a restart.
  function connect() {
    const es = new EventSource('/api/events');
    es.onerror = () => setConn('down');
    on(es, 'hello', d => {
      if (conn.boot && conn.boot !== d.boot) { location.reload(); return; }
      conn.boot = d.boot;
      snapRunning = new Set();
    });
    on(es, 'block', d => {
      if (blocks[d.name] && blocks[d.name].rev === d.rev) return;
      blocks[d.name] = d;
      touch('block:' + d.name);
    });
    on(es, 'command', onCommand);
    on(es, 'log', d => {
      if (d.rev <= logRev) return;
      logRev = d.rev;
      logs.push(d);
      logs.splice(0, logs.length - 500);
      touch('log');
    });
    on(es, 'ready', () => { reconcilePending(); touch('conn'); });
    on(es, 'ping', () => {});  // a frame like any other: on() refreshes the last-seen time
    setInterval(() => {
      const now = Date.now();
      if (conn.state === 'open' && now - conn.lastAt > 40000) setConn('down');
      Object.values(pending).forEach(p => {
        if (now > p.deadlineAt && !p.seenRunning) fail(p.id, `${p.route}: no answer from the server`);
      });
    }, 1000);
  }

  return { blocks, pending, commands, logs, ui, conn, subscribe, command, pendingFor, setUI, connect, touch };
})();
