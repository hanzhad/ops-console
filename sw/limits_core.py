"""Pure limits transformations for limits.json: zero I/O, never mutates caller inputs."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any, Optional, Tuple

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import limit_keys  # noqa: E402

_DELTA_RE = re.compile(r"[+-]?[0-9]+")
MAX_LIMIT = 20


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clamp(value: int) -> int:
    return min(MAX_LIMIT, max(0, value))


def parse_int_delta(raw: str) -> int:
    if not isinstance(raw, str) or not _DELTA_RE.fullmatch(raw):
        raise ValueError(f"invalid integer delta: {raw!r}")
    return int(raw)


def parse_limit_op(op: str) -> Optional[int]:
    if not isinstance(op, str):
        raise ValueError(f"invalid limit operation: {op!r}")
    if op == "pause":
        return None
    if _DELTA_RE.fullmatch(op):
        return int(op)
    raise ValueError(f"invalid limit operation: {op!r}")


def parse_limits(raw: Optional[str]) -> dict:
    if raw is None:
        return {"default": 1}
    if not isinstance(raw, str):
        raise ValueError("limits JSON must be a string or None")
    if raw.strip() == "":
        return {"default": 1}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid limits JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("limits JSON must be a JSON object")
    return dict(parsed)


def apply_machine_max(limits: dict, op: str) -> Tuple[dict, int]:
    if not isinstance(limits, dict):
        raise ValueError("limits must be a dict")
    delta = parse_limit_op(op)
    raw_cur = limits.get("machine_max", MAX_LIMIT)
    cur = raw_cur if _is_int(raw_cur) else MAX_LIMIT
    new_val = 0 if delta is None else _clamp(cur + delta)
    updated = dict(limits)
    updated["machine_max"] = new_val
    return updated, new_val


def apply_session_limit(
    limits: dict,
    ref: str,
    op: int | str,
    legacy_sessions: Optional[dict] = None,
) -> Tuple[dict, int]:
    if not isinstance(limits, dict):
        raise ValueError("limits must be a dict")
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"invalid session ref: {ref!r}")
    if _is_int(op):
        delta: Optional[int] = op
    elif isinstance(op, str):
        delta = parse_limit_op(op)
    else:
        raise ValueError(f"limit operation must be a non-bool int or string: {op!r}")
    raw_sessions = limits.get("sessions")
    sessions = dict(raw_sessions) if isinstance(raw_sessions, dict) else {}
    raw_default = limits.get("default", 1)
    default_val = _clamp(raw_default) if _is_int(raw_default) else 1
    key = limit_keys.find_key(sessions, ref)
    if key is not None:
        raw_cur = sessions[key]
        cur = _clamp(raw_cur) if _is_int(raw_cur) else default_val
    else:
        legacy = legacy_sessions if isinstance(legacy_sessions, dict) else {}
        lkey = limit_keys.find_key(legacy, ref)
        cur = _clamp(legacy[lkey]) if (lkey is not None and _is_int(legacy[lkey])) else default_val
        key = ref
    new_val = 0 if delta is None else _clamp(cur + delta)
    if new_val == limits.get("default"):
        sessions.pop(key, None)
    else:
        sessions[key] = new_val
    updated = dict(limits)
    updated["sessions"] = sessions
    return updated, new_val
