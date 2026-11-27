"""CI gate route (/api/ci) and the ci_gate and gh_queue blocks. bin/ci.py shells out to ci-runner.sh."""

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import ci as ci_mod
from sw import core
from sw.commands import RouteSpec
from sw.ctx import action_lock, note, run_bin
from sw.watch import BlockSpec

PERIOD = 60


def validate_ci(body):
    args = body.get("args", [])
    if not isinstance(args, list):
        return None
    args = [str(a) for a in args]
    return args if core.validate_ci(args) else None


def do_ci(args: list):
    with action_lock:
        rc, out = run_bin("ci.py", *args)
    note(f"CI {' '.join(args)}: {out}")
    return rc, out


def read_ci_gate(store):
    return ci_mod.gate_status()


def read_gh_queue(store):
    """The queued runs come from the ci_gate block (ci-runner.sh status --json), with 0 GitHub API calls. Returns None when unknown."""
    gate = store.block_data("ci_gate") or {}
    if gate.get("queue_unknown"):
        return None
    return core.sort_queue(gate.get("queue") or [])


BLOCKS = [
    BlockSpec("ci_gate", PERIOD, read_ci_gate),
    BlockSpec("gh_queue", PERIOD, read_gh_queue, after=("ci_gate",)),
]
ROUTES = {"/api/ci": RouteSpec(validate_ci, do_ci, lambda args: ("ci_gate",), 150)}
