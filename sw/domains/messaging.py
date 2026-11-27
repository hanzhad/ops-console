"""Polymorphic messaging across harnesses (/api/send). No zone: the command is ok when the action returns."""

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import sessions as sessions_mod
from sw.commands import RouteSpec
from sw.ctx import note


def validate_send(body):
    to = body.get("to", [])
    text = str(body.get("text", "")).strip()
    if not to or not isinstance(to, list) or not (0 < len(text) <= 2000):
        return None
    return to, text


def do_send(args):
    recipients, text = args
    note(f"dispatching message to {len(recipients)} session(s)…")
    success_count = 0
    for r in recipients:
        if isinstance(r, dict):
            ref = r.get("ref") or r.get("id") or r.get("title", "")
            harness = r.get("harness")
        else:
            ref = str(r)
            harness = None
        ok, out = sessions_mod.send_message(ref, text, harness=harness)
        if ok:
            success_count += 1
            note(f"send to {ref}: delivered")
        else:
            note(f"send to {ref} failed: {out[:100]}")
    return (0 if success_count == len(recipients) else 1), f"delivered to {success_count}/{len(recipients)}"


BLOCKS = []
ROUTES = {"/api/send": RouteSpec(validate_send, do_send, lambda args: (), 30)}
