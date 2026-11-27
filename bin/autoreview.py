import os
#!/usr/bin/env python3
"""Auto-review container service management for ops-console.

Provides status inspection and lifecycle controls (start, stop, restart,
set-global harness/model, pause/resume repo, set repo all/self_review, trigger tick, sync tokens).
"""
import json, os, pathlib, re, shlex, subprocess, sys

HOME = pathlib.Path.home()
HOST_REPO = pathlib.Path(os.environ.get("OPS_CONSOLE_AUTOREVIEW_REPO", HOME / "Projects" / "pr-auto-review"))
COMPOSE = HOST_REPO / "tools" / "auto-review" / "docker-compose.yml"
HOST_TOKEN_DIR = pathlib.Path(f"/run/user/{os.getuid()}/auto-review")


# The auto-review container starts as root without
# DAC_OVERRIDE and the gate (uid 10001) owns /run/auto-review and
# /var/lib/auto-review 0700: a root `docker exec` can read neither.
GATE_UID = os.environ.get("AUTO_REVIEW_GATE_UID", "10001")

def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def get_container_state():
    rc, out, _ = sh(["docker", "inspect", "auto-review", "--format", "{{json .State}}"], timeout=5)
    if rc != 0 or not out:
        return {"status": "stopped", "running": False, "paused": False, "uptime": None}
    try:
        st = json.loads(out)
        status = st.get("Status", "unknown")
        running = st.get("Running", False)
        paused = st.get("Paused", False)
        started_at = st.get("StartedAt")
        return {"status": status, "running": running and not paused, "paused": paused, "started_at": started_at}
    except Exception:
        return {"status": "unknown", "running": False, "paused": False, "uptime": None}


EMPTY_REGISTRY = {"version": 1, "harness": "claude-code", "model": "sonnet", "projects": {}}


def cli(*args, timeout=30):
    """Runs the auto-review CLI inside the container as the gate uid.

    The registry lives in the container's state volume and its CLI is the only
    writer (upstream issue); this file never touches projects.json.
    """
    return sh(["docker", "exec", "-u", GATE_UID, "auto-review", "auto-review", *args], timeout=timeout)


class RegistryError(Exception):
    """The registry could not be read: the CLI failed or printed something unparseable.

    Distinct from a registry that is genuinely empty, which the CLI prints as JSON.
    """


def get_registry_raw():
    rc, out, err = cli("registry", "show", timeout=10)
    if rc != 0:
        raise RegistryError(err or out or "auto-review CLI failed (is the container running?)")
    try:
        return json.loads(out)
    except Exception:
        raise RegistryError("auto-review registry show printed unparseable output: " + (out[:200] or err or "(empty)"))


def cli_result(args, ok_msg):
    rc, out, err = cli(*args)
    if rc != 0:
        return rc, err or out or "auto-review CLI failed (is the container running?)"
    return 0, ok_msg


def parse_models(out):
    """`auto-review models` JSON -> (harnesses, {harness: models}, {harness: default})."""
    entries = json.loads(out)["harnesses"]
    return (
        [e["harness"] for e in entries],
        {e["harness"]: list(e.get("models", [])) for e in entries},
        {e["harness"]: e.get("default") for e in entries},
    )


def get_models():
    """The CLI is the only source of harness and model names; on failure the lists are empty.

    Returns (harnesses, models, defaults, error).
    """
    rc, out, err = cli("models", timeout=10)
    if rc != 0:
        return [], {}, {}, err or out or "auto-review models failed (is the container running?)"
    try:
        return (*parse_models(out), None)
    except Exception:
        return [], {}, {}, "auto-review models printed unparseable output: " + (out[:200] or err or "(empty)")


def get_health_status():
    """Query `auto-review status --json` for token problems and harness faults."""
    rc, out, err = cli("status", "--json", timeout=10)
    if rc != 0:
        return {"tokens": [], "harnesses": {}, "error": err or out or "auto-review status --json failed"}
    idx = out.find("{")
    if idx == -1:
        return {"tokens": [], "harnesses": {}, "error": "auto-review status --json printed unparseable output: " + (out[:200] or err or "(empty)")}
    try:
        d = json.loads(out[idx:])
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        return {
            "tokens": d.get("tokens") if isinstance(d.get("tokens"), list) else [],
            "harnesses": d.get("harnesses") if isinstance(d.get("harnesses"), dict) else {},
            "error": None,
        }
    except Exception:
        return {"tokens": [], "harnesses": {}, "error": "auto-review status --json printed unparseable output: " + (out[:200] or err or "(empty)")}


