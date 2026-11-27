"""HTTP routing, replies, the SSE loop and static files."""

import json
import re
import secrets
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any

from sw import config, core, ctx
from sw.store import command_payload

STATIC = re.compile(r"/static/([a-z_]+)\.js")
KEEPALIVE = 15
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code: int, body: Any, ctype: str = "application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        return urllib.parse.urlsplit(self.path).path

    def do_HEAD(self):
        if self.route() == "/api/events":
            if not core.is_valid_host(self.headers.get("Host")):
                return self.reply(403, {"error": "host"})
            return self.sse_headers()  # a HEAD must not open the stream
        self.do_GET()

    def do_GET(self):
        if not core.is_valid_host(self.headers.get("Host")):
            return self.reply(403, {"error": "host"})
        path = self.route()
        if path in ("/", "/index.html"):
            page = (config.HERE / "page.html").read_text(encoding="utf-8").replace("__TOKEN__", config.TOKEN)
            return self.reply(200, page.encode("utf-8"), "text/html")
        m = STATIC.fullmatch(path)
        if m:
            f = config.HERE / "static" / f"{m[1]}.js"
            if f.is_file():
                return self.reply(200, f.read_bytes(), "application/javascript")
        elif path == "/api/state":
            return self.reply(200, self.state_payload())
        elif path == "/api/events":
            return self.stream()
        self.reply(404, {"error": "not found"})

    def state_payload(self):
        blocks = ctx.store.blocks()
        error = next((b["error"] for b in blocks if b["error"]), None)
        return core.api_state({b["name"]: b["data"] for b in blocks}, ctx.store.commands(),
                              [e["text"] for e in ctx.store.events(20)], time.time(), error)

    def do_POST(self):
        if not core.is_valid_host(self.headers.get("Host")) or not secrets.compare_digest(self.headers.get("X-Token", ""), config.TOKEN):
            return self.reply(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n < 0:
                return self.reply(400, {"error": "bad content length"})
            body = json.loads(self.rfile.read(min(n, 16384)) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "bad json"})
        path = self.route()
        if not isinstance(body, dict):
            return self.reply(400, {"error": "bad json"})
        if path not in ctx.routes:
            return self.reply(404, {"error": "not found"})
        try:
            code, payload = ctx.commands.submit(path, body)
        except sqlite3.Error as e:
            code, payload = 503, {"error": f"database: {e}"[:200]}
        self.reply(code, payload)

    # --- GET /api/events ------------------------------------------------------------------

    def sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def send_full(self):
        """Everything, in order: hello, blocks, commands, log lines, ready. Returns the rev it is current to."""
        rev, blocks, cmds, events = ctx.store.full_view()
        w = self.wfile.write
        w(core.sse_frame("hello", {"boot": ctx.store.boot_id(), "rev": rev, "now": time.time()}, rev))
        for b in blocks:
            w(core.sse_frame("block", b, b["rev"]))
        for c in cmds:
            w(core.sse_frame("command", command_payload(c), c["rev"]))
        for e in events:
            w(core.sse_frame("log", e, e["rev"]))
        w(core.sse_frame("ready", {"rev": rev}, rev))
        return rev

    def stream(self):
        self.sse_headers()
        try:
            self.wfile.write(b"retry: 3000\n")  # no blank line: it belongs to the hello frame that follows
            last = self.send_full()
            while True:
                frames, gap, tip = ctx.bus.wait_after(last, KEEPALIVE)
                if gap:
                    last = self.send_full()
                elif frames:
                    self.wfile.write(b"".join(frames))
                    last = tip
                else:
                    self.wfile.write(core.sse_ping_frame())
        except GONE:
            pass
