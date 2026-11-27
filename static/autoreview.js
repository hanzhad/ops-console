// Auto-review daemon: status, global harness/model, repositories and logs.
// An ordinary command: the selects stay locked while it is pending (see renderAutoReview) and the note shows its outcome.
function setGlobal(key, val) {
  const note = $('ar-save-note');
  note.style.color = 'var(--text-dim)';
  note.textContent = 'saving…';
  const id = store.command('/api/autoreview', { args: ['set-global', key, val] }, { key: 'ar.global.' + key, value: val });
  renderAutoReview();
  const stop = store.subscribe(['pending'], () => {
    if (store.pending[id]) return;
    stop();
    const c = store.commands[id];
    const ok = Boolean(c) && c.state === 'ok';
    note.style.color = ok ? 'var(--text-dim)' : '#f66';
    note.textContent = ok ? (c.message || 'saved') : ((c && c.message) || 'failed');
    renderAutoReview();
  });
}

function renderAutoReview() {
  const ar = S.autoreview || {};
  const container = ar.container || {};
  const isPaused = container.paused || container.status === 'paused';
  const isRunning = container.running && !isPaused;

  const health = ar.health || {};
  const healthErr = health.error || null;
  const tokIssues = Array.isArray(health.tokens) ? health.tokens : [];
  const harnessFaults = Object.entries(health.harnesses || {}).filter(([, v]) => v && v.state === 'fault');

  const badge = $('ar-status-badge');
  if (badge) {
    if (isPaused) {
      badge.className = 'badge badge-yellow';
      badge.textContent = 'Paused';
    } else if (isRunning && (harnessFaults.length > 0 || healthErr)) {
      badge.className = 'badge badge-red';
      badge.textContent = 'Fault';
    } else if (isRunning && tokIssues.length > 0) {
      badge.className = 'badge badge-yellow';
      badge.textContent = 'Token Issue';
    } else if (isRunning) {
      badge.className = 'badge badge-green';
      badge.textContent = 'Running';
    } else {
      badge.className = 'badge badge-red';
      badge.textContent = 'Stopped';
    }
  }

  const pBtn = $('btn-ar-pause');
  if (pBtn) {
    if (isPaused) {
      pBtn.textContent = 'Resume';
      pBtn.className = 'btn-xs btn-primary';
      pBtn.title = 'Resume auto-review daemon execution';
      pBtn.onclick = () => api('/api/autoreview', { args: ['resume'] });
    } else if (isRunning) {
      pBtn.textContent = 'Pause';
      pBtn.className = 'btn-xs btn-danger';
      pBtn.title = 'Pause auto-review daemon execution';
      pBtn.onclick = () => api('/api/autoreview', { args: ['pause'] });
    } else {
      pBtn.textContent = 'Start';
      pBtn.className = 'btn-xs btn-primary';
      pBtn.title = 'Start auto-review daemon';
      pBtn.onclick = () => api('/api/autoreview', { args: ['resume'] });
    }
  }

  const hSelect = $('ar-global-harness');
  const mSelect = $('ar-global-model');

  // Lists come from the auto-review CLI only; a mismatch is shown, never masked.
  const opt = (v, label, sel, warn) => `<option value="${esc(v)}" ${sel ? 'selected' : ''}${warn ? ' style="color:#f0a030;font-weight:600;"' : ''}>${esc(label)}</option>`;
  const warnStyle = (sel, bad) => { sel.style.borderColor = bad ? '#f0a030' : '#333a4d'; sel.style.color = bad ? '#f0a030' : 'var(--text-main)'; };
  const harnesses = ar.available_harnesses || [];
  const hOpts = harnesses.map(h => opt(h, h, h === ar.global_harness));
  if (ar.global_harness && !harnesses.includes(ar.global_harness)) hOpts.unshift(opt(ar.global_harness, ar.global_harness + ' (not offered by auto-review)', true, true));
  hSelect.innerHTML = hOpts.join('');
  warnStyle(hSelect, hOpts.length > harnesses.length);

  const models = (ar.available_models || {})[ar.global_harness] || [];
  const mOpts = models.map(m => opt(m, m, m === ar.global_model));
  if (ar.global_model && !models.includes(ar.global_model)) mOpts.unshift(opt(ar.global_model, ar.global_model + ' (not offered by ' + ar.global_harness + ')', true, true));
  mSelect.innerHTML = mOpts.join('');
  warnStyle(mSelect, mOpts.length > models.length);

  const mErr = $('ar-models-error');
  if (mErr) {
    const errs = [];
    if (ar.models_error) errs.push('Harness/model list unavailable: ' + ar.models_error);
    if (isRunning && healthErr) errs.push('Health check failed: ' + healthErr);
    harnessFaults.forEach(([h, info]) => errs.push(`${h} fault: ${info.message || 'unavailable'}`));
    tokIssues.forEach(t => errs.push(`${t.name || t.kind || 'token'}: ${t.problem || 'problem'}`));
    mErr.textContent = errs.join(' · ');
  }

  // A set-global in flight shows its value and keeps both selects locked until the terminal event.
  const pendH = store.pendingFor('ar.global.harness');
  const pendM = store.pendingFor('ar.global.model');
  if (pendH) hSelect.value = pendH.value;
  if (pendM) mSelect.value = pendM.value;
  hSelect.disabled = mSelect.disabled = Boolean(pendH || pendM);

  hSelect.onchange = () => setGlobal('harness', hSelect.value);
  mSelect.onchange = () => setGlobal('model', mSelect.value);

  const projects = Object.entries(ar.projects || {});
  const tbody = $('ar-tbody');
  if (projects.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No repositories monitored by auto-review</td></tr>';
  } else {
    tbody.innerHTML = projects.map(([repo, cfg]) => `
      <tr>
        <td class="mono" style="font-weight:600;">${esc(repo)}</td>
        <td><span class="badge ${cfg.paused ? 'badge-custom' : 'badge-green'}">${cfg.paused ? 'Paused' : 'Enabled'}</span></td>
        <td>${cfg.paused ? esc(cfg.paused_reason || '') + (cfg.paused_at ? ' <span style="color:var(--text-dim);">(' + esc(cfg.paused_at) + ')</span>' : '') : ''}</td>
        <td style="text-align:right;">
          <button class="btn-xs" title="Pause or resume automatic PR review for this repository." onclick="api('/api/autoreview', { args: ['toggle', '${esc(repo)}'] })">${cfg.paused ? 'Resume' : 'Pause'}</button>
          <button class="btn-xs btn-primary" title="Run one review cycle for this repository now instead of waiting for the schedule." onclick="api('/api/autoreview', { args: ['tick', '${esc(repo)}'] })">Trigger Tick</button>
        </td>
      </tr>
    `).join('');
  }

  const logsEl = $('ar-logs-output');
  if (logsEl) {
    const logs = ar.recent_logs || [];
    logsEl.textContent = logs.length > 0
      ? logs.join('\n')
      : (container.running ? 'No recent logs recorded' : 'Daemon container is stopped');
  }
}

$('btn-ar-sync').onclick = () => api('/api/autoreview', { args: ['sync-tokens'] });
$('btn-ar-restart').onclick = () => api('/api/autoreview', { args: ['restart'] });

onState(['block:autoreview', 'pending'], renderAutoReview);
