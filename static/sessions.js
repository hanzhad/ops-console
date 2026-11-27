// Sessions table, selection and messaging. Selection, filter and open trees live in the store.
const checkedSessions = store.ui.selected;
const expandedTrees = store.ui.expanded;   // root ids the operator opened
const collapsedTrees = store.ui.collapsed;  // root ids the operator closed (wins over auto-open)
const collapsedGroups = store.ui.collapsedGroups || (store.ui.collapsedGroups = new Set());

function projectNameOf(cwd) {
  const cleaned = String(cwd ?? '').trim().replace(/[/\\]+$/, '');
  if (!cleaned) return '(no project)';
  const parts = cleaned.split(/[/\\]+/).filter(Boolean);
  if (parts.length === 0) return '(no project)';
  const last = parts[parts.length - 1];
  if (/^[A-Za-z]:$/.test(last)) return '(no project)';
  return last;
}

const cmpNatural = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
const harnessLabelOf = h => h === 'antigravity' ? 'Antigravity' : h === 'claude' ? 'Claude Code' : h;
const sessionTitleOf = s => s.title || s.ref || s.id || '';
const sessionCanonOf = s => s.ref || s.id || '';
const cmpSessions = (a, b) => cmpNatural(sessionTitleOf(a), sessionTitleOf(b)) || cmpNatural(sessionCanonOf(a), sessionCanonOf(b));
const projectKeyOf = s => projectNameOf(s.cwd).toLowerCase();
const cmpProjectKeys = (aKey, bKey) => {
  if (aKey === '(no project)' && bKey !== '(no project)') return 1;
  if (bKey === '(no project)' && aKey !== '(no project)') return -1;
  return cmpNatural(aKey, bKey);
};
const cmpRoots = (a, b) => cmpProjectKeys(projectKeyOf(a), projectKeyOf(b)) || cmpSessions(a, b);

function isGroupOpen(key, q) {
  return !!q || !collapsedGroups.has(key);
}

function toggleGroup(key) {
  if (collapsedGroups.has(key)) collapsedGroups.delete(key);
  else collapsedGroups.add(key);
  store.setUI({});
}

