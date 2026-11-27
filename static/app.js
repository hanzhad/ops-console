// The page's wiring: S (what /api/state used to return) is assembled from the store's blocks, commands and
// log lines whenever one of them changes; the render modules subscribe after this file and see the fresh S.
let S = null;

const dataOf = blk => (blk && blk.data != null ? blk.data : null);

function assembleState() {
  const b = store.blocks;
  if (!b.slots || !b.sessions) return null;
  const slots = dataOf(b.slots) || {}, sessions = dataOf(b.sessions) || {}, cleaners = dataOf(b.cleaners) || {};
  const s = {
    slots: slots.slots || [], machine_max: slots.machine_max ?? 20, machine_in_use: (slots.slots || []).length,
    default_limit: sessions.default_limit ?? 1, sessions: sessions.sessions || [],
    ci: dataOf(b.runners) || [], cleaners: cleaners.cleaners || {}, rescue_bundles: cleaners.rescue_bundles || [],
    host: dataOf(b.host) || {}, autoreview: dataOf(b.autoreview),
  };
  if (b.ci_gate && b.ci_gate.data != null) {
    const q = b.ci_gate.data.queue_unknown ? null : ((b.gh_queue && b.gh_queue.data) || []);
    s.gate = { ...b.ci_gate.data, queue: q };
  }
  s.events = store.logs.slice(-20).map(e => e.text);
  s.results = {};
  Object.values(store.commands).sort((x, y) => x.rev - y.rev).forEach(c => {
    const target = c.body && c.body.target;
    if (c.route === '/api/rescue' && c.state !== 'running' && target) s.results[target] = { ok: c.state === 'ok', message: c.message };
  });
  return s;
}

store.subscribe(['block:*', 'commands', 'log'], () => { S = assembleState(); });

// Subscribes a render function that waits for the first state.
function onState(keys, fn) {
  return store.subscribe(keys, () => { if (S) fn(); });
}

// A POST is not a UI transition: the command id is tracked by the store until its terminal event.
function api(path, body) {
  return store.command(path, body);
}

// Active section tracking for mobile-nav
const navSections = ['sec-slots', 'sec-cleaners', 'sec-sessions', 'sec-ci', 'sec-ar', 'sec-logs'];
const navChips = document.querySelectorAll('.nav-chip');

window.addEventListener('scroll', () => {
  const scrollPos = window.scrollY + 130;
  for (let i = navSections.length - 1; i >= 0; i--) {
    const el = document.getElementById(navSections[i]);
    if (el && el.offsetTop <= scrollPos) {
      navChips.forEach(chip => {
        chip.classList.toggle('active', chip.getAttribute('href') === '#' + navSections[i]);
      });
      break;
    }
  }
}, { passive: true });

navChips.forEach(chip => {
  chip.addEventListener('click', () => {
    navChips.forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
  });
});

// The render modules are loaded after this file: connect once all of them have subscribed.
window.addEventListener('load', () => store.connect());
