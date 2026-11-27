#!/usr/bin/env python3
"""Resume worker: run the letter through a resumed Claude and mail the answer back to the sender."""

import subprocess
import sys
from typing import Callable, List, Optional, TextIO

import claude_send
import xmsg_envelope as xe


def handle(letter_text: str, cli_session_id: str, *, claude_exe: str,
           run: Callable[[list, str], str], send: Callable[[str, str], None],
           local_id: Optional[str] = None) -> bool:
    """True if a reply was sent. No run at the hop limit, no send on empty output.

    local_id is this session's row id, the address a recipient can resolve; the CLI id is only
    the fallback when the caller does not know it."""
    parsed = xe.parse_envelope(letter_text)
    if parsed is None:
        return False
    allowed, hops = xe.check_hops(parsed["hops"])
    if not allowed or parsed["hops"] >= xe.MAX_HOPS:
        return False
    out = (run(claude_send.resume_argv(claude_exe, cli_session_id), letter_text) or "").strip()
    if not out:
        return False
    to = f"{parsed['from_harness']}:{parsed['from_id']}"
    send(to, xe.build_envelope(f"claude:{local_id or cli_session_id}", False, hops, out))
    return True


def _run(argv: list, input_text: str) -> str:
    p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=600)
    return p.stdout


def _send(to: str, text: str) -> None:
    """Deliver the reply; raise RuntimeError with the reason when it cannot be delivered."""
    import sessions
    harness, _, ref = to.partition(":")
    sess, why = sessions.resolve_target(sessions.discover_sessions(liveness=True), harness, ref)
    if not sess:
        raise RuntimeError(f"cannot deliver reply to {to}: {why or 'target not found'}")
    ok, detail, _ = sessions.deliver(sess, text)
    if not ok:
        raise RuntimeError(f"cannot deliver reply to {to}: {detail}")


def main(argv: List[str], letter_text: str, *, run: Callable[[list, str], str],
         send: Callable[[str, str], None], out: TextIO, err: Optional[TextIO] = None) -> bool:
    """argv is [claude_exe, cli_session_id, optional local_id]. The reply is also printed to out,
    so a caller waiting on this process gets it. A run or send failure is written to err
    (stderr) as `claude_resume_worker: <reason>` and the process exits 1."""
    def send_and_print(to: str, text: str) -> None:
        send(to, text)
        parsed = xe.parse_envelope(text)
        out.write((parsed["body"] if parsed else text).strip() + chr(10))
        out.flush()

    try:
        return handle(letter_text, argv[1], claude_exe=argv[0], run=run, send=send_and_print,
                      local_id=argv[2] if len(argv) > 2 else None)
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        (err or sys.stderr).write(f"claude_resume_worker: {e}" + chr(10))
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:], sys.stdin.read(), run=_run, send=_send, out=sys.stdout)