function renderSessions() {
  const sessions = S.sessions || [];
  const tbody = $('sessions-tbody');
  $('sessions-count').textContent = `(${sessions.length} active)`;

  const q = (store.ui.search || '').toLowerCase().trim();

  const matches = s => {
    if (store.ui.harness !== 'all' && (s.harness || '').toLowerCase() !== store.ui.harness.toLowerCase()) return false;
    if (!q) return true;
    return [s.title, projectNameOf(s.cwd), s.cwd, s.ref, s.id, s.harness].some(v => (v || '').toLowerCase().includes(q));
  };

  // A tree is shown whole when any of its sessions matches, so a child never appears without its parent.
  const rootOf = s => s.root_id || s.id;
  const shownRoots = new Set(sessions.filter(matches).map(rootOf));
  const children = {};
  sessions.forEach(s => {
    if (s.parent_id) (children[s.parent_id] = children[s.parent_id] || []).push(s);
  });
  Object.values(children).forEach(list => list.sort(cmpSessions));
  const roots = sessions.filter(s => !s.parent_id && shownRoots.has(s.id));
  const descendants = id => (children[id] || []).flatMap(c => [c, ...descendants(c.id)]);

  if (roots.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No sessions found matching filter.</td></tr>';
    return;
  }
  $('sessions-count').textContent = `(${sessions.length} active, ${roots.length} groups)`;

  const row = (s, depth, rootId, projSep = false) => {
    const ref = s.ref || s.id;
    const isRoot = depth === 0;
    const isChecked = checkedSessions.has(ref) ? 'checked' : '';
    const activeText = s.archived ? '<span style="color:var(--text-muted)">archived</span>'
      : s.is_active ? '<span style="color:var(--status-green)">● Live</span>'
      : (s.last_active ? ago(Date.now()/1000 - s.last_active) : 'idle');
    const kids = isRoot ? descendants(s.id) : [];
    const open = isRoot && kids.length > 0 && isTreeOpen(s, q);
    const toggle = isRoot && kids.length > 0
      ? `<button class="tree-toggle" title="Show or hide the sessions this one started" onclick="toggleTree(${escJs(s.id)}, ${open})">${open ? '▾' : '▸'} ${kids.length}</button>`
      : '';
    const titleStyle = `font-weight:${isRoot ? 600 : 500};max-width:min(100%, ${240 - depth * 16}px);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`;
    const displayTitle = s.title || ref;
    const hoverTip = s.cwd ? `${displayTitle} — ${s.cwd}` : displayTitle;

    // Per-session slot count & limit controls: live slots held under this session's id
    // (subagents included) vs its effective limit (0..20, 0 = paused).
    const held = s.held_slots ?? 0;
    const kidsHeld = isRoot ? Math.max(0, (s.tree_held ?? held) - held) : 0;
    const pend = store.pendingFor('limit:' + ref);
    const rawLim = s.limit ?? (S.default_limit ?? 1);
    const lim = pend ? pend.value : rawLim;
    const paused = lim === 0;
    const over = held > lim;
    const minusVal = Math.max(0, Math.min(20, rawLim - 1));
    const plusVal = Math.max(0, Math.min(20, rawLim + 1));
    const refJs = escJs(ref);
    const keyJs = escJs('limit:' + ref);
    const pausedBadge = paused ? '<span class="badge badge-yellow">Paused</span>' : '';
    const overBadge = over ? `<span class="badge badge-red">over by ${held - lim}</span>` : '';
    const slotsCell = `
          <span class="${pend ? 'is-pending' : ''}" style="display:inline-flex;align-items:center;flex-wrap:wrap;gap:4px;">
            <span style="font-weight:700;color:${over ? 'var(--status-red)' : 'var(--text-main)'};" title="Slots held under this session's id, its subagents' included">${held}</span>
            <span style="color:var(--text-muted);">/</span>
            <span style="font-weight:600;min-width:14px;text-align:center;">${lim}</span>
            <button class="btn-xs" title="Decrease slot limit by 1" onclick="store.command('/api/limit', { ref: ${refJs}, delta: -1 }, { key: ${keyJs}, value: ${minusVal} })" ${pend || lim <= 0 ? 'disabled' : ''}>−</button>
            <button class="btn-xs" title="Increase slot limit by 1" onclick="store.command('/api/limit', { ref: ${refJs}, delta: 1 }, { key: ${keyJs}, value: ${plusVal} })" ${pend || lim >= 20 ? 'disabled' : ''}>+</button>
            <button class="btn-xs" title="Pause session (set limit to 0)" onclick="store.command('/api/limit', { ref: ${refJs}, mode: 'pause' }, { key: ${keyJs}, value: 0 })" ${pend || paused ? 'disabled' : ''}>Pause</button>
            ${pausedBadge}${overBadge}${kidsHeld > 0 ? `<span style="color:var(--text-muted);font-size:10px;margin-left:4px;" title="Slots held by the sessions this one started">+${kidsHeld} in children</span>` : ''}
          </span>`;

    return `
      <tr class="${isRoot ? 'sess-root' : 'sess-child'}${projSep ? ' sess-proj-sep' : ''} ${s.archived ? 'sess-archived' : ''}" data-root="${esc(rootId)}" style="--depth:${depth};">
        <td><input type="checkbox" class="session-chk" data-ref="${esc(ref)}" ${isChecked} ${s.archived ? 'disabled' : ''}></td>
        <td style="--depth:${depth};padding-left:${8 + depth * 16}px;">
          <div style="display:flex;align-items:baseline;min-width:0;" title="${esc(hoverTip)}">${toggle}<div style="${titleStyle}" title="${esc(hoverTip)}">${esc(displayTitle)}</div></div>
          <div class="sess-project">${esc(projectNameOf(s.cwd))}</div>
        </td>
        <td class="mono">${slotsCell}</td>
        <td>${activeText}</td>
      </tr>
    `;
  };

  const byId = Object.fromEntries(sessions.map(x => [x.id, x]));
  const depthOf = s => { let d = 0, p = s.parent_id; while (p && byId[p] && d < 10) { d++; p = byId[p].parent_id; } return d; };

  const harnessMap = new Map();
  roots.forEach(r => {
    const hKey = (r.harness || 'unknown').toLowerCase();
    if (!harnessMap.has(hKey)) harnessMap.set(hKey, []);
    harnessMap.get(hKey).push(r);
  });

  const sortedHarnesses = [...harnessMap.keys()].sort((a, b) =>
    cmpNatural(harnessLabelOf(a), harnessLabelOf(b)) || cmpNatural(a, b)
  );

  const html = [];
  for (const hKey of sortedHarnesses) {
    const hRoots = harnessMap.get(hKey);
    const hGroupKey = `harness:${hKey}`;
    const hOpen = isGroupOpen(hGroupKey, q);
    const hHeld = hRoots.reduce((sum, r) => sum + (r.tree_held ?? r.held_slots ?? 0), 0);
    const hHeldBadge = hHeld > 0 ? `<span class="badge">${hHeld} ${hHeld === 1 ? 'slot' : 'slots'}</span>` : '';
    html.push(`
      <tr class="sess-group-harness" data-group="${esc(hGroupKey)}">
        <td colspan="4">
          <button type="button" class="group-toggle" onclick="toggleGroup(${escJs(hGroupKey)})">
            <span class="group-caret">${hOpen ? '▾' : '▸'}</span>
            ${harnessBadge(hKey)}
            <span class="group-count">${hRoots.length}</span>
            ${hHeldBadge}
          </button>
        </td>
      </tr>
    `);
    if (!hOpen) continue;

    const sortedRoots = [...hRoots].sort(cmpRoots);
    let prevProjKey = null;
    for (const r of sortedRoots) {
      const projKey = projectKeyOf(r);
      const projSep = prevProjKey !== null && projKey !== prevProjKey;
      prevProjKey = projKey;
      const kids = descendants(r.id);
      const open = kids.length > 0 && isTreeOpen(r, q);
      html.push(row(r, 0, r.id, projSep));
      if (open) {
        for (const c of kids) html.push(row(c, depthOf(c), r.id, false));
      }
    }
  }

  tbody.innerHTML = html.join('');

  document.querySelectorAll('.session-chk').forEach(chk => {
    chk.onchange = () => {
      const r = chk.getAttribute('data-ref');
      if (chk.checked) checkedSessions.add(r);
      else checkedSessions.delete(r);
    };
  });
}

