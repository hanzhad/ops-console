#!/usr/bin/env python3
"""Windows process table for pid checks from WSL (os.kill cannot see Windows pids).

parse_process_table is pure; resolve_windows_exe and query_windows_processes handle WSL/systemd lookup."""

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

POWERSHELL_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_LINE = re.compile(r"^\s*(\d+)(?: (\d+)(?=\s)| )\s+(\S+)\s*$")


def resolve_windows_exe(name: str, fallback: str) -> Optional[str]:
    """Resolve a Windows executable via PATH, falling back to a known System32 path under systemd."""
    exe = shutil.which(name)
    if exe:
        return exe
    if fallback and os.path.isfile(fallback):
        return fallback
    return None


def parse_process_table(text: str) -> Dict[int, Tuple[Optional[int], str]]:
    """Parse "pid filetime name" lines into {pid: (filetime_or_None, lowercase name sans .exe)}."""
    table: Dict[int, Tuple[Optional[int], str]] = {}
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        name = m.group(3).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        table[int(m.group(1))] = (int(m.group(2)) if m.group(2) else None, name)
    return table


def query_windows_processes(pids: List[int],
                            timeout: int = 15) -> Optional[Dict[int, Tuple[Optional[int], str]]]:
    """One powershell call for all pids. {} for no pids (nothing spawned), None on any failure."""
    if not pids:
        return {}
    exe = resolve_windows_exe("powershell.exe", POWERSHELL_FALLBACK)
    if not exe:
        return None
    ids = ",".join(str(int(p)) for p in pids)
    ps = (f"Get-Process -Id {ids} -ErrorAction SilentlyContinue | ForEach-Object "
          "{ '{0} {1} {2}' -f $_.Id, $_.StartTime.ToFileTimeUtc(), $_.ProcessName }")
    try:
        res = subprocess.run([exe, "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return parse_process_table(res.stdout)
