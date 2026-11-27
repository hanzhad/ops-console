// CI gate: counters, seats, GitHub queue and the repositories that run locally.
// A click goes through store.command with a pending key (ci.paused, ci.budget, ci.local:<repo>); the control shows
// the pending value and a spinner until the terminal event of that command, then falls back to the block value.

function gateView() {
  const b = store.blocks.ci_gate;
  const g = { ...((b && b.data) || {}) };
  const pp = store.pendingFor('ci.paused');
  if (pp) g.paused = pp.value;
  const pb = store.pendingFor('ci.budget');
  if (pb) { g.budget = pb.value.budget; g.cap = pb.value.cap; }
  return g;
}

function sendBudget(budget, cap) {
  store.command('/api/ci', { args: ['budget', String(budget), String(cap)] }, { key: 'ci.budget', value: { budget, cap } });
}

function adjustRunners(kind, delta) {
  const g = gateView();
  const bigCost = g.class_costs?.big || 4;
  const littleCost = g.class_costs?.little || 2;
  let big = Math.floor((g.cap ?? 8) / bigCost);
  let little = Math.max(0, Math.floor(((g.budget ?? 14) - (g.cap ?? 8)) / littleCost));

  if (kind === 'big') {
    big = Math.max(1, big + delta);
  } else {
    little = Math.max(0, little + delta);
  }
  sendBudget(bigCost * big + littleCost * little, bigCost * big);
}

