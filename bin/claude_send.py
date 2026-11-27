#!/usr/bin/env python3
"""Claude Code delivery: a headless Haiku relays the letter with SendMessage."""

import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

_WIN_USER = os.environ.get("OPS_CONSOLE_WIN_HOME", "/mnt/c/Users/user")
CLAUDE_EXE = os.environ.get("OPS_CONSOLE_CLAUDE_EXE", f"{_WIN_USER}/.local/bin/claude.exe")
CLAUDE_CWD = os.environ.get("OPS_CONSOLE_CLAUDE_CWD", f"{_WIN_USER}/AppData/Local/Temp")


def build_prompt(target: str, letter: str) -> str:
    if not target.startswith("local_"):
        target = f"local_{target}"
    return (f"SendMessage to {json.dumps(target)}, "
            f"saying: {json.dumps(letter)}. You do nothing else.")


def send(sess: Dict[str, Any], session_ref: str, letter: str,
         wait: int = 0) -> Tuple[bool, str, Optional[str]]:
    """Deliver letter to a Claude session. Returns (ok, detail, reply_or_None)."""
    prompt = build_prompt(session_ref, letter)
    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "WSLENV": "MSYS_NO_PATHCONV"}
    try:
        p = subprocess.run(
            [CLAUDE_EXE, "-p", "--model", "haiku", "--strict-mcp-config", "--no-session-persistence",
             "--setting-sources", "local", "--tools", "SendMessage", "--allowedTools", "SendMessage(*)"],
            input=prompt, capture_output=True, text=True, timeout=90, cwd=CLAUDE_CWD, env=env
        )
        out = (p.stdout.strip() or p.stderr.strip())[-300:]
        return (p.returncode == 0, out or "Claude message sent", None)
    except Exception as e:
        return (False, f"Claude send error: {e}", None)


LOCK_DIR = pathlib.Path.home() / ".cache" / "xmsg" / "locks"


def claude_route(liveness: Optional[bool]) -> str:
    """Pure: a live session takes the relay, a dead one a resume, an unknown one is refused."""
    if liveness is True:
        return "relay"
    if liveness is False:
        return "resume"
    return "refuse"


def resume_argv(claude_exe: str, cli_id: str) -> List[str]:
    return [claude_exe, "-p", "--resume", cli_id, "--model", "haiku", "--tools", ""]


def _kill_group(proc: Any) -> None:
    """Kill the worker and anything it started (it runs in its own session), then reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        proc.kill()
    proc.communicate()


LOG_MAX_BYTES = 64 * 1024


def _open_log(lock_dir: pathlib.Path, cli_session_id: str) -> Any:
    """Open the session's worker log for append, starting it over when past LOG_MAX_BYTES, and
    write a timestamped header for this launch. The caller closes it."""
    path = lock_dir / f"{cli_session_id}.log"
    try:
        too_big = path.stat().st_size > LOG_MAX_BYTES
    except OSError:
        too_big = False
    log = open(path, "w" if too_big else "a", encoding="utf-8")
    log.write(f"--- launch {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ---\n")
    log.flush()
    return log


def launch_resume(cli_session_id: str, letter: str, *, claude_exe: str, lock_dir: Any,
                  popen: Callable[..., Any], flock: Callable[[int, int], Any],
                  liveness: Callable[[], Optional[bool]],
                  wait: bool = False, local_id: Optional[str] = None, timeout: float = 300,
                  worker_path: Optional[str] = None) -> Tuple[bool, str]:
    """Resume a dead session with the letter under a per-session lock the worker inherits.

    local_id is the row id the worker signs its reply with. With wait, the worker's printed reply
    is returned; a worker past timeout is killed and reported, never raised."""
    lock_dir = pathlib.Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / f"{cli_session_id}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return (False, f"session {cli_session_id} is already resuming")
        if liveness() is not False:
            return (False, "session became live or its liveness is unknown; not resuming")
        worker = worker_path or str(pathlib.Path(__file__).resolve().with_name("claude_resume_worker.py"))
        argv = [sys.executable, worker, claude_exe, cli_session_id] + ([local_id] if local_id else [])
        log = _open_log(lock_dir, cli_session_id)
        log_path = lock_dir / f"{cli_session_id}.log"
        try:
            return _spawn(argv, letter, fd, log, log_path, popen, wait, timeout)
        finally:
            log.close()
    finally:
        os.close(fd)  # the worker holds its own copy of the open lock


def _spawn(argv: List[str], letter: str, fd: int, log: Any, log_path: pathlib.Path,
           popen: Callable[..., Any], wait: bool, timeout: float) -> Tuple[bool, str]:
    """Start the worker; its stderr always reaches the log, and with wait is also returned."""
    if wait:
        proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, pass_fds=[fd], start_new_session=True)
        try:
            out, err = proc.communicate(input=letter, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            log.write(f"resume timed out after {timeout}s; worker killed\n")
            return (False, f"resume timed out after {timeout}s; worker killed")
        log.write((err or "") + (out or ""))
        if proc.returncode != 0:
            return (False, ((err or out or "").strip() or f"worker exited {proc.returncode}")[-300:])
        return (True, (out or "").strip() or "resumed")
    proc = popen(argv, stdin=subprocess.PIPE, stdout=log,
                 stderr=log, text=True, pass_fds=[fd], start_new_session=True)
    if proc.stdin is not None:
        proc.stdin.write(letter)
        proc.stdin.close()
    return (True, f"resuming; log: {log_path}")
