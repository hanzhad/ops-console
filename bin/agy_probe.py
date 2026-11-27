#!/usr/bin/env python3
"""Built-in probe: Google Antigravity (SQLite conversation_summaries.db via DrvFS-safe
immutable URI + annotations/*.pbtxt + presence/*.lock)."""

import json
import pathlib
import re
import shutil
import sqlite3
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List

# Antigravity Paths (WSL /mnt/c)
AGY_BASE = pathlib.Path("/mnt/c/Users/user/.gemini/antigravity")
AGY_DB = AGY_BASE / "conversation_summaries.db"
AGY_ANNOTATIONS = AGY_BASE / "annotations"
AGY_PRESENCE = AGY_BASE / "presence"
AGY_DAEMON = AGY_BASE / "daemon"


def _probe_antigravity() -> List[Dict[str, Any]]:
    if not AGY_DB.exists():
        return []

    sessions: List[Dict[str, Any]] = []
    conn = None

    # Safe read-only SQLite URI with immutable=1 (DrvFS safe, avoids lock / disk I/O errors)
    db_uri = f"file:{AGY_DB}?immutable=1"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT conversation_id, title, workspace_uris, last_modified_time, killed, status "
            "FROM conversation_summaries "
            "WHERE killed = 0 "
            "ORDER BY last_modified_time DESC LIMIT 100"
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        # Live shadow fallback if immutable fails or WAL uncheckpointed
        try:
            snap_path = "/tmp/agy_summary_snap.db"
            shutil.copy2(str(AGY_DB), snap_path)
            wal = AGY_BASE / "conversation_summaries.db-wal"
            if wal.exists():
                shutil.copy2(str(wal), snap_path + "-wal")
            conn = sqlite3.connect(snap_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT conversation_id, title, workspace_uris, last_modified_time, killed, status "
                "FROM conversation_summaries "
                "WHERE killed = 0 "
                "ORDER BY last_modified_time DESC LIMIT 100"
            )
            rows = cursor.fetchall()
        except Exception:
            rows = []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    now = time.time()
    for cid, db_title, uris_raw, lmt_raw, killed, status in rows:
        # Antigravity UI sidebar conversations MUST have an annotation file
        ann_file = AGY_ANNOTATIONS / f"{cid}.pbtxt"
        if not ann_file.exists():
            continue

        title = db_title or ""
        is_pinned = False
        last_view_sec = 0
        try:
            txt = ann_file.read_text(encoding="utf-8", errors="ignore")
            # If archived in Antigravity, do not show in active list
            if "archived:true" in txt or "archived: true" in txt:
                continue
            m_title = re.search(r'title:\s*"([^"]+)"', txt)
            if m_title and not title:
                title = m_title.group(1)
            m_time = re.search(r'last_user_view_time:\s*\{seconds:\s*(\d+)', txt)
            if m_time:
                last_view_sec = int(m_time.group(1))
            is_pinned = "pinned:true" in txt
        except Exception:
            continue

        # Skip internal subagent conversations without user-facing title
        if not title and not is_pinned:
            continue

        # Presence lock
        lock_file = AGY_PRESENCE / f"{cid}.lock"
        is_active = lock_file.exists()

        # Parse CWD from workspace_uris
        cwd = ""
        if uris_raw:
            try:
                uris = json.loads(uris_raw)
                if isinstance(uris, list) and len(uris) > 0:
                    u = uris[0]
                    if u.startswith("file:///"):
                        clean = urllib.parse.unquote(u[8:])
                        if len(clean) >= 2 and clean[1] == ":":
                            cwd = clean[0].upper() + ":\\" + clean[3:].replace("/", "\\")
                        else:
                            cwd = clean
                    else:
                        cwd = u
            except Exception:
                cwd = ""

        # Parse last active time
        last_active = int(now)
        if last_view_sec > 0:
            last_active = last_view_sec
        elif lmt_raw:
            try:
                s_dt = lmt_raw.strip()
                m_iso = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$", s_dt)
                if m_iso:
                    base = m_iso.group(1).replace(" ", "T")
                    sub = (m_iso.group(2) or "")[:7]
                    tz = m_iso.group(3) or "+00:00"
                    dt = datetime.fromisoformat(f"{base}{sub}{tz}")
                    last_active = int(dt.timestamp())
            except Exception:
                pass

        if not title:
            title = f"/meta ({cid[:6]})" if cid.startswith("e2dc") else f"session-{cid[:6]}"

        sessions.append({
            "id": cid,
            "ref": f"local_{cid}",
            "raw_ref": cid,
            "harness": "antigravity",
            "harness_name": "Google Antigravity",
            "title": title,
            "cwd": cwd,
            "last_active": last_active,
            "is_active": True,
            "held_slots": 0,
            "limit": None,
            "is_over_limit": False,
            "can_message": True,
            "metadata": {"status": status, "presence": is_active}
        })

    return sessions


def probe(liveness: bool = False) -> List[Dict[str, Any]]:
    """Antigravity rows. liveness is accepted for a uniform probe signature and unused here."""
    return _probe_antigravity()
