"""Pure core of ops-console: ready data in, processed data out. No I/O, no clock, no threads."""

import json
import re
import time

# --- host check -------------------------------------------------------------------------------


def is_valid_host(host):
    if not host:
        return False
    h = host.split(":")[0].strip().lower()
    if h in ("127.0.0.1", "localhost"):
        return True
    if h.endswith(".ts.net"):
        return True
    if h.startswith("100."):
        parts = h.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            if 64 <= int(parts[1]) <= 127:
                return True
    if h.startswith("192.168.") or h.startswith("10."):
        return True
    if h.startswith("172."):
        parts = h.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            if 16 <= int(parts[1]) <= 31:
                return True
    return False


# --- blocks -----------------------------------------------------------------------------------


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decide_put(prev, json_s, pass_seq) -> str:
    """prev is None or {"json", "pass_seq"}. A pass older than the stored one never writes."""
    if prev is None:
        return "changed"
    if pass_seq < prev["pass_seq"]:
        return "older"
    return "same" if prev["json"] == json_s else "changed"


def pass_counts(pass_started_seq, finished_seq) -> bool:
    """A watcher pass confirms a command only if it started strictly after the command finished."""
    return finished_seq is not None and pass_started_seq > finished_seq


# --- command lifecycle ------------------------------------------------------------------------


def _done(cmd, state, message, now, **kw):
    return {**cmd, "state": state, "message": message, "done_at": now, **kw}


def cmd_on_finish(cmd, rc, msg, seq, now):
    if cmd["state"] != "running":
        return cmd
    tail = (msg or "")[-300:]
    if rc != 0:
        return _done(cmd, "failed", tail or f"exit {rc}", now, finished_seq=seq)
    if not cmd["zones"]:
        return _done(cmd, "ok", tail, now, finished_seq=seq, waiting=[])
    return {**cmd, "message": tail, "finished_seq": seq, "waiting": list(cmd["zones"])}


def cmd_on_pass(cmd, zone, pass_started_seq, now):
    if cmd["state"] != "running" or zone not in cmd["waiting"] or not pass_counts(pass_started_seq, cmd["finished_seq"]):
        return cmd
    waiting = [z for z in cmd["waiting"] if z != zone]
    if waiting:
        return {**cmd, "waiting": waiting}
    return _done(cmd, "ok", cmd["message"], now, waiting=[])


def cmd_on_deadline(cmd, now, block_errors):
    if cmd["state"] != "running" or now <= cmd["deadline_at"]:
        return cmd
    msg = "not confirmed"
    errs = [f"{z}: {block_errors[z]}" for z in cmd["zones"] if block_errors.get(z)]
    if errs:
        msg += " (" + "; ".join(errs) + ")"
    return _done(cmd, "failed", msg, now)


def cmd_on_boot(cmd, now=None):
    if cmd["state"] != "running":
        return cmd
    return {**cmd, "state": "failed", "message": "server restarted", "done_at": time.time() if now is None else now}


def prune_plan(rows, keep):
    """Ids of the oldest terminal commands beyond `keep`. A running command is never pruned."""
    done = sorted((r for r in rows if r["state"] != "running"), key=lambda r: r["rev"])
    return [r["id"] for r in done[:max(0, len(done) - keep)]]


def cmd_id_ok(s) -> bool:
    return isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", s) is not None


# --- SSE --------------------------------------------------------------------------------------


def sse_frame(event, data, rev) -> bytes:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\nid: {rev}\ndata: {body}\n\n".encode("utf-8")


def sse_ping_frame() -> bytes:
    """The keep-alive: a named event, because EventSource never delivers a comment line to the page."""
    return b"event: ping\ndata: {}\n\n"


def frames_after(ring, last_rev):
    """ring is a list of (rev, frame) in rev order. gap is True when frames after last_rev were dropped."""
    frames = [f for r, f in ring if r > last_rev]
    gap = bool(ring) and last_rev < ring[0][0] - 1
    return frames, gap


# --- CI output (bin/ci.py shells out to ci-runner.sh; these parse what it prints) ---------------


def parse_ci_list(out, skip):
    lines = (out or "").splitlines()
    head = next((l for l in lines if l.startswith("gate")), "")
    repos = []
    for l in lines:
        m = re.match(r"(\S+/\S+)\s+(?:(?:no)?docker\s+)?CI_RUNNER=(\S*)\s+runners=(\d+)", l)
        if m and m[1] not in skip:
            repos.append({"repo": m[1], "local": m[2] == "1", "runners": int(m[3])})
    return {"gate": head.removeprefix("gate").strip() or "unknown", "running": "running" in head,
            "locked": "locked" in head and "unlocked" not in head, "repos": repos}


