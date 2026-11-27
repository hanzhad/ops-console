#!/usr/bin/env python3
"""The CI half of the ops-console pane, through host_repo's ci-runner.sh (the design docs).

  ci.py status                   JSON: gate state, live budget, repository switches
  ci.py pause | resume | reset
  ci.py budget <cores> <cap>
  ci.py on|off <owner/repo>      flips CI_RUNNER directly by owner/repo (checkout_of fallback for legacy script)
  ci.py mega-on|mega-off <owner/repo>

sw/domains/ci.py imports gate_status() and queue(); nothing is cached here.
"""
import json, pathlib, re, shlex, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sw import core  # noqa: E402

HOME = pathlib.Path.home()
CI_TOOLS = HOME / "Projects/host_repo/tools/ci-runner/ci-runner.sh"
CI = CI_TOOLS if CI_TOOLS.exists() else (HOME / "Projects/host_repo/ci-runner/ci-runner.sh")
SKIP = {"hanzhad/demo-app-ai-extension"}  # dropped from cross-project work


def run(*args, timeout=60):
    cmd = " ".join(shlex.quote(str(a)) for a in (CI, *args))
    p = subprocess.run(["bash", "-lc", f"{cmd} 2>&1"], capture_output=True, text=True, timeout=timeout)
    return p.returncode, "\n".join(l for l in p.stdout.splitlines() if "WARNING" not in l)


def _overlay_live_budget(data, budget_out):
    parsed = core.parse_ci_budget(budget_out)
    data["boot"] = parsed.get("boot")
    if parsed.get("live") is True and parsed.get("budget") is not None and parsed.get("cap") is not None:
        data["budget"] = parsed["budget"]
        data["cap"] = parsed["cap"]
        data["live"] = True
    elif re.search(r"^live\s+not set\b", budget_out or "", re.M) and isinstance(parsed.get("boot"), list) and len(parsed["boot"]) == 2:
        data["budget"], data["cap"] = parsed["boot"]
        data["live"] = False
    return data


def gate_status():
    """The ci_gate block: prefers `status --json` (schema 1), falling back to legacy `list` and `budget`."""
    rc, out = run("status", "--json")
    idx = out.find("{") if rc == 0 else -1
    if idx != -1:
        try:
            data = core.parse_ci_status_json(out[idx:], SKIP)
            if data is not None:
                b_rc, b = run("budget")
                if b_rc == 0:
                    _overlay_live_budget(data, b)
                return data
        except ValueError:
            pass
    _, out = run("list")
    _, b = run("budget")
    return {
        **core.parse_ci_list(out, SKIP),
        **core.parse_ci_budget(b),
        "queue": [],
        "non_local_queued": 0,
        "rate_limit": None,
        "queue_unknown": True,
    }


def status():
    """Gate plus queue, for the CLI."""
    data = gate_status()
    data["queue"] = core.sort_queue(data.get("queue") or [])
    return data


def queue(repos=None):
    """Runs waiting for a runner, read from ci_gate status --json (0 GitHub API calls). Returns None when unknown."""
    data = gate_status()
    if data.get("queue_unknown"):
        return None
    return core.sort_queue(data.get("queue") or [])


def checkout_of(repo):
    for d in [*HOME.glob("Projects/*"), *HOME.glob("Projects/*/*")]:
        if not (d / ".git").is_dir():  # a main checkout, not a worktree
            continue
        url = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
        if re.search(rf"[:/]{re.escape(repo)}(\.git)?$", url):
            return d
    return None


def main(argv):
    verb = argv[0]
    if verb == "status":
        print(json.dumps(status()))
        return 0
    if verb in ("pause", "resume"):
        rc, out = run(verb)
    elif verb == "reset":
        rc, out = run("budget", "--reset")
    elif verb == "budget":
        rc, out = run("budget", str(int(argv[1])), "--big-cap", str(int(argv[2])))
    elif verb in ("on", "off", "mega-on", "mega-off"):
        repo = argv[1]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            print(f"bad repo {repo!r}")
            return 2
        uses_tools = (CI == CI_TOOLS)
        if verb in ("on", "off"):
            if uses_tools:
                target = repo
            else:
                d = checkout_of(repo)
                if d is None:
                    print(f"no checkout of {repo} under ~/Projects")
                    return 1
                target = str(d)
            rc, out = run(verb, target, timeout=90)
        else:
            if not uses_tools:
                print("mega requires tools/ci-runner/ci-runner.sh")
                return 2
            rc, out = run("mega", "on" if verb == "mega-on" else "off", repo, timeout=90)
    else:
        print(f"unknown verb {verb}")
        return 2
    print(out.strip().splitlines()[-1] if out.strip() else "done")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
