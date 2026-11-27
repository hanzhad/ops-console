#!/usr/bin/env python3
"""setlimit.py <ref> <delta|pause>: move a per-session slot limit in ~/.dev-containers/limits.json (ops-console#49).

Accepts a non-empty session ref and "pause" (sets 0) or an integer delta.
Starts from sessions[key], else clamped default (default 1); the result stays in 0..20.
Every other key in the file is kept. Prints the new limit.
"""

import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parent
ROOT = BIN.parent
for p in (str(ROOT), str(BIN)):
    if p not in sys.path:
        sys.path.insert(0, p)

from limits_store import default_state_dir, update_limits  # noqa: E402
from sw.limits_core import apply_session_limit, parse_limit_op  # noqa: E402


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: setlimit.py <ref> <delta|pause>", file=sys.stderr)
        return 2
    ref, op = args
    try:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"invalid session ref: {ref!r}")
        parse_limit_op(op)
        new = update_limits(default_state_dir(), lambda doc: apply_session_limit(doc, ref, op))
    except ValueError as exc:
        print(f"invalid limit arguments: {exc}", file=sys.stderr)
        return 2
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
