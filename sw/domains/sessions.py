"""Sessions block (every harness's active sessions, held slot counts, and per-session limits)."""

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import sessions as sessions_mod
from sw import core
from sw.commands import RouteSpec
from sw.ctx import action_lock, note, run_bin
from sw.watch import BlockSpec

PERIOD = 10


def read_sessions(store):
    limits = sessions_mod.read_limits()
    rows = sessions_mod.discover_sessions(leases=sessions_mod.read_all_leases(), limits=limits)
    return core.sessions_block(rows, limits)


def validate_limit(body):
    ref = body.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    if body.get("mode") == "pause":
        return (ref, "pause")
    delta = body.get("delta")
    if isinstance(delta, int) and not isinstance(delta, bool) and delta in (-1, 1):
        return (ref, delta)
    return None


def do_limit(args):
    ref, op = args
    with action_lock:
        rc, out = run_bin("setlimit.py", ref, str(op), timeout=20)
    label = "pause" if op == "pause" else f"{op:+d}"
    note(f"session {ref} limit {label} → {out}" if rc == 0 else f"session {ref} limit failed: {out}")
    return rc, out


BLOCKS = [BlockSpec("sessions", PERIOD, read_sessions)]
ROUTES = {"/api/limit": RouteSpec(validate_limit, do_limit, lambda args: ("sessions",), 30)}