def get_global(raw=None):
    raw = raw if raw is not None else get_registry_raw()
    harness = raw.get("harness", "claude-code")
    model = raw.get("model", "sonnet" if harness == "claude-code" else "gemini-2.5-flash")
    return {"harness": harness, "model": model}


def set_global(key, val):
    if key == "harness":
        # One write: the CLI sets the harness together with its default model.
        return cli_result(["set-global", "harness", val], f"global harness={val}")
    elif key == "model":
        model = str(val).strip()
        return cli_result(["set-global", "model", model], f"global model={model}")
    return 1, f"unknown global key: {key}"


def is_paused(entry):
    # `paused` is the key in current registries; a registry from before the migration carries `enabled: false`.
    if "paused" in entry:
        return bool(entry["paused"])
    return entry.get("enabled", True) is False


def get_projects(raw=None):
    raw = raw if raw is not None else get_registry_raw()
    return raw.get("projects", {})


def get_container_tokens():
    rc, out, _ = sh(["docker", "exec", "-u", GATE_UID, "auto-review", "ls", "-1", "/run/auto-review"], timeout=5)
    if rc != 0 or not out:
        return []
    return [l.removesuffix(".token") for l in out.splitlines() if l.endswith(".token")]


def get_host_tokens():
    if not HOST_TOKEN_DIR.is_dir():
        return []
    return [p.stem for p in HOST_TOKEN_DIR.glob("*.token")]


def sync_tokens():
    host_tokens = list(HOST_TOKEN_DIR.glob("*.token")) if HOST_TOKEN_DIR.is_dir() else []
    if not host_tokens:
        return 0, "no tokens found on host"
    injected = 0
    for tok in host_tokens:
        name = tok.stem
        if name == "test-repo":
            continue
        try:
            content = tok.read_bytes()
            p = subprocess.run(
                ["docker", "exec", "-i", "-u", GATE_UID, "auto-review", "sh", "-c",
                 f"cat > /run/auto-review/{name}.token && chmod 0600 /run/auto-review/{name}.token"],
                input=content, capture_output=True, timeout=10
            )
            if p.returncode == 0:
                injected += 1
        except Exception:
            pass
    return 0, f"synced {injected} tokens into auto-review container"


def status():
    c_state = get_container_state()
    registry_error = None
    raw = json.loads(json.dumps(EMPTY_REGISTRY))
    if c_state["running"]:
        try:
            raw = get_registry_raw()
        except RegistryError as e:
            registry_error = str(e)
    glob = get_global(raw)
    if c_state["running"]:
        harnesses, models, default_models, models_error = get_models()
    else:
        harnesses, models, default_models, models_error = [], {}, {}, "auto-review container is not running"
    projs = {r: {**e, "paused": is_paused(e)} for r, e in get_projects(raw).items()}
    c_tokens = get_container_tokens() if c_state["running"] else []
    h_tokens = get_host_tokens()
    health = get_health_status() if c_state["running"] else {"tokens": [], "harnesses": {}, "error": "auto-review container is not running"}

    recent_logs = []
    if c_state["running"]:
        rc, out, _ = sh(["docker", "logs", "--tail", "50", "auto-review"], timeout=5)
        if rc == 0 and out:
            recent_logs = out.splitlines()[-50:]

    data = {
        "container": c_state,
        "global_harness": glob["harness"],
        "global_model": glob["model"],
        "available_harnesses": harnesses,
        "available_models": models,
        "default_models": default_models,
        "models_error": models_error,
        "projects": projs,
        "registry_error": registry_error,
        "container_tokens": c_tokens,
        "host_tokens": h_tokens,
        "has_tokens": len(c_tokens) > 0,
        "health": health,
        "recent_logs": recent_logs,
    }
    return data


def toggle_repo(repo):
    try:
        projs = get_projects()
    except RegistryError as e:
        return 1, str(e)
    if repo not in projs:
        return 1, f"repo {repo} not registered"
    if is_paused(projs[repo]):
        return cli_result(["resume", repo], f"{repo} is now resumed")
    return cli_result(["pause", repo], f"{repo} is now paused")


# Per-repo settings that remain; model, harness, interval and slots are the machine's (set-global).
REPO_KEYS = ("all", "self_review")


def set_repo(repo, key, val):
    try:
        projs = get_projects()
    except RegistryError as e:
        return 1, str(e)
    if repo not in projs:
        return 1, f"repo {repo} not registered"
    if key not in REPO_KEYS:
        return 1, f"{key} is not a per-repo setting; it is machine-wide (set-global)"
    val = "true" if str(val).lower() == "true" else "false"
    return cli_result(["set", repo, key, val], f"set {key}={val} on {repo}")


