#!/usr/bin/env python3
"""Dynamic Harness Provider Discovery Registry & Session Management.

Implements Zero-Hardcode System-Reflecting harness discovery:
1. Drop-in manifests: ~/.config/ops-console/harnesses.d/
2. Built-in system probes:
   - Google Antigravity (SQLite conversation_summaries.db via DrvFS-safe immutable URI + annotations/*.pbtxt + presence/*.lock)
   - Claude Code (AppData/.../local_*.json)
3. Active lease introspection (~/.agent-slots/leases/*.json and ~/.dev-containers/leases/*.json)
4. Limits state reader for machine/project caps (limits.json)
5. Polymorphic messaging router
"""

import glob
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import fcntl
from typing import Any, Dict, List, Optional, Tuple


import agy_probe
import agy_send
import claude_probe
import claude_send
import limit_keys

HOME = pathlib.Path.home()
HARNESSES_DIR = HOME / ".config" / "ops-console" / "harnesses.d"
AGENT_SLOT_STATE = pathlib.Path(os.environ.get("AGENT_SLOT_STATE", HOME / ".agent-slots"))
DEV_CONTAINER_STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", HOME / ".dev-containers"))

# Claude Paths
CLAUDE_CANDIDATES = [
    pathlib.Path(os.environ.get("OPS_CONSOLE_WIN_HOME", "/mnt/c/Users/user")) / "AppData/Local/Claude-Data",
    pathlib.Path(os.environ.get("OPS_CONSOLE_WIN_HOME", "/mnt/c/Users/user")) / "AppData/Local/Packages",
]


def sh(argv: List[str], timeout: int = 15) -> str:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return res.stdout.strip()
    except Exception:
        return ""



# ---------------------------------------------------------------------------
# Drop-in Manifest Providers (~/.config/ops-console/harnesses.d/)
# ---------------------------------------------------------------------------

def _probe_dropin_harnesses() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    sessions: List[Dict[str, Any]] = []
    manifests: Dict[str, Dict[str, Any]] = {}

    if not HARNESSES_DIR.exists():
        return sessions, manifests

    for item in HARNESSES_DIR.iterdir():
        if item.name.startswith("."):
            continue
        manifest = None
        if item.suffix == ".json":
            try:
                manifest = json.loads(item.read_text(encoding="utf-8"))
            except Exception:
                continue
        elif os.access(item, os.X_OK):
            manifest = {
                "id": item.stem,
                "name": item.stem.title(),
                "sessions_cmd": str(item)
            }

        if not manifest or "id" not in manifest:
            continue

        hid = manifest["id"]
        manifests[hid] = manifest

        detect_cmd = manifest.get("detect_cmd")
        if detect_cmd:
            if subprocess.run(detect_cmd, shell=True, capture_output=True).returncode != 0:
                continue

        sessions_cmd = manifest.get("sessions_cmd")
        if sessions_cmd:
            try:
                out = subprocess.run(sessions_cmd, shell=True, capture_output=True, text=True, timeout=10).stdout
                if out.strip():
                    loaded = json.loads(out)
                    if isinstance(loaded, list):
                        for s in loaded:
                            s.setdefault("harness", hid)
                            s.setdefault("harness_name", manifest.get("name", hid.title()))
                            s.setdefault("can_message", bool(manifest.get("message_cmd")))
                            sessions.append(s)
            except Exception:
                pass

    return sessions, manifests


# ---------------------------------------------------------------------------
# Active Lease Introspection & Aggregation
# ---------------------------------------------------------------------------

def read_all_leases() -> List[Dict[str, Any]]:
    leases = []
    seen_ids = set()

    for state_dir in (DEV_CONTAINER_STATE, AGENT_SLOT_STATE):
        leases_dir = state_dir / "leases"
        if not leases_dir.exists():
            continue
        for lf in leases_dir.glob("*.json"):
            if not lf.exists():
                continue
            sid = lf.stem
            if sid in seen_ids:
                continue
            try:
                data = json.loads(lf.read_text(encoding="utf-8"))
                data["_file"] = str(lf)
                data["_slot_id"] = sid
                leases.append(data)
                seen_ids.add(sid)
            except Exception:
                pass
    return leases