def parse_ci_budget(out):
    live = re.search(r"^live\s+(\d+) (\d+)", out or "", re.M)
    boot = re.search(r"budget (\d+) cores, big cap (\d+)", out or "")
    return {"budget": int(live[1]) if live else int(boot[1]) if boot else None,
            "cap": int(live[2]) if live else int(boot[2]) if boot else None,
            "live": bool(live), "boot": [int(boot[1]), int(boot[2])] if boot else None}


def parse_queue_tsv(repo, out):
    items = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0].isdigit():
            items.append({"repo": repo, "id": int(parts[0]), "created": parts[1], "name": parts[2], "branch": parts[3]})
    return items


def sort_queue(items):
    """Oldest first: the order the gate's FIFO serves them in."""
    return sorted(items, key=lambda q: q.get("created") or "")


def parse_ci_status_json(out, skip):
    """Parse `ci-runner.sh status --json` (schema 1) into the ci_gate block payload."""
    d = json.loads(out)
    if not isinstance(d, dict) or d.get("schema") != 1:
        return None
    gate_obj = d.get("gate") or {}
    st_obj = d.get("status") or {}
    repos = [
        {"repo": r["repo"], "local": r.get("ci_runner") == 1, "mega": r.get("mega"), "runners": 0}
        for r in (d.get("repositories") or [])
        if isinstance(r, dict) and r.get("repo") and r.get("repo") not in skip
    ]
    state = gate_obj.get("state") or "unknown"
    queue = sort_queue([
        {
            "repo": q.get("repo"),
            "id": q.get("run_id") if "run_id" in q else q.get("id"),
            "created": q.get("since") if "since" in q else q.get("created"),
            "name": q.get("name"),
            "branch": q.get("branch"),
            "class": q.get("class"),
        }
        for q in (st_obj.get("queue") or [])
        if isinstance(q, dict) and q.get("repo") not in skip
    ])
    nlq = st_obj.get("non_local_queued")
    non_local_queued = nlq if isinstance(nlq, int) and not isinstance(nlq, bool) else 0
    rl = st_obj.get("rate_limit")
    rate_limit = {
        "remaining": rl.get("remaining"),
        "limit": rl.get("limit"),
        "reset_at": rl.get("reset_at"),
        "throttled": bool(rl.get("throttled", False)),
    } if isinstance(rl, dict) and rl else None
    return {
        "gate": state,
        "running": state == "running",
        "locked": bool(gate_obj.get("locked")),
        "paused": bool(gate_obj.get("paused") if gate_obj.get("paused") is not None else st_obj.get("paused")),
        "commit": gate_obj.get("commit") or st_obj.get("commit"),
        "token_expires": gate_obj.get("token_expires"),
        "owner_tokens": gate_obj.get("owner_tokens") or [],
        "repos": repos,
        "queue": queue,
        "non_local_queued": non_local_queued,
        "rate_limit": rate_limit,
        "budget": st_obj.get("budget"),
        "cap": st_obj.get("big_cap"),
        "used": st_obj.get("used", 0),
        "big_used": st_obj.get("big_used", 0),
        "busy": st_obj.get("busy", 0),
        "class_costs": st_obj.get("class_costs") or {},
        "draining": st_obj.get("draining"),
        "waiting": st_obj.get("waiting"),
        "live": bool(st_obj),
        "boot": None,
    }


def validate_ci(args) -> bool:
    return bool(args in (["pause"], ["resume"], ["reset"])
                or (len(args) == 2 and args[0] in ("on", "off", "mega-on", "mega-off")
                    and re.fullmatch(r"[\w.-]+/[\w.-]+", args[1]))
                or (len(args) == 3 and args[0] == "budget" and args[1].isdigit() and args[2].isdigit()
                    and int(args[2]) >= 4 and int(args[1]) >= int(args[2])))


# --- host, slots and sessions blocks ----------------------------------------------------------


def mem_mb(s):
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", s.strip())
    if not m:
        return 0.0
    v, u = float(m[1]), m[2][0]
    return v / 1024 if u == "K" else v * 1024 if u == "G" else v


def parse_docker_stats(out):
    """`docker stats --format {{.Name}}|{{.CPUPerc}}|{{.MemUsage}}` -> {name: [cpu, mem_mb]}; malformed lines are dropped."""
    stats = {}
    for line in (out or "").splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        try:
            stats[parts[0]] = [float(parts[1].strip().rstrip("%") or 0), mem_mb(parts[2].split("/")[0])]
        except ValueError:
            continue
    return stats


