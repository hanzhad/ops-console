// Small helpers shared by every render module.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const escJs = s => esc(JSON.stringify(String(s ?? '')));
const ago = s => s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${(s / 3600).toFixed(1)}h ago` : `${Math.round(s / 86400)}d ago`;

// Deterministic pastel badge generator for dynamic open harnesses
function harnessBadge(harness) {
  const h = (harness || 'unknown').toLowerCase();
  if (h === 'antigravity') return '<span class="badge badge-antigravity">Antigravity</span>';
  if (h === 'claude') return '<span class="badge badge-claude">Claude Code</span>';
  return `<span class="badge badge-custom">${esc(h)}</span>`;
}

// Shows a failure that no control can show for itself.
function toast(message) {
  let box = document.getElementById('toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    box.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:9999;display:flex;flex-direction:column;gap:6px;max-width:420px;';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.textContent = message;
  el.style.cssText = 'padding:10px 14px;border-radius:6px;background:#3a1d1d;color:#ffd6d6;border:1px solid #f66;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.5);';
  box.appendChild(el);
  setTimeout(() => el.remove(), 8000);
}
