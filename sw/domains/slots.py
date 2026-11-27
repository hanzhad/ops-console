"""Slots block, and the slot routes: the machine cap (/api/cap) and releasing or evicting a slot (/api/evict)."""

import json

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import sessions as sessions_mod
import slots as slots_mod
from sw import core
from sw.commands import RouteSpec
from sw.ctx import action_lock, note, run_bin
from sw.watch import BlockSpec

PERIOD = 10


def read_slots(store):
    """The docker sample comes from the host block and the session list from the sessions block: no second read of either."""
    stats = (store.block_data("host") or {}).get("stats", {})
    sessions = (store.block_data("sessions") or {}).get("sessions", [])
    return core.slots_block(slots_mod.discover_slots(stats, sessions), sessions_mod.read_limits())


def validate_cap(body):
    if body.get("mode") == "pause":
        return ("pause",)
    delta = body.get("delta")
    if isinstance(delta, int) and not isinstance(delta, bool) and delta in (-1, 1):
        return (delta,)
    return None


def do_cap(args):
    (op,) = args
    with action_lock:
        rc, out = run_bin("setcap.py", op, timeout=20)
    label = "pause" if op == "pause" else f"{op:+d}"
    note(f"machine cap {label} → {out}" if rc == 0 else f"machine cap failed: {out}")
    return rc, out


def validate_evict(body):
    sid = str(body.get("target", "")).strip()
    mode = str(body.get("mode", "")).strip()
    return (sid, mode) if sid else None


def precheck_evict(args, store):
    sid, _mode = args
    live = (store.block_data("slots") or {}).get("slots", [])
    if sid not in {s["id"] for s in live}:
        return 400, f"slot {sid} not active"
    for c in store.commands("running"):
        if c["route"] == "/api/evict" and json.loads(c["body"]).get("target") == sid:
            return 409, "already running"
    return None


def do_evict(args):
    sid, mode = args
    note(f"{mode} {sid}…")
    try:
        rc, out = run_bin("evict.py", sid, mode, timeout=900)
    except Exception as e:
        rc, out = 1, str(e)[:300]
    note(f"{mode} {sid}: {out}" if rc == 0 else f"{mode} {sid} refused: {out}")
    return rc, out


BLOCKS = [BlockSpec("slots", PERIOD, read_slots, after=("host", "sessions"))]
ROUTES = {
    "/api/cap": RouteSpec(validate_cap, do_cap, lambda args: ("slots",), 30),
    "/api/evict": RouteSpec(validate_evict, do_evict, lambda args: ("slots",), 960, precheck_evict,
                          wakes=lambda args: ("sessions", "cleaners")),
}
