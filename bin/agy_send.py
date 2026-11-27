#!/usr/bin/env python3
"""Antigravity delivery via Antigravity's language_server.exe agentapi CLI."""

import os
import pathlib
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_AGENTAPI_BIN = "/mnt/c/Users/user/AppData/Local/Programs/antigravity/resources/bin/language_server.exe"
DEFAULT_AGENTAPI_BAT = "/mnt/c/Users/user/.gemini/antigravity/bin/agentapi.bat"
SEND_TIMEOUT = 30

_BAT_QUOTED_EXE = re.compile(r'"([A-Za-z]:[\\/][^"\r\n]+?\.exe)"', re.IGNORECASE)


def resolve_conversation_id(sess: Optional[Dict[str, Any]],
                            session_ref: Optional[str]) -> str:
    """Pure: extract the Antigravity conversation ID and strip any leading 'local_' prefix."""
    s = sess if isinstance(sess, dict) else {}
    raw = session_ref or s.get("raw_ref") or s.get("id") or ""
    if not isinstance(raw, str):
        return ""
    cid = raw[6:] if raw.startswith("local_") else raw
    return cid if cid.strip() else ""


def win_to_wsl_path(path: str) -> str:
    """Pure: convert a Windows '<Drive>:\\...' path to '/mnt/<drive>/...'."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", (path or "").strip())
    if not m:
        return path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/").lstrip("/")
    return f"/mnt/{drive}/{rest}"


def parse_agentapi_bat(text: str) -> Optional[str]:
    """Pure: parse the quoted Windows .exe path out of agentapi.bat text as a WSL path."""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("::"):
            continue
        no_at = line[1:].lstrip() if line.startswith("@") else line
        if no_at[:3].lower() == "rem" and (len(no_at) == 3 or no_at[3].isspace()):
            continue
        for m in _BAT_QUOTED_EXE.finditer(line):
            exe = win_to_wsl_path(m.group(1))
            if os.path.basename(exe).lower() != "cmd.exe":
                return exe
    return None


def build_send_argv(cid: str, letter: str,
                    language_server_bin: Optional[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Pure: build direct argv for language_server.exe (never cmd.exe or a .bat file)."""
    if language_server_bin:
        return ([language_server_bin, "agentapi", "send-message", cid, letter], None)
    return (None, "Antigravity agentapi binary not found")


def _resolve_binary() -> Tuple[Optional[str], Optional[str]]:
    """Resolve language_server.exe directly via env override, default path, or agentapi.bat text."""
    env_bin = os.environ.get("AGY_AGENTAPI_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin):
        return (env_bin, None)
    if os.path.isfile(DEFAULT_AGENTAPI_BIN):
        return (DEFAULT_AGENTAPI_BIN, None)
    if os.path.isfile(DEFAULT_AGENTAPI_BAT):
        try:
            bat_text = pathlib.Path(DEFAULT_AGENTAPI_BAT).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            return (None, f"Failed to read {DEFAULT_AGENTAPI_BAT}: {e}")
        bat_exe = parse_agentapi_bat(bat_text)
        if not bat_exe:
            return (None, f"Could not parse language_server.exe path from {DEFAULT_AGENTAPI_BAT}")
        if os.path.isfile(bat_exe):
            return (bat_exe, None)
        return (None, f"Antigravity agentapi binary from agentapi.bat not found: {bat_exe}")
    return (None, "Antigravity agentapi binary not found")


def send(sess: Optional[Dict[str, Any]], session_ref: Optional[str], letter: Optional[str],
         wait: int = 0) -> Tuple[bool, str, Optional[str]]:
    """Deliver letter to an Antigravity session. Returns (ok, detail, reply_or_None)."""
    del wait  # accepted for uniform harness send() signature
    cid = resolve_conversation_id(sess, session_ref)
    if not cid:
        return (False, "Antigravity conversation ID is missing", None)
    if not isinstance(letter, str) or not letter:
        return (False, "Antigravity message letter is empty", None)
    exe, err = _resolve_binary()
    argv, argv_err = build_send_argv(cid, letter, exe)
    if not argv:
        return (False, err or argv_err or "Antigravity agentapi binary not found", None)
    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=SEND_TIMEOUT,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return (False, f"Antigravity send timed out after {SEND_TIMEOUT}s", None)
    except Exception as e:
        return (False, f"Antigravity send error: {e}", None)
    if res.returncode == 0:
        out = (res.stdout or "").strip() or (res.stderr or "").strip()
        return (True, out or "Antigravity message sent", None)
    err_out = (res.stderr or "").strip() or (res.stdout or "").strip()
    return (False, (err_out or f"agentapi exited {res.returncode}")[-300:], None)
