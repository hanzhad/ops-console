"""One key per session in limits.json, same rule as the engine (agent-dev-slots#37).

Strip one leading `local_` from both the key and the id. The exact key wins, else the other spelling,
else the default. The 6-character prefix/suffix keys the console once tried match nothing in the engine and
are gone.
"""
import json
import pathlib

PREFIX = "local_"


def norm(s):
    return s[len(PREFIX):] if isinstance(s, str) and s.startswith(PREFIX) else s


def find_key(table, *refs):
    """The existing key in `table` for a session known as any of `refs`, or None. Exact first, then other spelling."""
    refs = [r for r in refs if r]
    for r in refs:
        if r in table:
            return r
    for r in refs:
        n = norm(r)
        for cand in (n, PREFIX + n):
            if cand in table:
                return cand
    return None


def spellings(*refs):
    out = set()
    for r in refs:
        if r:
            out.add(norm(r))
            out.add(PREFIX + norm(r))
    return out


def read_json(path):
    try:
        d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