def read_limits() -> Dict[str, Any]:
    """limits.json of the engine state dir only, which is what the engine enforces.

    The legacy ~/.agent-slots/limits.json is never merged in: a silent fallback would show limits the engine
    does not apply, and bring a legacy value back after a session is set to the default.
    """
    d = limit_keys.read_json(DEV_CONTAINER_STATE / "limits.json")
    if not d:
        return {"default": 1, "machine_max": 20, "sessions": {}}
    d.setdefault("sessions", {})
    return d


def legacy_only_limit_keys() -> List[str]:
    """Migration notice, never applied: session keys the legacy file holds that the engine file lacks."""
    mine = limit_keys.read_json(DEV_CONTAINER_STATE / "limits.json").get("sessions") or {}
    old = limit_keys.read_json(AGENT_SLOT_STATE / "limits.json").get("sessions") or {}
    return sorted(k for k in old if limit_keys.find_key(mine, k) is None)


def _clamp_limit(v: Any, default: int) -> int:
    return min(20, max(0, v)) if isinstance(v, int) and not isinstance(v, bool) else default


def discover_sessions(leases: Optional[List[Dict[str, Any]]] = None,
                      limits: Optional[Dict[str, Any]] = None,
                      active_only: bool = True,
                      liveness: bool = False) -> List[Dict[str, Any]]:
    """Discover sessions across drop-ins, probes, active leases, and per-session limits."""
    if leases is None:
        leases = read_all_leases()
    if limits is None:
        limits = read_limits()
    default_limit = _clamp_limit(limits.get("default") if isinstance(limits, dict) else None, 1)
    sess_limits = limits.get("sessions") if isinstance(limits, dict) and isinstance(limits.get("sessions"), dict) else {}

    all_sessions: List[Dict[str, Any]] = []

    # 1. Drop-ins
    dropin_sessions, _ = _probe_dropin_harnesses()
    all_sessions.extend(dropin_sessions)

    # 2. Antigravity probe
    agy_sessions = agy_probe.probe(liveness)
    all_sessions.extend(agy_sessions)

    # 3. Claude probe
    claude_sessions = claude_probe.probe(liveness)
    all_sessions.extend(claude_sessions)

    # Index existing sessions for lookup
    session_lookup: Dict[str, Dict[str, Any]] = {}

    def register_lookup(key: str, sess: Dict[str, Any]):
        k = key.strip()
        if not k:
            return
        session_lookup[k] = sess
        session_lookup[k.lower()] = sess
        if k.startswith("local_"):
            clean = k[6:]
            session_lookup[clean] = sess
            session_lookup[clean.lower()] = sess
        else:
            session_lookup[f"local_{k}"] = sess
            session_lookup[f"local_{k.lower()}"] = sess
        if len(k) >= 6:
            session_lookup[k[:6]] = sess
            session_lookup[k[-6:]] = sess

    # Archived sessions register first, so a live session wins any short key they share.
    for s in sorted(all_sessions, key=lambda x: not x.get("archived")):
        s.setdefault("held_slots", 0)
        if s.get("id"):
            register_lookup(s["id"], s)
        if s.get("ref"):
            register_lookup(s["ref"], s)
        if s.get("raw_ref"):
            register_lookup(s["raw_ref"], s)

    # 4. Active Lease Introspection
    now = time.time()
    for lease in leases:
        owner = lease.get("owner") or ""
        holder = lease.get("holder") or ""
        harness_declared = lease.get("harness")

        m_ref = re.search(r"\[([^\][]+)\]$", owner)
        ref_cand = m_ref.group(1).strip() if m_ref else owner.strip()
        if not ref_cand or ref_cand == "-":
            ref_cand = holder.strip()

        matched = session_lookup.get(ref_cand) or session_lookup.get(ref_cand.lower())
        if matched:
            matched["held_slots"] = matched.get("held_slots", 0) + 1
            if harness_declared and matched.get("harness") in ("unknown", None):
                matched["harness"] = harness_declared
                matched["harness_name"] = harness_declared.title()
        else:
            h_slug = harness_declared or ("antigravity" if ("30dad0" in ref_cand or "e2dc" in ref_cand) else "unknown")
            h_name = "Google Antigravity" if h_slug == "antigravity" else (h_slug.title() if h_slug != "unknown" else "Agent Lease")
            new_sess = {
                "id": ref_cand,
                "ref": ref_cand,
                "raw_ref": ref_cand,
                "harness": h_slug,
                "harness_name": h_name,
                "title": f"{holder} ({ref_cand[-6:]})" if holder else ref_cand,
                "cwd": lease.get("repo_root", ""),
                "last_active": int(lease.get("since", now)),
                "is_active": True,
                "held_slots": 1,
                "limit": None,
                "is_over_limit": False,
                "can_message": (h_slug in ("antigravity", "claude")),
                "metadata": {"discovered_from": "lease", "lease_id": lease.get("_slot_id")}
            }
            all_sessions.append(new_sess)
            register_lookup(ref_cand, new_sess)

    # Deduplicate sessions by normalized id
    unique_sessions: Dict[str, Dict[str, Any]] = {}
    for s in all_sessions:
        norm_id = s.get("id") or s.get("ref")
        if not norm_id:
            continue
        if norm_id not in unique_sessions:
            unique_sessions[norm_id] = s
        else:
            cur = unique_sessions[norm_id]
            cur["held_slots"] = max(cur["held_slots"], s["held_slots"])
            cur["is_active"] = cur["is_active"] or s["is_active"]
            cur["last_active"] = max(cur["last_active"], s["last_active"])
            if not cur.get("title") or cur["title"].startswith("session-"):
                if s.get("title") and not s["title"].startswith("session-"):
                    cur["title"] = s["title"]

    result = list(unique_sessions.values())
    for s in result:
        key = limit_keys.find_key(sess_limits, s.get("id"), s.get("ref"), s.get("raw_ref"))
        raw_lim = sess_limits[key] if key is not None else None
        eff = _clamp_limit(raw_lim, default_limit)
        s["limit"] = eff
        s["is_over_limit"] = s.get("held_slots", 0) > eff
    link_session_tree(result, session_lookup, default_limit=default_limit)

    # Only show sessions that are active now (presence lock, recent activity, or holding leases)
    # Inactive/archived sessions stay hidden unless active or holding slots.
    # A parent is kept while any session of its tree is shown, so a child never loses its group.
    if active_only:
        shown = {s["id"] for s in result if s.get("is_active") or (s.get("held_slots", 0) > 0)}
        by_id = {s["id"]: s for s in result}
        for sid in list(shown):
            p = by_id[sid].get("parent_id")
            while p and p in by_id and p not in shown:
                shown.add(p)
                p = by_id[p].get("parent_id")
        result = [s for s in result if s["id"] in shown]

    result.sort(key=lambda x: (not x["is_active"], -x["held_slots"], -x["last_active"]))
    return result


