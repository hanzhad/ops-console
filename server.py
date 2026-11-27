#!/usr/bin/env python3
"""ops-console server: Zero-Hardcode System-Reflecting Operations Console.

Serves http://127.0.0.1:7631 (systemd unit ops-console.service). One watcher per block of state writes a
SQLite read model (~/.cache/ops-console/state.db); one SSE stream (/api/events) carries blocks, command
statuses and log lines to the page. The code lives in sw/ (core, store, bus, watch, commands, http, domains/).
"""

from http.server import ThreadingHTTPServer

from sw import config, ctx, domains
from sw.bus import Bus
from sw.commands import Commands
from sw.http import Handler
from sw.store import Store
from sw.watch import Watchers


def main():
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    store, bus = Store(config.DB_PATH), Bus()
    store.publish = bus.publish
    store.boot()
    blocks, routes = domains.discover()
    commands = Commands(store, None, routes)
    watchers = Watchers(store, commands.on_pass)
    commands.watchers = watchers
    ctx.init(store, bus, commands, routes)
    watchers.start(blocks)
    commands.start_ticker()
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    print(f"ops-console on http://0.0.0.0:{config.PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