def parse_host(meminfo, loadavg, cpus, stats):
    """The host block: no clock in it, so it changes only when a number does."""
    mi = dict(re.findall(r"(\w+):\s+(\d+)", meminfo))
    load = loadavg.split()
    return {"cpus": cpus, "load1": float(load[0]), "load5": float(load[1]),
            "mem_total_mb": int(mi["MemTotal"]) // 1024,
            "mem_used_mb": (int(mi["MemTotal"]) - int(mi["MemAvailable"])) // 1024,
            "docker_cpu": round(sum(c for c, _ in stats.values()), 1), "stats": stats}


def _clamp_limit(v, default):
    return min(20, max(0, v)) if isinstance(v, int) and not isinstance(v, bool) else default


def slots_block(slots, limits):
    lim = limits or {}
    return {"slots": slots, "machine_max": _clamp_limit(lim.get("machine_max"), 20)}


def sessions_block(sessions, limits=None):
    lim = limits or {}
    return {"sessions": sessions, "default_limit": _clamp_limit(lim.get("default"), 1)}


# --- the /api/state payload -------------------------------------------------------------------

# Which top-level keys of the payload each block feeds.
BLOCK_KEYS = {"ci_gate": ["gate"], "gh_queue": ["gate"], "autoreview": ["autoreview"], "host": ["host"],
              "slots": ["slots", "machine_in_use"], "sessions": ["sessions", "default_limit"],
              "runners": ["ci"], "cleaners": ["cleaners", "rescue_bundles"]}
LEGACY_DEFAULTS = {"slots": [], "cleaners": {}, "rescue_bundles": [], "sessions": [], "machine_max": 20, "machine_in_use": 0}


def legacy_without(block_names):
    return sorted({k for n in block_names for k in BLOCK_KEYS.get(n, [])})


def _gate_with_queue(blocks):
    gate = blocks["ci_gate"]
    q = None if gate.get("queue_unknown") else (blocks.get("gh_queue") or [])
    return {**gate, "queue": q}


def assemble_legacy(blocks, extras):
    """Rebuilds today's /api/state payload from block data ({name: data}); extras are merged at the top level."""
    out = {**LEGACY_DEFAULTS, **(blocks.get("legacy") or {})}
    for name, data in blocks.items():
        if name not in ("legacy", "ci_gate", "gh_queue") and data is not None:
            out[name] = data
    if blocks.get("ci_gate") is not None:
        out["gate"] = _gate_with_queue(blocks)
    return {**out, **extras}


def command_results(commands):
    """What the old in-memory `evicting` and `results` held, derived from the commands table."""
    evicting, results = {}, {}
    for c in sorted(commands, key=lambda c: c["rev"]):
        body = json.loads(c["body"]) if isinstance(c["body"], str) else c["body"]
        target = str(body.get("target", "")).strip()
        if not target or c["route"] not in ("/api/evict", "/api/rescue"):
            continue
        if c["state"] == "running":
            if c["route"] == "/api/evict":
                evicting[target] = str(body.get("mode", "")).strip()
            continue
        ok = c["state"] == "ok"
        if c["route"] == "/api/evict":
            results[target] = {"mode": str(body.get("mode", "")).strip(), "ok": ok, "text": c["message"] or "", "at": int(c["done_at"] or 0)}
        else:
            results[target] = {"ok": ok, "message": c["message"] or "", "action": body.get("action"), "ts": c["done_at"]}
    return evicting, results


def api_state(blocks, commands, events, now, error):
    """GET /api/state: the page's keys, flattened from the blocks ({name: data})."""
    def part(name, default):
        return default if blocks.get(name) is None else blocks[name]

    sl, se, cl = part("slots", {}), part("sessions", {}), part("cleaners", {})
    slots = sl.get("slots", [])
    snap = {"ts": int(now), "now": int(now), "slots": slots, "machine_max": sl.get("machine_max", 20),
            "machine_in_use": len(slots), "sessions": se.get("sessions", []),
            "default_limit": se.get("default_limit", 1),
            "ci": part("runners", []), "cleaners": cl.get("cleaners", {}), "rescue_bundles": cl.get("rescue_bundles", [])}
    for name in ("host", "autoreview"):
        if blocks.get(name) is not None:
            snap[name] = blocks[name]
    if blocks.get("ci_gate") is not None:
        snap["gate"] = _gate_with_queue(blocks)
    evicting, results = command_results(commands)
    return {**snap, "snap": snap, "error": error, "events": list(events)[-20:], "evicting": evicting,
            "results": results}
