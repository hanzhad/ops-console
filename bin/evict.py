#!/usr/bin/env python3
"""evict.py <slot id> <release|force|rescue|remove>: run slot action via its project's dev-container (or legacy script).

release   `evict --yes`: keeps the script's lease, ssh and process refusals
force     `evict --yes --force`: over a live holder
rescue    `evict --yes` on a folder nobody holds: commits, bundles, pushes rescue/…, resets
remove    `down`, never --force: removes the slot only if the script's own guard agrees
"""
import json, os, pathlib, re, shlex, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import projects

VERBS = {"release": "evict {} --yes", "force": "evict {} --yes --force", "rescue": "evict {} --yes", "remove": "down {}"}
STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", pathlib.Path.home() / ".dev-containers"))
THIN_ENGINE = pathlib.Path.home() / "Projects/host_repo/tools/dev-container-engine/scripts/dev-container-thin"


def _read_lease(sid):
    lease_file = STATE / "leases" / f"{sid}.json"
    if not lease_file.is_file():
        return None
    try:
        lease = json.loads(lease_file.read_text(encoding="utf-8"))
        return lease if isinstance(lease, dict) else None
    except (OSError, ValueError):
        return None


def _resolve_slot(sid):
    lease = _read_lease(sid)
    found = projects.slot_of(projects.discover(), sid, lease=lease)
    if found:
        return found
    parsed = projects.parse_slot_id(sid, lease=lease)
    if parsed and lease is not None:
        repo_root = lease.get("repo_root")
        if repo_root and pathlib.Path(repo_root).is_dir():
            root = pathlib.Path(repo_root)
            script = projects.engine_script(root, thin_engine=THIN_ENGINE, fallback=THIN_ENGINE)
            return {"prefix": parsed[0], "main": root, "slots_dir": root.parent / "dev-containers", "script": script}, parsed[1]
    return None


def _lease_holder(sid):
    lease = _read_lease(sid)
    if lease is None:
        return None
    holder = lease.get("holder")
    return holder if isinstance(holder, str) and holder else None


def main():
    sid, verb = sys.argv[1], sys.argv[2]
    found = _resolve_slot(sid)
    if not found or verb not in VERBS:
        sys.exit(f"not a slot or action: {sys.argv[1:]}")
    p, num = found
    script_path = pathlib.Path(p["script"])

    # A login shell: the service's own PATH has neither the gh guard nor the docker wrappers.
    holder = _lease_holder(sid) if verb == "release" else None
    action = f"release slot-{num} --for {shlex.quote(holder)}" if holder else VERBS[verb].format(f"slot-{num}")
    cmd = f"cd {shlex.quote(str(p['main']))} && {shlex.quote(str(script_path))} {action}"
    c = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True)
    print(c.stdout + c.stderr)
    sys.exit(c.returncode)


if __name__ == "__main__":
    main()
