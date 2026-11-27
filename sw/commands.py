"""Generic command runner. A command does not write state: it changes the real system, then wakes the
watchers of its zones; `ok` is written only after a pass that started after it finished (core.cmd_on_pass).
"""

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from sw import core, ctx, watch

TICK = 5  # seconds between deadline sweeps
CONFIRM_S = 30  # window for the zone passes, measured from the moment the action finished


@dataclass(frozen=True)
class RouteSpec:
    validate: Callable[[dict], Optional[Any]]        # whole body (id included) -> args, or None when bad
    run: Callable[[Any], Tuple[int, str]]            # args -> (rc, message)
    zones: Callable[[Any], tuple]                    # args -> block names that must show the result
    deadline_s: int
    precheck: Optional[Callable[[Any, Any], Optional[Tuple[int, str]]]] = None  # (args, store) -> (code, error)
    wakes: Callable[[Any], tuple] = lambda args: ()  # blocks woken when the action finishes; they do not gate ok


class Commands:
    def __init__(self, store, watchers, routes, clock=time.time):
        self.store = store
        self.watchers = watchers
        self.routes = routes
        self.clock = clock
        self.lock = threading.Lock()  # read-modify-write of a command row
        self.inflight = set()  # ids whose action is queued or running: the deadline does not apply yet

    def submit(self, path, body):
        """(code, payload) for POST /api/<route>. The caller has checked host, token and that the route exists."""
        spec = self.routes[path]
        cid = body.get("id")
        if cid is None or cid == "":
            cid = "c" + secrets.token_hex(8)
            body = {**body, "id": cid}
        if not core.cmd_id_ok(cid):
            return 400, {"error": "bad command id"}
        args = spec.validate(body)
        if args is None:
            return 400, {"error": "bad parameters"}
        text = core.canonical(body)
        with self.lock:
            prior = self.store.get_command(cid)
            if prior is not None:
                same = prior["body"] == text
                return (202, {"ok": True, "id": cid}) if same else (409, {"error": "id reused with a different body"})
            if self.store.was_pruned(cid):
                return 409, {"error": "id reused after it was pruned"}
            if spec.precheck:
                refused = spec.precheck(args, self.store)
                if refused:
                    return refused[0], {"error": refused[1]}
            now = self.clock()
            zones = list(spec.zones(args))
            cmd = {"id": cid, "route": path, "body": text, "zones": zones, "waiting": zones, "state": "running",
                   "message": None, "created_at": now, "deadline_at": now + spec.deadline_s, "finished_seq": None,
                   "done_at": None, "rev": 0}
            self.store.save_command(cmd)
            self.inflight.add(cid)
        threading.Thread(target=self._exec, args=(cmd, spec, args), daemon=True, name="cmd-" + cid).start()
        return 202, {"ok": True, "id": cid}

    def _exec(self, cmd, spec, args):
        try:
            rc, msg = spec.run(args)
        except Exception as e:
            rc, msg = 1, f"{type(e).__name__}: {e}"[:300]
        s = watch.seq()
        try:
            self._apply(cmd["id"], lambda c: self._finish(c, rc, msg, s))
        finally:
            with self.lock:
                self.inflight.discard(cmd["id"])
        for zone in dict.fromkeys([*cmd["zones"], *spec.wakes(args)]):
            self.watchers.wake(zone)

    def _finish(self, cmd, rc, msg, seq):
        """core.cmd_on_finish, then a full confirmation window from now when zones still have to show it."""
        now = self.clock()
        new = core.cmd_on_finish(cmd, rc, msg, seq, now)
        if new["state"] == "running" and new["waiting"]:
            new = {**new, "deadline_at": max(new["deadline_at"], now + CONFIRM_S)}
        return new

    def _apply(self, cid, fn):
        with self.lock:
            cur = self.store.get_command(cid)
            if cur is None:
                return
            new = fn(cur)
            if new != cur:
                self.store.save_command(new)

    def on_pass(self, zone, pass_started_seq):
        """A watcher finished a pass of `zone` that started at `pass_started_seq`."""
        for c in self.store.commands("running"):
            if zone in c["waiting"]:
                self._apply(c["id"], lambda cur: core.cmd_on_pass(cur, zone, pass_started_seq, self.clock()))

    def sweep(self):
        errors = self.store.block_errors()
        with self.lock:
            busy = set(self.inflight)
        for c in self.store.commands("running"):
            if c["id"] in busy:
                continue
            self._apply(c["id"], lambda cur: core.cmd_on_deadline(cur, self.clock(), errors))

    def start_ticker(self):
        def loop():
            while True:
                time.sleep(TICK)
                try:
                    self.sweep()
                except Exception as e:
                    ctx.note(f"deadline sweep failed: {e}"[:200])
        threading.Thread(target=loop, daemon=True, name="cmd-ticker").start()
