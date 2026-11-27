#!/usr/bin/env bash
# Wait until no CI runner is busy for two checks in a row, then rebuild and restart the gate, then raise the PIN window.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CI_REPO="${OPS_CONSOLE_HOST_REPO:-$HOME/Projects/docker-ci-gate}"
UNLOCK_TASK="${OPS_CONSOLE_UNLOCK_TASK:-pin-vault unlock}"
log=~/.cache/ops-console/regate.log; exec >>"$log" 2>&1
echo "== $(date '+%F %T') waiting for CI to go quiet"
quiet=0; n=0
while :; do
  busy=$(python3 "$SCRIPT_DIR/bin/runners.py" 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(1 for c in d or [] if c.get("busy")))' 2>/dev/null || echo 9)
  if [ "$busy" = 0 ]; then quiet=$((quiet + 1)); else quiet=0; fi
  [ "$quiet" -ge 2 ] && break
  n=$((n + 1)); [ "$n" -ge 480 ] && { echo "✗ $(date '+%T') still busy after 8h, giving up"; exit 1; }
  sleep 60
done
echo "== $(date '+%T') quiet; updating $CI_REPO"
cd "$CI_REPO" || exit 1
[ -z "$(git status --porcelain)" ] || { echo "✗ clone not clean, stopping"; exit 1; }
git fetch -q origin && git merge -q --ff-only origin/main || { echo "✗ ff failed"; exit 1; }
git log --oneline -1
./ci-runner.sh redeploy --yes || { echo "✗ redeploy failed"; ./ci-runner.sh resume; exit 1; }
echo "== $(date '+%T') gate up on the new image; opening the PIN window"
/mnt/c/Windows/System32/schtasks.exe /Run /TN "$UNLOCK_TASK"
echo "== done"
