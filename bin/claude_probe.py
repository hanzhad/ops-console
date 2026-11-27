#!/usr/bin/env python3
"""Built-in probe: Claude Code (AppData/.../claude-code-sessions/*/*/local_*.json)."""

import json
import pathlib
import time
from typing import Any, Dict, List, Optional

import claude_registry
import winproc

REGISTRY_DIR = "/mnt/c/Users/user/.claude/sessions"


def _probe_claude() -> List[Dict[str, Any]]:
    sessions: List[Dict[str, Any]] = []
    session_files: List[pathlib.Path] = []

    # 1. AppData/Local/Claude-Data/claude_*/claude-code-sessions/*/*/local_*.json
    c_data = pathlib.Path("/mnt/c/Users/user/AppData/Local/Claude-Data")
    if c_data.exists():
        for cdir in c_data.glob("claude_*/claude-code-sessions"):
            try:
                for sf in cdir.glob("*/*/local_*.json"):
                    session_files.append(sf)
            except Exception:
                pass

    # 2. Packages/Claude_*/LocalCache/Roaming/Claude/claude-code-sessions/*/*/local_*.json
    pkg_data = pathlib.Path("/mnt/c/Users/user/AppData/Local/Packages")
    if pkg_data.exists():
        for pdir in pkg_data.glob("Claude_*/LocalCache/Roaming/Claude/claude-code-sessions"):
            try:
                for sf in pdir.glob("*/*/local_*.json"):
                    session_files.append(sf)
            except Exception:
                pass

    now = time.time()
    seen_ids = set()

    for sf in session_files:
        try:
            data = json.loads(sf.read_text(encoding="utf-8", errors="ignore"))
            # Archived sessions are kept, marked, and never active: they name the parent of a
            # child or lease that outlived it, and drop out of the list otherwise (active_only).
            archived = bool(data.get("isArchived"))
            sid = data.get("sessionId")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)

            title = data.get("title") or sid[-6:]
            cwd = data.get("cwd") or ""
            last_ms = data.get("lastActivityAt") or 0
            last_active = int(last_ms / 1000) if last_ms > 0 else int(sf.stat().st_mtime)
            # Active if unarchived and had activity within 24h
            is_active = (now - last_active < 86400) and not archived
            spawned = data.get("spawnedFrom") or {}
            parent_id = spawned.get("sessionId") if isinstance(spawned, dict) else None

            ref = sf.stem  # e.g. local_016199fc-...

            sessions.append({
                "id": sid,
                "ref": ref,
                "raw_ref": sid,
                "harness": "claude",
                "harness_name": "Claude Code",
                "title": title,
                "cwd": cwd,
                "last_active": last_active,
                "is_active": is_active,
                "archived": archived,
                "parent_id": parent_id,
                "cliSessionId": data.get("cliSessionId"),
                "held_slots": 0,
                "limit": None,
                "is_over_limit": False,
                "can_message": not archived,
                "metadata": {"file": str(sf)}
            })
        except Exception:
            continue

    return sessions


def read_registry(dir: Any = REGISTRY_DIR) -> Optional[List[Dict[str, Any]]]:
    """Parsed live-session records (digit-named *.json only; .key files are never opened).
    None when the directory is unreadable."""
    try:
        files = [f for f in pathlib.Path(dir).iterdir()
                 if f.suffix == ".json" and f.stem.isdigit()]
    except OSError:
        return None
    records: List[Dict[str, Any]] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def probe(liveness: bool = False) -> List[Dict[str, Any]]:
    """Claude rows, each carrying cliSessionId. With liveness, every row also gets "live"
    (True/False/None) from the live-session registry and the Windows process table."""
    rows = _probe_claude()
    if not liveness:
        return rows
    records = read_registry()
    proc = None
    if records is not None:
        pids = sorted({int(r["pid"]) for r in records if str(r.get("pid")).isdigit()})
        proc = winproc.query_windows_processes(pids)
    live = claude_registry.claude_liveness(records, rows, proc)
    for row in rows:
        row["live"] = live.get(row["id"])
    return rows
