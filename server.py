#!/usr/bin/env python3
"""ops-console server: Zero-Hardcode System-Reflecting Operations Console.

Serves http://127.0.0.1:7631 (systemd unit ops-console.service). One watcher per block of state writes a
SQLite read model (~/.cache/ops-console/state.db); one SSE stream (/api/events) carries blocks, command
statuses and log lines to the page. The code lives in sw/ (core, store, bus, watch, commands, http, domains/).
"""

import argparse
from http.server import ThreadingHTTPServer
import os
import pathlib
import tempfile

from sw import config, ctx, demo, domains
from sw.bus import Bus
from sw.commands import Commands
from sw.http import Handler
from sw.store import Store
from sw.watch import Watchers


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ops-console HTTP & SSE operations dashboard")
    p.add_argument("--demo", action="store_true", help="Serve sanitized fixture data without Docker/host probes")
    p.add_argument("--host", default=None, help="Bind address (default 0.0.0.0, or 127.0.0.1 in --demo)")
    p.add_argument("--port", type=int, default=config.PORT, help="Listen port (default OPS_CONSOLE_PORT or 7631)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    is_demo = args.demo or os.environ.get("OPS_CONSOLE_DEMO") == "1"
    tmp_dir = tempfile.TemporaryDirectory(prefix="ops-console-demo-") if is_demo else None
    db_path = (pathlib.Path(tmp_dir.name) / "state.db") if tmp_dir else config.DB_PATH
    if not is_demo:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    store, bus = Store(db_path), Bus()
    store.publish = bus.publish
    store.boot()
    blocks, routes = domains.discover()
    commands = Commands(store, None, routes)
    watchers = Watchers(store, commands.on_pass)
    commands.watchers = watchers
    ctx.init(store, bus, commands, routes)
    if is_demo:
        demo.seed_demo(store)
    else:
        watchers.start(blocks)
        commands.start_ticker()
    host = args.host or ("127.0.0.1" if is_demo else "0.0.0.0")
    srv = ThreadingHTTPServer((host, args.port), Handler)
    label = "ops-console (demo)" if is_demo else "ops-console"
    print(f"{label} on http://{host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
