#!/usr/bin/env python3
"""Tests for the HTTP contract and the SSE stream (issue #9 part A, issue #33, issue #34).

Runs an in-process ThreadingHTTPServer(('127.0.0.1', 0), Handler) backed by Store, Bus,
Commands (with an injected deterministic fake clock), and Watchers (driven via step(spec)),
with HOME and state dirs isolated in a temp directory.
"""

import contextlib
import http.client
import inspect
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from sw import config, ctx, domains
from sw.bus import Bus
from sw.commands import Commands
from sw.http import Handler
from sw.store import Store
from sw.watch import BlockSpec, Watchers

BAD_CI = ["on", "zz/none"]  # no checkout of it exists: fails fast without touching anything


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def set(self, value):
        self.now = float(value)
        return self.now


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls._stack = contextlib.ExitStack()
        dc_home = os.path.join(cls.home, "dev-containers")
        as_home = os.path.join(cls.home, "agent-slots")
        cache_dir = pathlib.Path(cls.home) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cls._stack.enter_context(mock.patch.dict(os.environ, {
            "HOME": cls.home,
            "DEV_CONTAINER_HOME": dc_home,
            "AGENT_SLOT_STATE": as_home,
        }))
        cls._stack.enter_context(mock.patch.object(config, "CACHE_DIR", cache_dir))
        cls._stack.enter_context(mock.patch.object(config, "LOG", cache_dir / "actions.log"))
        cls._stack.enter_context(mock.patch.object(config, "DB_PATH", cache_dir / "state.db"))

        cls._prev_ctx = (ctx.store, ctx.bus, ctx.commands, ctx.routes)
        cls.store = Store(cache_dir / "state.db")
        cls.bus = Bus()
        cls.store.publish = cls.bus.publish
        cls.store.boot()
        _blocks, routes = domains.discover()
        cls.clock = FakeClock(1000.0)
        cls.has_clock = "clock" in inspect.signature(Commands.__init__).parameters
        if cls.has_clock:
            cls.commands = Commands(cls.store, None, routes, clock=cls.clock)
        else:
            cls.commands = Commands(cls.store, None, routes)
        cls.watchers = Watchers(cls.store, cls.commands.on_pass)
        cls.commands.watchers = cls.watchers
        ctx.init(cls.store, cls.bus, cls.commands, routes)

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        cls.token = config.TOKEN
        cls._srv_thread = threading.Thread(target=cls.srv.serve_forever, daemon=True, name="test-sse-http")
        cls._srv_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._srv_thread.join(timeout=5)
        for t in list(threading.enumerate()):
            if t.name.startswith("cmd-") and t.is_alive():
                t.join(timeout=5)
        ctx.init(*cls._prev_ctx)
        cls.store.close()
        cls._stack.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def conn(cls, timeout=10):
        return http.client.HTTPConnection("127.0.0.1", cls.port, timeout=timeout)

    def post(self, path, body, token=True, host=None, raw=None):
        c = self.conn()
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Token"] = self.token
        if host:
            h["Host"] = host
        c.request("POST", path, raw if raw is not None else json.dumps(body), h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def stream(self, until, timeout=10, path="/api/events"):
        """Read SSE frames until until(frames) is true or the time is up; returns (status, response, frames)."""
        c = self.conn(timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        frames, cur, first = [], {}, None
        end = time.monotonic() + timeout
        if r.status != 200:
            c.close()
            return r.status, r, frames
        try:
            while time.monotonic() < end:
                try:
                    line = r.fp.readline().decode()
                except (socket.timeout, TimeoutError):
                    continue
                if not line:
                    break
                line = line.rstrip("\n")
                if first is None:
                    first = line
                if line == "":
                    if cur:
                        frames.append(cur)
                        cur = {}
                        if until(frames):
                            break
                elif not line.startswith(":") and ": " in line:
                    k, v = line.split(": ", 1)
                    cur[k] = v
        finally:
            c.close()
        r.first_line = first
        return 200, r, frames


class TestHTTPContract(ServerCase):
    def test_post_without_token_is_403(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-aaaa0001", "args": BAD_CI}, token=False)[0], 403)

    def test_post_with_foreign_host_is_403(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-aaaa0002", "args": BAD_CI}, host="evil.example")[0], 403)

    def test_unknown_route_is_404(self):
        self.assertEqual(self.post("/api/nope", {"id": "cmd-aaaa0003"})[0], 404)

    def test_retired_project_limit_route_is_404(self):
        self.assertEqual(self.post("/api/project-limit", {"id": "cmd-aaaa0099", "project": "acme-org", "delta": 1})[0], 404)

    def test_bad_json_is_400(self):
        self.assertEqual(self.post("/api/ci", None, raw="{not json")[0], 400)

    def test_bad_args_is_400(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-aaaa0004", "args": ["on", "a/b;rm"]})[0], 400)

    def test_malformed_id_is_400(self):
        self.assertEqual(self.post("/api/ci", {"id": "a b", "args": BAD_CI})[0], 400)

    def test_accepted_is_202_with_id(self):
        code, body = self.post("/api/ci", {"id": "cmd-aaaa0005", "args": BAD_CI})
        self.assertEqual(code, 202)
        self.assertEqual(body, {"ok": True, "id": "cmd-aaaa0005"})

    def test_same_id_same_body_is_202_again(self):
        b = {"id": "cmd-aaaa0006", "args": BAD_CI}
        self.assertEqual(self.post("/api/ci", b)[0], 202)
        self.assertEqual(self.post("/api/ci", b)[0], 202)

    def test_same_id_different_body_is_409(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-aaaa0007", "args": BAD_CI})[0], 202)
        self.assertEqual(self.post("/api/ci", {"id": "cmd-aaaa0007", "args": ["off", "zz/none"]})[0], 409)

    def test_missing_id_is_filled_in_server_side(self):
        code, body = self.post("/api/ci", {"args": BAD_CI})
        self.assertEqual(code, 202)
        self.assertRegex(str(body.get("id") if isinstance(body, dict) else body), r"^[A-Za-z0-9_-]{8,64}$")


class TestCommandsOnTheStream(ServerCase):
    def command_event(self, cid, states, timeout=10):
        def matched(fs):
            for f in fs:
                if f.get("event") == "command":
                    d = json.loads(f["data"])
                    if d["id"] == cid and d["state"] in states:
                        return True
            return False

        code, _, frames = self.stream(matched, timeout=timeout)
        self.assertEqual(code, 200, "GET /api/events must stream (issue #9 part A)")
        for f in frames:
            if f.get("event") == "command":
                d = json.loads(f["data"])
                if d["id"] == cid and d["state"] in states:
                    return d
        self.fail("no command event for %s in states %s" % (cid, states))

    def test_failed_command_is_reported_with_message(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-bbbb0001", "args": BAD_CI})[0], 202)
        d = self.command_event("cmd-bbbb0001", ("failed",))
        self.assertTrue(d["message"])
        self.assertEqual(d["route"], "/api/ci")

    def test_running_command_carries_deadline_for_its_route(self):
        self.assertTrue(self.has_clock, "Commands.__init__ must accept clock=time.time (issue #33)")
        t0 = self.clock.set(1000.0)
        self.assertEqual(self.post("/api/ci", {"id": "cmd-bbbb0002", "args": BAD_CI})[0], 202)
        d = self.command_event("cmd-bbbb0002", ("running", "ok", "failed"))
        self.assertEqual(d["deadline_at"], t0 + 150)

    def test_cap_deadline_is_30_seconds(self):
        self.assertTrue(self.has_clock, "Commands.__init__ must accept clock=time.time (issue #33)")
        t0 = self.clock.set(2000.0)
        self.assertEqual(self.post("/api/cap", {"id": "cmd-bbbb0003", "delta": -1})[0], 202)
        d = self.command_event("cmd-bbbb0003", ("running", "ok", "failed"))
        self.assertEqual(d["deadline_at"], t0 + 30)


class TestStream(ServerCase):
    def test_connect_sends_everything_in_order(self):
        code, r, frames = self.stream(lambda fs: any(f.get("event") == "ready" for f in fs))
        self.assertEqual(code, 200, "GET /api/events must stream (issue #9 part A)")
        events = [f["event"] for f in frames]
        self.assertEqual(events[0], "hello")
        self.assertEqual(events[-1], "ready")
        order = {"hello": 0, "block": 1, "command": 2, "log": 3, "ready": 4}
        ranks = [order[e] for e in events]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(set(json.loads(frames[0]["data"])), {"boot", "rev", "now"})
        self.assertEqual(set(json.loads(frames[-1]["data"])), {"rev"})

    def test_headers_and_retry_line(self):
        code, r, frames = self.stream(lambda fs: len(fs) >= 1, timeout=10)
        self.assertEqual(code, 200, "GET /api/events must stream (issue #9 part A)")
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("X-Accel-Buffering"), "no")
        self.assertEqual(r.first_line, "retry: 3000")

    def test_hello_boot_is_stable_between_connects(self):
        a = self.stream(lambda fs: len(fs) >= 1, timeout=10)
        b = self.stream(lambda fs: len(fs) >= 1, timeout=10)
        self.assertEqual(a[0], 200, "GET /api/events must stream (issue #9 part A)")
        self.assertEqual(json.loads(a[2][0]["data"])["boot"], json.loads(b[2][0]["data"])["boot"])

    def test_a_block_event_arrives_with_the_documented_shape(self):
        self.assertTrue(hasattr(self.watchers, "step"), "Watchers must expose step(spec) (issue #33)")
        spec = BlockSpec("test_block", 10, lambda _st: {"ok": True})
        self.watchers.specs[spec.name] = spec
        self.watchers.events[spec.name] = threading.Event()
        self.watchers.step(spec)
        code, _, frames = self.stream(lambda fs: any(f.get("event") == "block" for f in fs), timeout=5)
        self.assertEqual(code, 200, "GET /api/events must stream (issue #9 part A)")
        blocks = [f for f in frames if f.get("event") == "block"]
        self.assertTrue(blocks, "no block event arrived after watchers.step(spec)")
        d = json.loads(blocks[0]["data"])
        self.assertEqual(set(d), {"name", "rev", "updated_at", "period", "error", "data"})
        self.assertEqual(blocks[0]["id"], str(d["rev"]))

    def test_foreign_host_is_403(self):
        c = self.conn()
        c.request("GET", "/api/events", headers={"Host": "evil.example"})
        self.assertEqual(c.getresponse().status, 403)

    def test_query_string_is_tolerated(self):
        code, _, frames = self.stream(lambda fs: len(fs) >= 1, timeout=10, path="/api/events?x=1")
        self.assertEqual(code, 200, "GET /api/events?x=1 must stream (issue #9 part A)")

    def test_head_does_not_stream_forever(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request("HEAD", "/api/events")
            c.getresponse().read()
        except socket.timeout:
            self.fail("HEAD /api/events hung")


class TestStatic(ServerCase):
    def get(self, path):
        c = self.conn()
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()

    def test_serves_a_script(self):
        code, body = self.get("/static/store.js")
        self.assertEqual(code, 200)
        self.assertIn(b"EventSource", body)

    def test_refuses_names_outside_the_pattern(self):
        self.assertEqual(self.get("/static/Bad.js")[0], 404)
        self.assertEqual(self.get("/static/../server.py")[0], 404)
        self.assertEqual(self.get("/static/store.py")[0], 404)


class TestNegativeContentLength(ServerCase):
    def test_negative_content_length_is_400_and_does_not_block(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(("POST /api/ci HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Token: %s\r\nContent-Length: -1\r\n\r\n"
                       % (self.port, self.token)).encode())
            try:
                head = s.recv(256).decode("latin-1")
            except socket.timeout:
                self.fail("the server blocked reading a negative Content-Length")
        finally:
            s.close()
        self.assertTrue(head.startswith("HTTP/1.1 400") or head.startswith("HTTP/1.0 400"), head[:40])


class TestUiOnTheServer(ServerCase):
    UI = {"key": "ci.budget", "value": 7}

    def test_post_with_ui_is_accepted(self):
        code, body = self.post("/api/ci", {"id": "cmd-cccc0001", "args": BAD_CI, "ui": self.UI})
        self.assertEqual(code, 202)
        self.assertEqual(body, {"ok": True, "id": "cmd-cccc0001"})

    def test_same_id_different_ui_is_409(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-cccc0002", "args": BAD_CI, "ui": self.UI})[0], 202)
        self.assertEqual(self.post("/api/ci", {"id": "cmd-cccc0002", "args": BAD_CI, "ui": {"key": "x", "value": 1}})[0], 409)

    def test_command_event_exposes_ui(self):
        self.assertEqual(self.post("/api/ci", {"id": "cmd-cccc0003", "args": BAD_CI, "ui": self.UI})[0], 202)

        def matched(fs):
            return any(
                f.get("event") == "command" and json.loads(f["data"])["id"] == "cmd-cccc0003"
                for f in fs
            )

        code, _, frames = self.stream(matched, timeout=10)
        self.assertEqual(code, 200)
        for f in frames:
            if f.get("event") == "command":
                d = json.loads(f["data"])
                if d["id"] == "cmd-cccc0003":
                    self.assertEqual(d["body"].get("ui"), self.UI)
                    return
        self.fail("no command event for cmd-cccc0003")


class TestStoreStructure(unittest.TestCase):
    STORE_JS = (pathlib.Path(__file__).resolve().parents[1] / "static" / "store.js").read_text(encoding="utf-8")

    def test_command_sends_ui(self):
        m = re.search(r"JSON\.stringify\(.*\bui\b", self.STORE_JS)
        self.assertTrue(m, "command() must put ui = {key, value} in the POST body")

    def test_command_sends_ui_only_with_a_key(self):
        self.assertRegex(self.STORE_JS, r"o\.key\s*\?\s*\{\s*key:\s*o\.key", "ui is sent only when opts.key is given")

    def test_running_event_rebuilds_pending_from_body_ui(self):
        self.assertIn("body.ui", self.STORE_JS)
        self.assertRegex(self.STORE_JS, r"seenRunning:\s*true", "a rebuilt entry has been seen running")

    def test_rebuilt_entry_uses_the_command_deadline(self):
        self.assertGreaterEqual(len(re.findall(r"deadline_at\s*\*\s*1000", self.STORE_JS)), 2,
                                "both the running update and the rebuild take the server deadline")


class TestPageStoreReconcile(unittest.TestCase):
    def test_store_js_drops_pending_ids_not_running_in_the_snapshot(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "static" / "store.js").read_text(encoding="utf-8")
        self.assertIn("function reconcilePending", src)
        ready = [ln for ln in src.splitlines() if "on(es, 'ready'" in ln]
        self.assertEqual(len(ready), 1)
        self.assertIn("reconcilePending", ready[0])


class TestCiQueueRepoName(unittest.TestCase):
    def calls(self, repos):
        bin_dir = str(pathlib.Path(__file__).resolve().parents[1] / "bin")
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        import ci
        run = mock.MagicMock(return_value=mock.MagicMock(stdout="", returncode=0))
        with mock.patch.object(ci.subprocess, "run", run):
            ci.queue(repos)
        return [c.args[0] for c in run.call_args_list]

    def test_hostile_repo_name_is_not_put_in_a_shell_string(self):
        evil = "a/b'; touch /tmp/pwned; echo '"
        for argv in self.calls([evil]):
            if argv[:2] == ["bash", "-lc"]:
                self.assertNotIn(evil, argv[2])
                self.assertNotIn("touch /tmp/pwned", argv[2])

    def test_a_normal_repo_is_still_queried(self):
        self.assertTrue(self.calls(["hanzhad/ops-console"]), "a valid repo must still be queried")


class TestEscSingleQuote(unittest.TestCase):
    def esc(self, s):
        src = (pathlib.Path(__file__).resolve().parents[1] / "static" / "util.js").read_text(encoding="utf-8")
        js = "const esc=new Function(process.argv[1]+';return esc;')();process.stdout.write(esc(process.argv[2]))"
        p = subprocess.run(["node", "-e", js, src, s], capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_single_quote_is_escaped(self):
        self.assertNotIn("'", self.esc("x');alert(1);('"))

    def test_other_characters_still_escaped(self):
        self.assertEqual(self.esc('<&>"'), "&lt;&amp;&gt;&quot;")


if __name__ == "__main__":
    unittest.main()