// A tree opens by itself while a search is typed, unless the operator closed it;
// otherwise it stays as the operator left it.
function isTreeOpen(root, q) {
  if (collapsedTrees.has(root.id)) return false;
  return expandedTrees.has(root.id) || !!q || !!root.tree_over_limit;
}

function toggleTree(id, open) {
  if (open) { expandedTrees.delete(id); collapsedTrees.add(id); }
  else { collapsedTrees.delete(id); expandedTrees.add(id); }
  store.setUI({});
}

// UI Setup & Handlers
$('chk-all-sessions').onchange = (e) => {
  const checked = e.target.checked;
  document.querySelectorAll('.session-chk').forEach(c => {
    c.checked = checked;
    const r = c.getAttribute('data-ref');
    if (checked) checkedSessions.add(r);
    else checkedSessions.delete(r);
  });
};

$('session-search').addEventListener('input', () => store.setUI({ search: $('session-search').value }));

$('btn-select-active').onclick = () => {
  (S.sessions || []).forEach(s => {
    if (s.is_active || (Date.now()/1000 - s.last_active < 3600)) {
      checkedSessions.add(s.ref || s.id);
    }
  });
  renderSessions();
};

$('btn-clear-selection').onclick = () => {
  checkedSessions.clear();
  renderSessions();
};

$('btn-send-msg').onclick = () => {
  const text = $('msg-input').value.trim();
  if (!text) return alert('Please enter message text');
  if (checkedSessions.size === 0) return alert('Please select at least one session recipient');

  const targets = Array.from(checkedSessions).map(ref => {
    const s = (S.sessions || []).find(item => item.ref === ref || item.id === ref);
    return { ref, harness: s?.harness || 'unknown' };
  });

  api('/api/send', { to: targets, text });
  $('msg-input').value = '';
};

onState(['block:sessions', 'pending', 'ui'], renderSessions);
