#!/usr/bin/env python3
"""The projects with dev-container slots on this machine, read off the machine.

Discovers projects connected to dev-container-engine via ~/.dev-containers/projects/*.json
and project.env files.
"""
import json, os, pathlib, re, subprocess, sys

HOME = pathlib.Path.home()
STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", HOME / ".dev-containers"))
THIN_ENGINE = HOME / "Projects/host_repo/tools/dev-container-engine/scripts/dev-container-thin"
ENGINE_BIN = HOME / "Projects/host_repo/tools/dev-container-engine/bin/dev-container"
_NEW_SLOT_RE = re.compile(r"^0-slot-(\d+)-(.+)$")
_LEGACY_SLOT_RE = re.compile(r"^(.+)-slot-(\d+)$")
_BUNDLE_TS_RE = re.compile(r"^(.+)-(\d{8}-\d{6})(?:-\d+)?$")


def parse_slot_id(sid, lease=None):
    """Return (project, slot_num) from structured lease fields, 0-slot-<N>-<project>, or <project>-slot-<N>."""
    if isinstance(lease, dict):
        label = lease.get("project_label")
        num = lease.get("slot_num")
        if isinstance(label, str) and label.strip() and isinstance(num, int) and not isinstance(num, bool) and num >= 0:
            return label.strip(), num
    if not isinstance(sid, str) or not sid:
        return None
    m = _NEW_SLOT_RE.fullmatch(sid)
    if m:
        return m.group(2), int(m.group(1))
    m = _LEGACY_SLOT_RE.fullmatch(sid)
    if m:
        return m.group(1), int(m.group(2))
    return None


def parse_rescue_bundle_id(bid):
    """Return (project, slot_num, lease_id) for a rescue bundle stem, or None."""
    if not isinstance(bid, str) or not bid:
        return None
    m = _BUNDLE_TS_RE.match(bid)
    lease_id = m.group(1) if m else bid
    parsed = parse_slot_id(lease_id)
    if parsed:
        return parsed[0], parsed[1], lease_id
    return None


def main_of(top):
    p = subprocess.run(["git", "-C", str(top), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True, timeout=5)
    return pathlib.Path(p.stdout.strip()).parent if p.returncode == 0 else None


def _parse_env(path):
    env = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _repo_of(main):
    url = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                         capture_output=True, text=True, timeout=5).stdout.strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return m[1] if m else None


def engine_script(main, thin_engine=None, fallback=None):
    main = pathlib.Path(main)
    thin = THIN_ENGINE if thin_engine is None else pathlib.Path(thin_engine)
    if (main / "scripts/dev-container-thin").exists() or main.resolve() == thin.parent.parent.resolve():
        return thin
    local_bin = main / "bin/dev-container"
    if local_bin.exists():
        return local_bin
    if fallback is not None:
        return fallback
    return thin if thin.exists() else ENGINE_BIN


def read_engine_project(main, meta=None):
    main = pathlib.Path(main)
    env_file = main / "project.env"
    if not env_file.is_file():
        return None
    env = _parse_env(env_file)
    prefix = env.get("PROJECT") or (meta or {}).get("label") or main.name
    if not prefix:
        return None
    script = engine_script(main)
    port_vars = [x for x in re.split(r"[\s,]+", env.get("SLOT_PORTS", "")) if x]
    ports = {v.removesuffix("_PORT").lower(): range(42001, 42100) for v in port_vars}
    return {
        "prefix": prefix,
        "repo": _repo_of(main),
        "main": main,
        "slots_dir": main.parent / "dev-containers",
        "script": script,
        "numbers": 20,
        "model": "engine",
        "ports": ports,
        "port_file": ".ports",
    }


def discover():
    projects = {}
    seen_mains = set()

    # 1. Projects registered in ~/.dev-containers/projects/*.json
    reg_dir = STATE / "projects"
    if reg_dir.is_dir():
        for jf in sorted(reg_dir.glob("*.json")):
            try:
                meta = json.loads(jf.read_text(encoding="utf-8"))
                root = pathlib.Path(meta.get("root", ""))
                if not root.is_dir():
                    continue
                main = main_of(root) or root
                if main in seen_mains:
                    continue
                seen_mains.add(main)
                p = read_engine_project(main, meta=meta)
                if p:
                    projects.setdefault(p["prefix"], p)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue

    # 2. Checkouts with project.env under ~/Projects
    for env_f in [*HOME.glob("Projects/*/project.env"), *HOME.glob("Projects/*/*/project.env")]:
        if "agent-slots" in env_f.parts or "dev-containers" in env_f.parts:
            continue
        try:
            main = main_of(env_f.parent) or env_f.parent
            if main in seen_mains or not (main / "project.env").is_file():
                continue
            seen_mains.add(main)
            p = read_engine_project(main)
            if p:
                projects.setdefault(p["prefix"], p)
        except (OSError, subprocess.SubprocessError):
            continue

    return projects


def slot_of(projects, sid, lease=None):
    """(project, number) of a slot ID (`0-slot-<N>-<project>` or `<project>-slot-<N>`), or None."""
    parsed = parse_slot_id(sid, lease=lease)
    return (projects[parsed[0]], parsed[1]) if parsed and parsed[0] in projects else None


def can_take(p, leases, folders, running, cap, taken_ports):
    """{"count", "why", "free"} for one project."""
    own = set()
    for s in leases:
        parsed = parse_slot_id(s)
        if parsed and parsed[0] == p["prefix"]:
            own.add(parsed[1])
    free = [n for n in range(1, p["numbers"] + 1) if n not in folders and n not in own]
    records = set()
    if p["slots_dir"].is_dir():
        for f in p["slots_dir"].glob(f"*/{p['port_file']}"):
            try:
                records |= {int(n) for n in re.findall(r"\b\d{4,5}\b", f.read_text(errors="replace"))}
            except OSError:
                continue
    ports = {name: sum(1 for port in r if port not in records and port not in taken_ports) for name, r in p["ports"].items()}
    tightest = min(ports, key=ports.get, default=None)
    plain = f"{len(free)} numbers have no folder"
    terms = [(cap - len(leases), f"{len(leases)} leases of cap {cap}, every project counts"), (len(free), plain)]
    if tightest:
        terms.append((ports[tightest], f"{ports[tightest]} {tightest} ports left"))
    count, why = min(terms, key=lambda t: t[0])
    return {"count": max(count, 0), "why": why, "free": free}


if __name__ == "__main__":
    print(json.dumps({k: {**v, "main": str(v["main"]), "slots_dir": str(v["slots_dir"]), "script": str(v["script"]),
                          "ports": {n: [r[0], r[-1]] if r else [] for n, r in v["ports"].items()}}
                      for k, v in discover().items()}, indent=1))
