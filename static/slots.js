// Slot grid and the machine cap.

// The block holds absolute times, so it does not change as time passes; the elapsed ones are worked out here.
function withClock(s) {
  const now = Date.now() / 1000;
  return {
    ...s,
    idle_seconds: (s.last_activity ?? s.since) == null ? 999999 : Math.max(0, Math.floor(now - (s.last_activity ?? s.since))),
    age_s: s.since == null ? 0 : Math.floor(now - s.since),
    ttl_left_s: s.ttl_until == null ? 0 : Math.floor(s.ttl_until - now),
  };
}

function clampLimit(v) {
  return Math.max(0, Math.min(20, v));
}

function renderSlots() {
  const cap = S.machine_max ?? 20;
  const pendCap = !!store.pendingFor('cap');
  const minusBtn = $('cap-minus');
  const plusBtn = $('cap-plus');
  const pauseBtn = $('cap-pause');
  if (minusBtn) minusBtn.disabled = cap <= 0 || pendCap;
  if (plusBtn) plusBtn.disabled = cap >= 20 || pendCap;
  if (pauseBtn) pauseBtn.disabled = cap === 0 || pendCap;
  const slots = (S.slots || []).map(withClock);
  const container = $('slots-container');
  $('slots-count').textContent = `(${slots.length} active leases)`;

  const filtered = store.ui.harness === 'all'
    ? slots
    : slots.filter(s => (s.harness || '').toLowerCase() === store.ui.harness.toLowerCase());

  if (filtered.length === 0) {
    container.innerHTML = '<div class="empty-state" style="grid-column:1/-1;">No live slots matching current filter.</div>';
    return;
  }

  // Leases of one parent session sit together in one frame, headed by that parent.
  const groups = [];
  const byGroup = {};
  filtered.forEach(s => {
    const key = s.abandoned ? '__abandoned__' : (s.group_ref || s.session_ref || s.holder || s.id);
    if (!byGroup[key]) {
      const root = (S.sessions || []).find(x => x.id === key);
      byGroup[key] = {
        key,
        title: s.abandoned ? 'Abandoned (no lease)' : (s.group_title || s.session_title || s.holder),
        archived: !!(root && root.archived),
        // A session the server made up from the lease itself is no parent at all.
        orphan: !s.abandoned && (!root || root.metadata?.discovered_from === 'lease'),
        slots: [],
      };
      groups.push(byGroup[key]);
    }
    byGroup[key].slots.push(s);
  });

  container.innerHTML = groups.map(g => {
    // A lease with no session behind it has no group to show: it stays a plain card.
    if (g.orphan || g.key === '__abandoned__') return g.slots.map(slotCard).join('');
    const note = g.archived ? ' · parent archived' : '';
    return `
      <div class="slot-group ${g.archived ? 'archived' : ''} ${g.slots.length > 1 ? 'wide' : ''}">
        <div class="slot-group-head"><b title="${esc(g.title)}">${esc(g.title)}</b><span>${g.slots.length} slot${g.slots.length === 1 ? '' : 's'}${note}</span></div>
        <div class="slots-matrix">${g.slots.map(slotCard).join('')}</div>
      </div>`;
  }).join('');
}

function slotCard(s) {
  {
    const cardClass = !s.running ? 'dead' : (s.idle_seconds > 3600 ? 'idle' : 'active');
    const portEntries = Object.entries(s.ports || {}).map(([k, v]) => `${k}:${v}`).join(' ');
    const sid = esc(s.id);
    const pendEvict = !!store.pendingFor('evict:' + s.id);
    const ephBadge = s.ephemeral ? '<span class="badge badge-yellow">ephemeral</span>' : '';
    const tbBadge = s.toolbox ? '<span class="badge badge-custom">toolbox</span>' : '';
    const repos = (s.repos || []).filter(r => r && r.alias);
    const reposRow = repos.length > 0
      ? `<div class="slot-meta" style="flex-wrap:wrap;gap:4px;">${repos.map(r => `<span class="badge badge-custom" title="${esc(r.path || r.alias)}">${esc(r.alias)} (${esc(r.mode || 'ro')})</span>`).join('')}</div>`
      : '';

    return `
      <div class="slot-card ${cardClass}">
        <div class="slot-top">
          <span class="slot-id">#${s.slot} · ${esc(s.project)}</span>
          <span style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;">${ephBadge}${tbBadge}${harnessBadge(s.harness)}</span>
        </div>
        <div class="slot-session">
          <div class="slot-title" title="${esc(s.session_title)}">${esc(s.session_title || s.holder)}</div>
          <div class="slot-meta">
            <span>Branch: <b style="color:var(--text-main);">${esc(s.branch || 'detached')}</b></span>
            ${s.head ? `<span>(${esc(s.head)})</span>` : ''}
          </div>
          ${reposRow}
          <div class="slot-meta">
            <span class="slot-ports">${esc(portEntries)}</span>
            <span>Active: ${ago(s.idle_seconds)}</span>
          </div>
        </div>
        <div class="slot-stats">
          <div class="stat-item"><small>CPU</small>${s.cpu}%</div>
          <div class="stat-item"><small>RAM</small>${s.mem_mb}M</div>
          <div class="stat-item"><small>STATUS</small>${s.running ? 'Running' : 'Stopped'}</div>
        </div>
        <div class="slot-actions">
          <button class="btn-xs" title="Gracefully release the slot. Refuses with a reason if a live lease, ssh session or process holds it. Uncommitted work is saved first. Try this button first." onclick="store.command('/api/evict', { target: '${sid}', mode: 'release' }, { key: 'evict:${sid}', value: 'release' })" ${pendEvict ? 'disabled' : ''}>Release</button>
          <button class="btn-xs btn-danger" title="Forcibly take the slot even if an agent is using it (--force). Use only if Release refused and the slot is definitely stuck. Asks for confirmation." onclick="if(confirm('Force evict slot ${sid}?')) store.command('/api/evict', { target: '${sid}', mode: 'force' }, { key: 'evict:${sid}', value: 'force' })" ${pendEvict ? 'disabled' : ''}>Evict</button>
        </div>
      </div>
    `;
  }
}

$('cap-minus').onclick = () => store.command('/api/cap', { delta: -1 }, { key: 'cap', value: clampLimit((S?.machine_max ?? 20) - 1) });
$('cap-plus').onclick = () => store.command('/api/cap', { delta: 1 }, { key: 'cap', value: clampLimit((S?.machine_max ?? 20) + 1) });
$('cap-pause').onclick = () => store.command('/api/cap', { mode: 'pause' }, { key: 'cap', value: 0 });

onState(['block:slots', 'ui', 'pending'], renderSlots);
setInterval(() => { if (S) renderSlots(); }, 15000);
