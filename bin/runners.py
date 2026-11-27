#!/usr/bin/env python3
"""CI runner containers: one entry per `ci-runner.class` container, busy when it has a Runner.Worker process.

Prints a JSON array when run (regate.sh counts the busy ones); the runners block of the server calls collect().
"""

import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sw import core  # noqa: E402

PS_FORMAT = '{{.Names}}|{{.Label "ci-runner.class"}}|{{.Label "ci-runner.gate"}}'
JOB_CMD = ("f=$(ls -t /opt/runner/_diag/Worker_*.log 2>/dev/null | head -1); "
           "[ -n \"$f\" ] && grep -o -m1 '\"jobDisplayName\": *\"[^\"]*\"' \"$f\" | sed 's/.*: *\"//; s/\"$//'")


def sh(argv, timeout=15):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def parse_ps(out):
    """`docker ps` lines `name|class|gate` -> [(name, klass, gate_slug)]; lines without a separator are dropped."""
    rows = []
    for line in (out or "").splitlines():
        if "|" in line:
            name, klass, gate_slug = (line.split("|") + ["", ""])[:3]
            rows.append((name, klass, gate_slug))
    return rows


def build_runner(name, klass, gate_slug, stat, worker, job):
    m = re.fullmatch(r"ci-runner-(.+)-pc-(\d+)-\d+", name)
    cpu, mem = stat
    return {
        "repo": gate_slug or (m[1] if m else name),
        "n": int(m[2]) if m else 0,
        "class": klass or "big",
        "busy": bool(worker),
        "job": job,
        "job_time": worker[1] if worker else None,
        "cpu": cpu,
        "mem_mb": round(mem),
    }


def sort_runners(runners):
    return sorted(runners, key=lambda c: (c["class"], c["repo"], c["n"]))


def busy_count(runners):
    return sum(1 for c in runners if c.get("busy"))


def collect(stats=None):
    """Every runner container; `stats` is {name: [cpu, mem_mb]} (the host block's sample), read here when None."""
    if stats is None:
        stats = core.parse_docker_stats(sh(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"]))
    runners = []
    for name, klass, gate_slug in parse_ps(sh(["docker", "ps", "--filter", "label=ci-runner.class", "--format", PS_FORMAT])):
        worker = next((r.split(None, 2) for r in sh(["docker", "top", name, "-eo", "pid,etime,comm"]).splitlines()
                       if "Runner.Worker" in r), None)
        job = (sh(["docker", "exec", name, "sh", "-c", JOB_CMD], 10).strip() or None) if worker else None
        runners.append(build_runner(name, klass, gate_slug, stats.get(name, (0, 0)), worker, job))
    return sort_runners(runners)


def main(argv=None):
    print(json.dumps(collect()))


if __name__ == "__main__":
    main()