function renderCIGate() {
  const g = gateView();
  const runners = (S && S.ci) || [];
  const busyN = runners.filter(c => c.busy).length;
  const budgetPending = !!store.pendingFor('ci.budget');
  const pausedPending = !!store.pendingFor('ci.paused');

  // Gate badge & toggle button
  const isRunning = g.running !== false;
  const isLocked = g.locked;
  const isPaused = g.paused;

  const badge = $('ci-gate-badge');
  const btnToggle = $('btn-gate-toggle');
  btnToggle.disabled = pausedPending;
  btnToggle.classList.toggle('is-pending', pausedPending);

  if (!isRunning) {
    badge.className = 'badge badge-red';
    badge.textContent = 'Gate Down';
    btnToggle.style.display = 'none';
  } else if (isLocked) {
    badge.className = 'badge badge-red';
    badge.textContent = 'Locked';
    btnToggle.style.display = 'none';
  } else if (isPaused) {
    badge.className = 'badge badge-yellow';
    badge.textContent = busyN ? `Paused (draining ${busyN})` : 'Paused';
    btnToggle.style.display = 'inline-flex';
    btnToggle.textContent = 'Resume';
    btnToggle.onclick = () => store.command('/api/ci', { args: ['resume'] }, { key: 'ci.paused', value: false });
  } else {
    badge.className = 'badge badge-green';
    badge.textContent = 'Running';
    btnToggle.style.display = 'inline-flex';
    btnToggle.textContent = 'Pause';
    btnToggle.onclick = () => store.command('/api/ci', { args: ['pause'] }, { key: 'ci.paused', value: true });
  }

  const rlEl = $('ci-rate-limit-badge');
  if (rlEl) {
    const rl = g.rate_limit;
    if (rl && typeof rl === 'object' && rl.remaining != null && rl.limit != null) {
      const rem = Number(rl.remaining);
      const lim = Number(rl.limit);
      const throttled = Boolean(rl.throttled);
      const cls = (throttled || rem < 500) ? 'badge-red' : rem < 1500 ? 'badge-yellow' : 'badge-green';
      let text = `GH API ${rem}/${lim}`;
      if ((throttled || rem < 500) && rl.reset_at) {
        const resetMs = Date.parse(rl.reset_at);
        if (!Number.isNaN(resetMs)) {
          const secLeft = Math.max(0, Math.ceil((resetMs - Date.now()) / 1000));
          const countdown = secLeft >= 60 ? `${Math.round(secLeft / 60)}m` : `${secLeft}s`;
          text += ` (reset ${countdown})`;
        }
      }
      rlEl.className = `badge ${cls}`;
      rlEl.textContent = text;
      rlEl.style.display = 'inline-flex';
      rlEl.title = rl.reset_at ? `GitHub API rate limit resets at ${rl.reset_at}` : 'GitHub API rate limit';
    } else {
      rlEl.style.display = 'none';
      rlEl.textContent = '';
    }
  }

  const metaEl = $('ci-gate-meta');
  if (metaEl) {
    const bits = [];
    if (g.commit) bits.push(String(g.commit).slice(0, 7));
    if (g.token_expires) bits.push(`token ${String(g.token_expires).slice(0, 10)}`);
    if (g.draining && g.draining.repo) bits.push(`draining ${g.draining.repo.split('/').pop()} (${g.draining.seconds || 0}s)`);
    if (g.waiting) bits.push(`waiting: ${g.waiting}`);
    metaEl.textContent = bits.join(' · ');
  }

  // Counters
  const bigCost = g.class_costs?.big || 4;
  const littleCost = g.class_costs?.little || 2;
  const big = Math.floor((g.cap ?? 8) / bigCost);
  const little = Math.max(0, Math.floor(((g.budget ?? 14) - (g.cap ?? 8)) / littleCost));
  const bigBusy = runners.filter(c => c.class === 'big' && c.busy).length;
  const littleBusy = runners.filter(c => c.class === 'little' && c.busy).length;

  $('ci-big-busy').textContent = bigBusy;
  $('ci-big-cap').textContent = big;
  $('ci-little-busy').textContent = littleBusy;
  $('ci-little-cap').textContent = little;

  $('btn-big-minus').disabled = budgetPending || big <= 1;
  $('btn-little-minus').disabled = budgetPending || little <= 0;
  $('btn-big-plus').disabled = budgetPending;
  $('btn-little-plus').disabled = budgetPending;

  $('btn-big-minus').onclick = () => adjustRunners('big', -1);
  $('btn-big-plus').onclick = () => adjustRunners('big', 1);
  $('btn-little-minus').onclick = () => adjustRunners('little', -1);
  $('btn-little-plus').onclick = () => adjustRunners('little', 1);

  // Budget summary & Reset
  const totalBudget = g.budget ?? (bigCost * big + littleCost * little);
  const usedBit = g.used != null ? `, used ${g.used}` : '';
  const megaBit = g.class_costs && g.class_costs.mega ? `, mega ${g.class_costs.mega}c` : '';
  $('ci-budget-summary').textContent = `= ${totalBudget} cores (big ${bigCost * big}, little ${littleCost * little}${usedBit}${megaBit})`;
  $('ci-budget-summary').classList.toggle('is-pending', budgetPending);

  const resetBtn = $('btn-ci-reset');
  if (g.boot && g.boot.length === 2 && (g.boot[0] !== g.budget || g.boot[1] !== g.cap)) {
    const bootBig = Math.floor(g.boot[1] / bigCost);
    const bootLittle = Math.max(0, Math.floor((g.boot[0] - g.boot[1]) / littleCost));
    resetBtn.style.display = 'inline-flex';
    resetBtn.textContent = `Reset to ${bootBig}b+${bootLittle}l`;
    resetBtn.disabled = budgetPending;
    resetBtn.onclick = () => store.command('/api/ci', { args: ['reset'] }, { key: 'ci.budget', value: { budget: g.boot[0], cap: g.boot[1] } });
  } else {
    resetBtn.style.display = 'none';
  }

  // Seats breakdown
  const tbody = $('ci-tbody');
  const rows = [];
  const megaCap = runners.filter(c => c.class === 'mega').length;
  for (const [k, n] of [...(megaCap > 0 ? [['mega', megaCap]] : []), ['big', big], ['little', little]]) {
    const held = runners.filter(c => c.class === k);
    for (let i = 0; i < Math.max(n, held.length); i++) {
      const c = held[i];
      const seatLabel = i < n ? `${k} #${i + 1}` : `${k} +${i - n + 1}`;
      if (c) {
        const repoShort = (c.repo || '').split('/').pop();
        rows.push(`
          <tr>
            <td><span class="badge ${k === 'big' ? 'badge-primary' : 'badge-custom'}">${seatLabel}</span></td>
            <td class="mono" style="font-weight:600;">${esc(repoShort)}</td>
            <td title="${esc(c.job)}">${esc(c.busy ? (c.job || 'job') : 'idle, waiting')}</td>
            <td><span class="badge ${c.busy ? 'badge-green' : 'badge-custom'}">${c.busy ? 'Busy' : 'Idle'}</span></td>
            <td class="mono">${Math.round(c.cpu || 0)}% · ${(c.mem_mb / 1024).toFixed(1)}GB${i >= n ? ' (draining)' : ''}</td>
          </tr>
        `);
      } else {
        rows.push(`
          <tr>
            <td><span class="badge badge-custom" style="opacity:0.6;">${seatLabel}</span></td>
            <td colspan="4" style="color:var(--text-muted);font-style:italic;">free seat</td>
          </tr>
        `);
      }
    }
  }
  tbody.innerHTML = rows.length > 0 ? rows.join('') : '<tr><td colspan="5" class="empty-state">No seats configured</td></tr>';
}