# Tree linking and the sending half live in their own modules. send_message stays here so
# patching sessions.<name> (deliver, resolve_target, discover_sessions) still steers it.
from session_tree import link_session_tree  # noqa: E402,F401
from session_send import resolve_target, _claude_live_now, deliver  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Polymorphic Messaging Router
# ---------------------------------------------------------------------------

def send_message(session_ref: str, message: str, harness: Optional[str] = None) -> Tuple[bool, str]:
    """Dispatch notification/message to a session polymorphically based on harness."""
    sessions = discover_sessions(liveness=True)
    if harness:
        sess, err = resolve_target(sessions, harness, session_ref)
        target_harness = harness
    else:
        names = sorted({x.get("harness") for x in sessions if x.get("harness")})
        found = [r for r in (resolve_target(sessions, h, session_ref) for h in names) if r[0]]
        if len(found) > 1:
            return (False, f"ambiguous session {session_ref}: matches {len(found)} harnesses")
        sess, err = found[0] if found else (None, f"no session {session_ref}")
        target_harness = sess.get("harness") if sess else "unknown"

    # 1. Built-in harnesses
    if target_harness in ("claude", "antigravity"):
        if not sess:
            return (False, err)
        ok, detail, _ = deliver(sess, message)
        return (ok, detail)

    # 2. Custom harness drop-in
    _, manifests = _probe_dropin_harnesses()
    if target_harness in manifests and "message_cmd" in manifests[target_harness]:
        cmd_template = manifests[target_harness]["message_cmd"]
        cmd = cmd_template.format(session_id=session_ref, text=shlex.quote(message))
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
            return (p.returncode == 0, p.stdout.strip() or p.stderr.strip() or "Delivered")
        except Exception as e:
            return (False, str(e))

    return (False, f"Harness '{target_harness}' does not support messaging")

if __name__ == "__main__":
    discovered = discover_sessions()
    print(json.dumps(discovered, indent=2))
