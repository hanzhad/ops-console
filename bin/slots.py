#!/usr/bin/env python3
"""Slot discovery: one entry per live lease (and, without the dev-container engine, per unleased slot folder).

The docker sample and the session list come from the caller (the host and sessions blocks), so a pass runs
no `docker stats` and no session discovery of its own. An entry holds absolute times only (`since`,
`ttl_until`): nothing in it changes with the clock, so the block's rev moves only when a slot does.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import engine as engine_mod
import projects as projects_mod
import sessions as sessions_mod

CACHE = pathlib.Path.home() / ".cache" / "ops-console"
DEV_CONTAINER_STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", pathlib.Path.home() / ".dev-containers"))
CLAIM = re.compile(r"Claimed by `(.+?)`")


def sh(argv, timeout=15):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def claims_for(repo, checkout, issue):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "claims.json"
    try:
        cache = json.loads(f.read_text())
    except Exception:
        cache = {}
    key = f"{repo}#{issue}"
    hit = cache.get(key)
    if hit and time.time() - hit["at"] < 600:
        return hit
    out = sh([
        "bash", "-lc",
        f"cd ~ && gh issue view {issue} -R {repo} --json title,comments "
        "--jq '{title, claims: [.comments[].body | select(startswith(\"Claimed by\"))]}' 2>/dev/null"
    ], 20)
    try:
        d = json.loads(out)
        m = CLAIM.search(d["claims"][-1]) if d["claims"] else None
        hit = {"at": time.time(), "title": d["title"], "session": m[1] if m else None}
    except Exception:
        hit = {"at": time.time(), "title": None, "session": None}
    cache[key] = hit
    try:
        f.write_text(json.dumps(cache))
    except Exception:
        pass
    return hit


def session_lookup(sessions):
    """Every key a lease may name a session by -> the session."""
    lookup = {}
    for s in sessions:
        for k in (s.get("id"), s.get("ref"), s.get("raw_ref"), s.get("title")):
            if k:
                lookup[k] = s
                lookup[k.lower()] = s
                if k.startswith("local_"):
                    lookup[k[6:]] = s
                    lookup[k[6:].lower()] = s
    return lookup


def lease_owner(lease):
    return lease.get("owner") if lease.get("owner") not in (None, "", "-") else ""


def lease_ref(lease):
    """The session a lease names: the `[ref]` at the end of its owner, else the owner, else the holder."""
    owner, holder = lease_owner(lease), lease.get("holder", "-")
    m_ref = re.search(r"\[([^\][]+)\]$", owner)
    return m_ref.group(1).strip() if m_ref else (owner.strip() or holder.strip())


def with_since(lease, now):
    """A lease with no `since` gets the mtime of its file (stable between passes), else the pass time."""
    if lease.get("since") is not None:
        return lease
    try:
        since = os.stat(lease["_file"]).st_mtime
    except (KeyError, OSError):
        since = now
    return {**lease, "since": since}


def _usage(sid, stats):
    app = f"{sid}-app-1"
    prefix = f"{sid}-"
    containers = [k for k in stats if k.startswith(prefix) and "-run-" not in k]
    if not containers:
        containers = [app, f"{sid}-db-1", f"{sid}-gateway-1"]
    cpu = sum(stats.get(c, (0, 0))[0] for c in containers)
    mem = sum(stats.get(c, (0, 0))[1] for c in containers)
    return app in stats, round(cpu, 1), round(mem)


def _read_slot_ports(sid):
    f = DEV_CONTAINER_STATE / "ports" / f"{sid}.ports"
    if not f.is_file():
        return None
    ports = {}
    try:
        for line in f.read_text(errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.strip().partition("=")
                if v.isdigit():
                    ports[k.removesuffix("_PORT").lower()] = int(v)
    except OSError:
        return None
    return ports


def _read_last_activity(sid, since):
    times = [since] if since is not None else []
    for ext in ("run", "cpu"):
        f = DEV_CONTAINER_STATE / "activity" / f"{sid}.{ext}"
        if f.is_file():
            try:
                times.append(int(f.read_text(errors="replace").strip()))
            except (OSError, ValueError):
                pass
    return max(times) if times else None


def _slot_ports(sid, ssh_port=None):
    disk_ports = _read_slot_ports(sid)
    ports = dict(disk_ports) if disk_ports is not None else {}
    if ssh_port and "ssh" not in ports:
        ports["ssh"] = ssh_port
    return ports


def _normalize_slot_repos(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        alias = item.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            continue
        entry = {
            "alias": alias,
            "path": str(item.get("path") or ""),
            "mode": str(item.get("mode") or "ro"),
        }
        if "attach" in item and item["attach"] is not None:
            entry["attach"] = str(item["attach"])
        out.append(entry)
    return out


def _read_slot_repos(sid, lease):
    f = DEV_CONTAINER_STATE / "repos" / f"{sid}.json"
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return _normalize_slot_repos(data)
        except (OSError, ValueError, TypeError):
            pass
    return _normalize_slot_repos(lease.get("repos") if isinstance(lease, dict) else [])


def _slot_has_toolbox(sid, stats):
    return (DEV_CONTAINER_STATE / "toolbox" / sid).is_file() or f"{sid}-toolbox-1" in (stats or {})


def build_slot(lease, sess, stats, branch, head, now=None, claim_title=None):
    """One slot entry from a lease, its session (or None) and the docker sample. `now` is accepted and unused."""
    sid = lease.get("project", lease.get("_slot_id"))
    parsed = projects_mod.parse_slot_id(sid, lease=lease)
    prefix, slot_num = parsed if parsed else (sid, 0)
    holder = lease.get("holder", "-")
    owner, ref = lease_owner(lease), lease_ref(lease)
    title = (sess.get("title") if sess else None) or claim_title
    running, cpu, mem = _usage(sid, stats)
    ssh_port = lease.get("ssh_port")
    ports = _slot_ports(sid, ssh_port)
    since = lease.get("since")
    last_activity = _read_last_activity(sid, since)
    return {
        "id": sid,
        "project": prefix or sid,
        "slot": slot_num,
        "holder": holder,
        "session_ref": ref,
        "harness": lease.get("harness") or (sess.get("harness") if sess else "unknown"),
        "session_title": title or holder,
        # Leases are grouped on the page by the root of their session's tree.
        "group_ref": (sess.get("root_id") if sess else None) or ref,
        "group_title": (sess.get("root_title") if sess else None) or title or holder,
        "ssh_port": ssh_port,
        "ports": ports,
        "branch": branch,
        "head": head,
        "running": running,
        "cpu": cpu,
        "mem_mb": mem,
        "owner": owner,
        "since": since,
        "last_activity": last_activity,
        "ttl_until": since + lease.get("ttl_seconds", 0) if since is not None else None,
        "repos": _read_slot_repos(sid, lease),
        "ephemeral": bool(lease.get("ephemeral", False)),
        "toolbox": _slot_has_toolbox(sid, stats),
    }


def _git(slot_dir, *args):
    return sh(["git", "-C", str(slot_dir), *args], 5).strip() or None


def abandoned_slot(prefix, slot_num, stats, branch, head):
    sid = f"{prefix}-slot-{slot_num}"
    running, cpu, mem = _usage(sid, stats)
    return {
        "id": sid, "project": prefix, "slot": slot_num, "holder": "abandoned", "session_ref": "",
        "harness": "unknown", "session_title": "abandoned slot (unleased)", "ssh_port": None,
        "ports": _slot_ports(sid), "branch": branch, "head": head,
        "running": running, "cpu": cpu, "mem_mb": mem, "owner": "", "since": None, "ttl_until": None,
        "repos": [], "ephemeral": False, "toolbox": False,
        "abandoned": True,
    }


def _leased(lease, sess, stats, projects):
    sid = lease.get("project", lease.get("_slot_id"))
    parsed = projects_mod.parse_slot_id(sid, lease=lease)
    prefix, num = parsed if parsed else (sid, 0)
    branch = head = claim_title = None
    p = projects.get(prefix) if parsed else None
    if p:
        d = p["slots_dir"] / f"slot-{num}"
        if d.is_dir():
            branch, head = _git(d, "branch", "--show-current"), _git(d, "rev-parse", "--short", "HEAD")
    if branch is None and f"{sid}-app-1" in stats:
        out = sh(["docker", "exec", f"{sid}-app-1", "sh", "-c",
                  "git -c safe.directory=/workspace -C /workspace branch --show-current; echo ---; git -c safe.directory=/workspace -C /workspace rev-parse --short HEAD"], 3)
        if "---" in out:
            b, _, h = out.partition("---")
            branch, head = b.strip() or None, h.strip() or None
    if p:
        m_issue = re.fullmatch(r"issue-(\d+)", lease.get("holder", "-"))
        if not (sess and sess.get("title")) and m_issue and p.get("repo") and p.get("main"):
            claim_title = claims_for(p["repo"], p["main"], m_issue.group(1)).get("title")
    return build_slot(lease, sess, stats, branch, head, claim_title=claim_title)


def _unleased(projects, seen, stats):
    for prefix, p in projects.items():
        slots_dir = p.get("slots_dir")
        if not slots_dir or not slots_dir.is_dir():
            continue
        for sd in sorted(slots_dir.glob("slot-*")):
            num = sd.name.replace("slot-", "")
            if sd.is_dir() and num.isdigit() and (prefix, int(num)) not in seen:
                yield abandoned_slot(prefix, int(num), stats, _git(sd, "branch", "--show-current"), _git(sd, "rev-parse", "--short", "HEAD"))


def discover_slots(stats, sessions, leases=None):
    """Live leased slots; without the engine, also the unleased slot folders on disk. Sorted by slot number and project."""
    leases = sessions_mod.read_all_leases() if leases is None else leases
    lookup = session_lookup(sessions)
    projects = projects_mod.discover()
    slots, seen = [], set()
    now = time.time()
    for lease in (with_since(l, now) for l in leases):
        entry_sid = lease.get("project", lease.get("_slot_id"))
        parsed = projects_mod.parse_slot_id(entry_sid, lease=lease)
        seen.add(parsed if parsed else (entry_sid, 0))
        ref = lease_ref(lease)
        slots.append(_leased(lease, lookup.get(ref) or lookup.get(ref.lower()), stats, projects))
    if not engine_mod.is_engine_available():
        slots.extend(_unleased(projects, seen, stats))
    slots.sort(key=lambda s: (s["slot"], s["project"]))
    return slots


get_slots = discover_slots
