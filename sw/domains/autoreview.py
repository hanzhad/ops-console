"""Auto-review daemon route (/api/autoreview) and the autoreview block. set-global is an ordinary 202 command like the rest."""

import re

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import autoreview as ar_mod
from sw.commands import RouteSpec
from sw.ctx import action_lock, note, run_bin
from sw.watch import BlockSpec

PERIOD = 15

REPO = r"[\w.-]+/[\w.-]+"


def validate_autoreview(body):
    args = body.get("args", [])
    if not isinstance(args, list):
        return None
    args = [str(a) for a in args]
    valid_simple = args in (["start"], ["stop"], ["restart"], ["sync-tokens"], ["pause"], ["resume"])
    valid_global = len(args) == 3 and args[0] == "set-global" and args[1] in ("harness", "model") and len(args[2]) <= 80
    valid_repo_single = len(args) == 2 and args[0] in ("toggle", "forget", "tick") and re.fullmatch(REPO, args[1])
    valid_set = len(args) == 4 and args[0] == "set" and re.fullmatch(REPO, args[1]) and args[2] in ("all", "self_review") and args[3] in ("true", "false")
    valid_adopt = 2 <= len(args) <= 4 and args[0] == "adopt" and re.fullmatch(REPO, args[1]) and all(a in ("--all", "--self-review") for a in args[2:])
    return args if (valid_simple or valid_global or valid_repo_single or valid_set or valid_adopt) else None


def do_autoreview(args: list):
    with action_lock:
        rc, out = run_bin("autoreview.py", *args)
    note(f"Auto-review {' '.join(args)}: {out}")
    return rc, out


def read_autoreview(store):
    return ar_mod.status()


BLOCKS = [BlockSpec("autoreview", PERIOD, read_autoreview)]
ROUTES = {"/api/autoreview": RouteSpec(validate_autoreview, do_autoreview, lambda args: ("autoreview",), 90)}
