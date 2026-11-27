"""Host telemetry block: load, memory and the one `docker stats` sample the slots and runners blocks read."""

import os
import pathlib
import subprocess

from sw import core
from sw.watch import BlockSpec

PERIOD = 5
STATS_ARGV = ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"]


def docker_stats():
    try:
        return subprocess.run(STATS_ARGV, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


def read_host(store):
    return core.parse_host(pathlib.Path("/proc/meminfo").read_text(), pathlib.Path("/proc/loadavg").read_text(),
                           os.cpu_count(), core.parse_docker_stats(docker_stats()))


BLOCKS = [BlockSpec("host", PERIOD, read_host)]
ROUTES = {}
