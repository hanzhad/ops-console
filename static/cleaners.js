// Engine cleaners and rescue archives.
function renderCleaners() {
  const c = S.cleaners || {};
  const fast = c.fast || {};
  const slow = c.slow || {};

  const fastResult = fast.result || fast.what_it_did || fast.status || '';
  const fastNext = fast.next_planned_run
    ? `Next: ${new Date(fast.next_planned_run * 1000).toLocaleTimeString()} (${fast.next_due_in_seconds ?? 0}s)`
    : `Next due in ${fast.next_due_in_seconds ?? 0}s`;
  $('fast-cleaner-status').textContent = fastNext;
  $('fast-cleaner-last').textContent = fast.last_run ? `Last run: ${ago(Date.now()/1000 - fast.last_run)}` : 'Never run';
  $('fast-cleaner-log').textContent = fast.last_log || (fastResult ? `Result: ${fastResult}` : 'OK');

  const slowResult = slow.result || slow.what_it_did || slow.status || '';
  const slowNext = slow.next_planned_run
    ? `Next: ${new Date(slow.next_planned_run * 1000).toLocaleTimeString()} (${slow.next_due_in_seconds ?? 0}s)`
    : `Next due in ${slow.next_due_in_seconds ?? 0}s`;
  $('slow-cleaner-status').textContent = slowNext;
  $('slow-cleaner-last').textContent = slow.last_run ? `Last run: ${ago(Date.now()/1000 - slow.last_run)}` : 'Never run';
  $('slow-cleaner-log').textContent = slow.last_log || (slowResult ? `Result: ${slowResult}` : 'OK');
}

function renderRescue() {
  const bundles = S.rescue_bundles || [];
  const tbody = $('rescue-tbody');
  $('rescue-count').textContent = `${bundles.length} archives recorded`;

  if (bundles.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No rescue archives recorded</td></tr>';
    return;
  }

  tbody.innerHTML = bundles.map(b => {
    const aid = b.id || (b.project + '/' + (b.lease || b.lease_id || ''));
    const rootBranches = (b.branches || []).filter(Boolean);
    const repos = (b.repos || []).filter(r => r && r.alias);
    const repoLabels = repos.map(r => {
      const rb = (r.branches || []).filter(Boolean);
      return `${r.alias} (${r.mode || 'ro'}${rb.length > 0 ? ': ' + rb.join(', ') : ''})`;
    });
    const summaryParts = [...(rootBranches.length ? [rootBranches.join(', ')] : []), ...repoLabels];
    const branchText = summaryParts.length ? summaryParts.join(', ') : '–';
    const branchHtml = summaryParts.length
      ? [
          ...(rootBranches.length ? [esc(rootBranches.join(', '))] : []),
          ...repoLabels.map(lbl => `<span class="badge badge-custom">${esc(lbl)}</span>`),
        ].join(' ')
      : '–';
    const hasVerdict = Boolean(b.has_verdict || b.verdict);
    const verdictBadge = hasVerdict
      ? '<span class="badge badge-green" title="' + esc(b.verdict_summary || 'Verdict generated') + '">Verdict ✓</span>'
      : '<span class="badge badge-custom">No Verdict</span>';

    const res = S.results?.[aid] || S.results?.[b.id];
    const resMsg = res ? `<div style="font-size:10px;color:${res.ok ? 'var(--status-green)' : 'var(--status-red)'};margin-top:2px;">${esc(res.message || (res.ok ? 'Success' : 'Failed'))}</div>` : '';

    const ageStr = b.age ? ago(b.age) : (b.created_at ? ago(Date.now()/1000 - b.created_at) : '–');
    const sizeStr = b.size_mb ? `${b.size_mb} MB` : (b.size_bytes ? `${Math.round(b.size_bytes / 1024)} KB` : (b.size ? `${b.size} B` : '–'));

    return `
      <tr>
        <td class="mono" style="font-weight:600;" title="${esc(aid)}">
          ${esc(aid)}
          ${resMsg}
        </td>
        <td class="mono" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;" title="${esc(branchText)}">${branchHtml}</td>
        <td>${ageStr}</td>
        <td class="mono">${sizeStr}</td>
        <td>${verdictBadge}</td>
        <td style="text-align:right;white-space:nowrap;">
          <button class="btn-xs btn-primary" title="Send archive to agents for verdict.md. Inspects meta.json, patch, and bundle without deleting any work." onclick="api('/api/rescue', { action: 'verdict', target: '${esc(aid)}' })">Send to Agents</button>
          <button class="btn-xs" title="Check that this archive is readable and the work can be restored from it. Deletes nothing." onclick="api('/api/rescue', { action: 'restore', target: '${esc(aid)}' })">Verify</button>
        </td>
      </tr>
    `;
  }).join('');
}

function verifyAllRescue(btn) {
  if (btn) {
    btn.textContent = 'verifying all…';
    btn.disabled = true;
  }
  api('/api/rescue', { action: 'verify_all', target: 'all' });
  setTimeout(() => {
    if (btn) {
      btn.textContent = 'triggered ✓';
      setTimeout(() => {
        btn.textContent = 'Verify All';
        btn.disabled = false;
      }, 3000);
    }
  }, 1000);
}

function purgeAllDelivered(btn) {
  if (confirm('Удалить все проверенные доставленные rescue-архивы (Delivered)? Архивы с уникальной неслитой работой (Unmerged) затронуты НЕ будут.')) {
    if (btn) {
      btn.textContent = 'purging…';
      btn.disabled = true;
    }
    api('/api/rescue', { action: 'purge_delivered', target: 'delivered' });
    setTimeout(() => {
      if (btn) {
        btn.textContent = 'Purge Delivered';
        btn.disabled = false;
      }
    }, 4000);
  }
}

$('btn-fast-reap').onclick = () => api('/api/cleaners', { action: 'fast' });
$('btn-slow-check').onclick = () => api('/api/cleaners', { action: 'slow' });

onState(['block:cleaners'], renderCleaners);
onState(['block:cleaners', 'commands'], renderRescue);
