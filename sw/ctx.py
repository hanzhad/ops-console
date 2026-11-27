"""Shared runtime handles and helpers for the domain modules.

`init` is called from main(); importing this module opens nothing and starts nothing.
"""

import subprocess
import sys
import threading
import time

from sw import config

store = None  # sw.store.Store, set by init()
bus = None  # sw.bus.Bus
commands = None  # sw.commands.Commands
routes = {}  # path -> RouteSpec
action_lock = threading.Lock()


def init(st, bus_=None, commands_=None, routes_=None):
    global store, bus, commands, routes
    store, bus, commands, routes = st, bus_, commands_, routes_ or {}


def note(text: str):
    """One line into actions.log (the full history) and into the events table (what the page shows)."""
    line = f"{time.strftime('%H:%M:%S')} {text}"
    if store is not None:
        try:
            store.add_event(line)
        except Exception:
            pass
    try:
        config.LOG.parent.mkdir(parents=True, exist_ok=True)
        with config.LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_bin(script: str, *args, timeout: int = 120):
    p = subprocess.run([sys.executable, str(config.BIN / script), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout.strip() or p.stderr.strip())[-300:]