function renderCIQueue() {
  const b = store.blocks.gh_queue;
  const g = gateView();
  const queueUnknown = !!(g.queue_unknown || (b && (b.error || b.data === null)));
  const q = Array.isArray(b && b.data) ? b.data : [];
  const qWrap = $('ci-queue-wrap');
  if (queueUnknown) {
    qWrap.style.display = 'block';
    $('ci-queue-count').textContent = 'unavailable';
    $('ci-queue-tbody').innerHTML = '<tr><td colspan="5" class="empty-state">queue unavailable (legacy status)</td></tr>';
  } else if (q.length > 0) {
    qWrap.style.display = 'block';
    $('ci-queue-count').textContent = `${q.length} waiting`;
    $('ci-queue-tbody').innerHTML = q.slice(0, 10).map((item, idx) => {
      const createdMs = item.created ? Date.parse(item.created) : 0;
      const waitSec = createdMs ? Math.max(0, (Date.now() - createdMs) / 1000) : 0;
      return `
        <tr>
          <td class="mono" style="color:var(--text-muted);">${idx + 1}</td>
          <td class="mono" style="font-weight:600;">${esc((item.repo || '').split('/').pop())}</td>
          <td title="${esc(item.name)}">${esc(item.name || 'workflow')}</td>
          <td class="mono">${esc(item.branch || '')}</td>
          <td><span class="badge ${waitSec > 1800 ? 'badge-red' : waitSec > 600 ? 'badge-yellow' : 'badge-custom'}">${ago(waitSec)}</span></td>
        </tr>
      `;
    }).join('');
  } else {
    qWrap.style.display = 'none';
    $('ci-queue-count').textContent = '0 waiting';
    $('ci-queue-tbody').innerHTML = '';
  }
}

// Repositories Local vs GitHub: rebuilt only when the set of repositories changes; otherwise the existing
// checkboxes are updated in place, so a click, a focus or a hover is never reset by a render.
function renderCIRepos() {
  const b = store.blocks.ci_gate;
  const repos = (b && b.data && b.data.repos) || [];
  const reposEl = $('ci-repos-list');
  const names = repos.map(r => r.repo).join(',');
  if (reposEl.dataset.repos !== names || !names) {
    reposEl.dataset.repos = names;
    reposEl.textContent = '';
    if (!repos.length) {
      const none = document.createElement('small');
      none.style.color = 'var(--text-muted)';
      none.textContent = 'None configured';
      reposEl.appendChild(none);
    }
    repos.forEach(repo => {
      const chip = document.createElement('div');
      chip.className = 'kpi-chip';
      chip.style.display = 'inline-flex';
      chip.style.alignItems = 'center';
      chip.style.gap = '6px';
      chip.dataset.repo = repo.repo;
      const label = document.createElement('label');
      label.style.display = 'inline-flex';
      label.style.alignItems = 'center';
      label.style.gap = '6px';
      label.style.cursor = 'pointer';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.addEventListener('change', () => {
        const value = input.checked;
        store.command('/api/ci', { args: [value ? 'on' : 'off', repo.repo] }, { key: 'ci.local:' + repo.repo, value });
      });
      const name = document.createElement('span');
      name.textContent = repo.repo.split('/').pop();
      label.append(input, name);
      const megaBtn = document.createElement('button');
      megaBtn.type = 'button';
      megaBtn.className = 'btn-xs ci-mega-btn';
      megaBtn.style.fontSize = '10px';
      megaBtn.style.padding = '0 5px';
      megaBtn.textContent = 'mega';
      megaBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const cur = megaBtn.dataset.active === '1';
        store.command('/api/ci', { args: [!cur ? 'mega-on' : 'mega-off', repo.repo] }, { key: 'ci.mega:' + repo.repo, value: !cur });
      });
      chip.append(label, megaBtn);
      reposEl.appendChild(chip);
    });
  }
  repos.forEach(repo => {
    const chip = [...reposEl.children].find(el => el.dataset.repo === repo.repo);
    if (!chip) return;
    const pending = store.pendingFor('ci.local:' + repo.repo);
    const input = chip.querySelector('input');
    input.checked = pending?.value ?? repo.local;
    input.disabled = !!pending;
    const mPend = store.pendingFor('ci.mega:' + repo.repo);
    const megaBtn = chip.querySelector('.ci-mega-btn');
    const megaOn = mPend ? !!mPend.value : !!repo.mega;
    if (megaBtn) {
      megaBtn.dataset.active = megaOn ? '1' : '0';
      megaBtn.disabled = !!mPend;
      megaBtn.style.opacity = megaOn ? '1' : '0.45';
      megaBtn.style.borderColor = megaOn ? 'var(--status-cyan)' : '';
      megaBtn.title = megaOn ? `Mega class enabled (${repo.mega || 'on'}) — click to disable` : 'Enable whole-machine mega class for this repo';
    }
    chip.classList.toggle('is-pending', !!(pending || mPend));
    chip.title = `${repo.repo}: ${input.checked ? 'Runs locally on this PC' : 'Runs on GitHub runners'}${megaOn ? ' · mega' : ''}`;
  });
}

store.subscribe(['block:ci_gate', 'pending'], () => { renderCIGate(); renderCIRepos(); });
store.subscribe(['block:*'], renderCIGate);  // the seat table reads S.ci (the runners), which arrives with the other blocks
store.subscribe(['block:gh_queue', 'block:ci_gate'], renderCIQueue);
