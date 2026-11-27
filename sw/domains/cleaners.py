"""Dev-container engine cleaners (/api/cleaners) and rescue archives (/api/rescue)."""

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import engine as engine_mod
from sw.commands import RouteSpec
from sw.ctx import note
from sw.watch import BlockSpec

PERIOD = 30


def read_cleaners(store):
    return {"cleaners": engine_mod.get_cleaners_status(), "rescue_bundles": engine_mod.get_rescue_archives(limit=50)}


def validate_cleaners(body):
    action = str(body.get("action", "")).strip().lower()
    return (action,) if action in ("fast", "slow") else None


def do_cleaner(args):
    (action,) = args
    note(f"cleaner {action} pass initiated…")
    try:
        ok, out = engine_mod.trigger_cleaner(action)
        note(f"cleaner {action}: {'OK' if ok else 'failed'} — {out[:140]}")
    except Exception as e:
        ok, out = False, str(e)
        note(f"cleaner {action} exception: {e}"[:200])
    return (0 if ok else 1), str(out)[-300:]


def validate_rescue(body):
    action = str(body.get("action", "")).strip().lower()
    bid = str(body.get("target", "")).strip()
    if not bid and "project" in body and "lease" in body:
        bid = f"{body['project']}/{body['lease']}"
    if action not in ("restore", "purge", "verify_all", "purge_delivered", "verdict", "agent_verdict") or not bid:
        return None
    return action, bid


def do_rescue(args):
    action, bundle_id = args
    note(f"rescue {action} {bundle_id}…")
    try:
        ok, out = engine_mod.manage_rescue(action, bundle_id)
        note(f"rescue {action} {bundle_id}: {'OK' if ok else 'failed'} — {out[:140]}")
    except Exception as e:
        ok, out = False, str(e)
        note(f"rescue {action} {bundle_id} exception: {e}"[:200])
    return (0 if ok else 1), str(out)[-300:]


BLOCKS = [BlockSpec("cleaners", PERIOD, read_cleaners)]
ROUTES = {
    "/api/cleaners": RouteSpec(validate_cleaners, do_cleaner, lambda args: ("cleaners",), 960, wakes=lambda args: ("slots",)),
    "/api/rescue": RouteSpec(validate_rescue, do_rescue, lambda args: ("cleaners",), 960, wakes=lambda args: ("slots",)),
}