def adopt_repo(repo, all_prs=False, self_review=False):
    # Model, harness and interval are the machine's, not the repository's.
    args = ["adopt", repo]
    if all_prs:
        args.append("--all")
    if self_review:
        args.append("--self-review")
    return cli_result(args, f"adopted {repo}")


def forget_repo(repo):
    try:
        projs = get_projects()
    except RegistryError as e:
        return 1, str(e)
    if repo not in projs:
        return 1, f"repo {repo} not found"
    return cli_result(["forget", repo], f"forgot {repo}")


def build_tick_script(repo, harness, model, all_prs=False, self_review=False):
    """Build container shell script to verify the clone and run auto-review tick."""
    clone_path = f"/var/lib/auto-review/clones/{repo.replace('/', '-')}"
    git_dir = f"{clone_path}/.git"
    missing_msg = f"clone not found at {clone_path}; run: auto-review setup {repo}"
    tick_args = [
        "/usr/local/bin/auto-review",
        "tick",
        clone_path,
        "--model",
        model,
        "--harness",
        harness,
    ]
    if all_prs:
        tick_args.append("--all")
    if self_review:
        tick_args.append("--self-review")
    quoted_tick = " ".join(shlex.quote(arg) for arg in tick_args)
    return (
        f"if [ ! -d {shlex.quote(git_dir)} ]; then "
        f"printf '%s\\n' {shlex.quote(missing_msg)} >&2; exit 1; "
        f"fi; "
        f"exec {quoted_tick}"
    )


def trigger_tick(repo):
    c_state = get_container_state()
    if not c_state["running"]:
        return 1, "auto-review container is not running"
    try:
        raw = get_registry_raw()
    except RegistryError as e:
        return 1, str(e)
    glob = get_global(raw)
    projs = get_projects(raw)
    entry = projs.get(repo, {}) or {}
    script = build_tick_script(
        repo,
        glob["harness"],
        glob["model"],
        all_prs=bool(entry.get("all")),
        self_review=bool(entry.get("self_review")),
    )
    cmd = ["docker", "exec", "-u", GATE_UID, "auto-review", "sh", "-c", script]
    rc, out, err = sh(cmd, timeout=300)
    msg = out or err or ("tick completed" if rc == 0 else f"tick failed (exit {rc})")
    return rc, msg.splitlines()[-1] if msg.splitlines() else "done"


def main(argv):
    if not argv:
        print(json.dumps(status()))
        return 0
    verb = argv[0]
    rc = 0
    msg = "done"

    if verb == "status":
        print(json.dumps(status()))
        return 0
    elif verb == "start":
        rc, out, err = sh(["docker", "compose", "-f", str(COMPOSE), "up", "-d"], timeout=60)
        msg = out or err or "started"
        if rc == 0:
            sync_tokens()
    elif verb == "stop":
        rc, out, err = sh(["docker", "compose", "-f", str(COMPOSE), "down"], timeout=60)
        msg = out or err or "stopped"
    elif verb == "pause":
        rc, out, err = sh(["docker", "pause", "auto-review"], timeout=10)
        msg = out or err or "paused"
    elif verb in ("resume", "unpause"):
        c = get_container_state()
        if c.get("paused"):
            rc, out, err = sh(["docker", "unpause", "auto-review"], timeout=10)
            msg = out or err or "resumed"
        else:
            rc, out, err = sh(["docker", "compose", "-f", str(COMPOSE), "up", "-d"], timeout=60)
            msg = out or err or "started"
            if rc == 0:
                sync_tokens()
    elif verb == "restart":
        rc, out, err = sh(["docker", "compose", "-f", str(COMPOSE), "restart"], timeout=60)
        msg = out or err or "restarted"
        if rc == 0:
            sync_tokens()
    elif verb == "sync-tokens":
        rc, msg = sync_tokens()
    elif verb == "set-global" and len(argv) > 2:
        rc, msg = set_global(argv[1], argv[2])
    elif verb == "toggle" and len(argv) > 1:
        rc, msg = toggle_repo(argv[1])
    elif verb == "set" and len(argv) > 3:
        rc, msg = set_repo(argv[1], argv[2], argv[3])
    elif verb == "adopt" and len(argv) > 1:
        rc, msg = adopt_repo(argv[1], "--all" in argv[2:], "--self-review" in argv[2:])
    elif verb == "forget" and len(argv) > 1:
        rc, msg = forget_repo(argv[1])
    elif verb == "tick" and len(argv) > 1:
        rc, msg = trigger_tick(argv[1])
    else:
        print(f"unknown verb {verb}")
        return 2

    print(msg.strip() if msg else "done")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
