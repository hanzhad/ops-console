#!/usr/bin/env python3
"""setcap.py <delta|pause>: move the machine-wide slot cap, `machine_max` in ~/.dev-containers/limits.json (ops-console#18, #31, #33).

Accepts "pause" (sets 0) or an integer delta. Starts from the file value, else 20; the result stays in 0..20.
Every other key in the file is kept. Prints the new cap.
"""

import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parent
ROOT = BIN.parent
for p in (str(ROOT), str(BIN)):
    if p not in sys.path:
        sys.path.insert(0, p)

from limits_store import default_state_dir, update_limits  # noqa: E402
from sw.limits_core import apply_machine_max, parse_limit_op  # noqa: E402


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: setcap.py <delta|pause>", file=sys.stderr)
        return 2
    op = args[0]
    try:
        parse_limit_op(op)
        new = update_limits(default_state_dir(), lambda doc: apply_machine_max(doc, op))
    except ValueError as exc:
        print(f"invalid cap operation: {exc}", file=sys.stderr)
        return 2
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
