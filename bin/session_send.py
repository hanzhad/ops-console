"""Sending half of the session router: pick the target session, hand it the letter."""

import fcntl
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import agy_send
import claude_probe
import claude_send


def resolve_target(sessions: List[Dict[str, Any]], harness: str,
                   ident: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Find the one session of this harness whose id, ref or raw_ref is exactly ident.

    A leading local_ is optional on either side. No title and no prefix match."""
    bare = ident[6:] if ident.startswith("local_") else ident
    wanted = {bare, f"local_{bare}"}
    for s in sessions:
        if s.get("harness") != harness:
            continue
        if any(s.get(k) in wanted for k in ("id", "ref", "raw_ref")):
            return (s, None)
    return (None, f"no {harness} session {ident}")


def _claude_live_now(local_id: Optional[str]) -> Optional[bool]:
    """Fresh liveness of one Claude row, for the re-check taken under the resume lock."""
    for row in claude_probe.probe(True):
        if row.get("id") == local_id:
            return row.get("live")
    return None


def deliver(sess: Dict[str, Any], letter: str,
            wait: int = 0) -> Tuple[bool, str, Optional[str]]:
    """Hand a letter to the harness module that owns the session."""
    harness = sess.get("harness")
    session_ref = sess.get("ref") or sess.get("id") or ""
    if harness == "claude":
        route = claude_send.claude_route(sess.get("live"))
        if route == "relay":
            return claude_send.send(sess, session_ref, letter, wait)
        if route == "resume" and sess.get("cliSessionId"):
            ok, detail = claude_send.launch_resume(
                sess["cliSessionId"], letter, claude_exe=claude_send.CLAUDE_EXE,
                lock_dir=claude_send.LOCK_DIR, popen=subprocess.Popen, flock=fcntl.flock,
                liveness=lambda: _claude_live_now(sess.get("id")), wait=bool(wait),
                local_id=sess.get("id"))
            return (ok, detail, None)
        return (False, "Claude session liveness unknown; not sending", None)
    if harness == "antigravity":
        return agy_send.send(sess, session_ref, letter, wait)
    return (False, f"Harness '{harness}' does not support messaging", None)
