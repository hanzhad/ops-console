#!/usr/bin/env python3
"""Pure core of Claude liveness: live-session registry records + process table -> alive/dead/unknown.

No I/O here; claude_probe reads the registry and winproc queries the process table."""

from typing import Any, Dict, List, Optional, Tuple

Proc = Dict[int, Tuple[Optional[int], str]]


def _matches(record: Dict[str, Any], session: Dict[str, Any]) -> bool:
    if record.get("hostSessionId") and record.get("hostSessionId") == session.get("id"):
        return True
    cli = session.get("cliSessionId")
    return bool(cli) and record.get("sessionId") == cli


def _record_state(record: Dict[str, Any], proc: Optional[Proc], tol_ticks: int) -> Optional[bool]:
    domain = record.get("pidDomain")
    if domain and not str(domain).startswith("win32"):
        return None
    if proc is None:
        return None
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return None
    if pid not in proc:
        return False
    start, image = proc[pid]
    unsure = None if image == "claude" else False
    try:
        proc_start = int(record.get("procStart"))
    except (TypeError, ValueError):
        return unsure
    if start is None:
        return unsure
    if abs(start - proc_start) <= tol_ticks:
        return True
    return unsure


def claude_liveness(records: Optional[List[Dict[str, Any]]], local_sessions: List[Dict[str, Any]],
                    proc: Optional[Proc], tol_ticks: int = 10000) -> Dict[str, Optional[bool]]:
    """{local_id: True|False|None}. None = cannot tell (no registry, no process table, odd record)."""
    out: Dict[str, Optional[bool]] = {}
    for s in local_sessions:
        if records is None:
            out[s["id"]] = None
            continue
        states = [_record_state(r, proc, tol_ticks) for r in records if _matches(r, s)]
        if any(st is True for st in states):
            out[s["id"]] = True
        elif any(st is None for st in states):
            out[s["id"]] = None
        else:
            out[s["id"]] = False
    return out
