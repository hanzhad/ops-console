// Action log.
function renderLog() {
  const events = S.events || [];
  $('log-output').textContent = events.length > 0 ? events.join('\n') : 'No actions recorded in log.';
}

$('btn-clear-log').onclick = () => {
  store.logs.length = 0;
  S = assembleState();
  if (S) renderLog();
};

onState(['log'], renderLog);
