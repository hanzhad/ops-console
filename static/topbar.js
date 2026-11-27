// KPI strip, harness filter pills and the connection dot.
function renderTopbar() {
  const h = S.host || {};
  const pendCap = store.pendingFor('cap');
  const max = pendCap ? pendCap.value : (S.machine_max ?? 20);
  const inUse = S.machine_in_use ?? 0;
  const paused = max === 0;
  const capEl = $('kpi-cap');
  capEl.textContent = paused ? `${inUse} / 0 (Paused)` : `${inUse} / ${max}`;
  capEl.style.color = paused ? 'var(--status-yellow)' : '';
  capEl.classList.toggle('is-pending', !!pendCap);
  $('kpi-load').textContent = `${(h.load1 ?? 0).toFixed(2)} · ${(h.load5 ?? 0).toFixed(2)}`;
  $('kpi-ram').textContent = `${Math.round((h.mem_used_mb ?? 0) / 1024)} / ${Math.round((h.mem_total_mb ?? 0) / 1024)} GB`;
  $('kpi-cpu').textContent = `${(h.docker_cpu ?? 0).toFixed(1)}%`;

  const runnersCount = (S.ci || []).filter(c => c.busy).length;
  $('kpi-ci').textContent = `${runnersCount} running`;

  const arRunning = S.autoreview?.container?.running;
  $('kpi-ar').textContent = arRunning ? 'Active' : 'Stopped';

  $('kpi-ts').textContent = new Date().toLocaleTimeString();
}

function renderHarnessPills() {
  const sessions = S.sessions || [];
  const harnesses = Array.from(new Set(sessions.map(s => s.harness).filter(Boolean))).sort();

  const container = $('slots-harness-pills');
  const sessContainer = $('sessions-harness-pills');

  let html = `<span class="pill ${store.ui.harness === 'all' ? 'active' : ''}" data-harness="all">All</span>`;
  harnesses.forEach(h => {
    html += `<span class="pill ${store.ui.harness === h ? 'active' : ''}" data-harness="${esc(h)}">${esc(h.charAt(0).toUpperCase() + h.slice(1))}</span>`;
  });

  container.innerHTML = html;
  sessContainer.innerHTML = html;

  [container, sessContainer].forEach(el => {
    el.querySelectorAll('.pill').forEach(pill => {
      pill.onclick = () => store.setUI({ harness: pill.getAttribute('data-harness') });
    });
  });
}

function renderConn() {
  const up = store.conn.state === 'open';
  $('heartbeat').className = up ? 'brand-dot' : 'brand-dot error';
  $('heartbeat').title = up ? '' : 'no connection';
}

onState(['block:host', 'block:slots', 'block:sessions', 'block:runners', 'block:autoreview', 'pending'], renderTopbar);
onState(['block:sessions', 'ui'], renderHarnessPills);
store.subscribe(['conn'], renderConn);
