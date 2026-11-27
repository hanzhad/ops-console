#!/usr/bin/env python3
"""Tests for sw/store.py (issue #9, part A): the SQLite read model and put_block.

Assumed from the blueprint: `sw.store.Store(path)` opens the database (an optional `boot()`
method, if the constructor does not already run the boot steps). The database is inspected
through plain sqlite3 against the schema the issue gives.
"""

import importlib
import inspect
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

from sw import commands as cmds
from sw import core
from sw import watch
from sw.commands import Commands, RouteSpec


def new_store(case, path=None):
    try:
        mod = importlib.import_module("sw.store")
    except ImportError as e:
        case.fail("sw/store.py is missing (issue #9 part A): " + str(e))
    if not hasattr(mod, "Store"):
        case.fail("sw/store.py lacks class Store(path) (issue #9 part A)")
    if path is None:
        d = tempfile.mkdtemp()
        case.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "state.db")
    st = mod.Store(path)
    if hasattr(st, "boot"):
        st.boot()
    case.addCleanup(lambda: getattr(st, "close", lambda: None)())
    return st, path


def rows(path, sql, *args):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


class TestSchema(unittest.TestCase):
    def test_tables_wal_and_boot_id(self):
        st, path = new_store(self)
        names = {r[0] for r in rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"meta", "blocks", "commands", "events"} <= names, names)
        self.assertEqual(rows(path, "PRAGMA journal_mode")[0][0].lower(), "wal")
        boot = rows(path, "SELECT value FROM meta WHERE key='boot'")
        self.assertEqual(len(boot), 1)
        self.assertEqual(len(boot[0][0]), 16)

    def test_command_state_is_constrained(self):
        st, path = new_store(self)
        con = sqlite3.connect(path)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO commands(id,route,body,zones,waiting,state,created_at,deadline_at,rev)"
                        " VALUES('x','/api/ci','{}','[]','[]','bogus',1,2,1)")
        con.close()


class TestPutBlock(unittest.TestCase):
    def test_first_write_returns_rev_and_stores_row(self):
        st, path = new_store(self)
        rev = st.put_block("host", {"a": 1}, 1)
        self.assertIsInstance(rev, int)
        r = rows(path, "SELECT json, rev, pass_seq FROM blocks WHERE name='host'")
        self.assertEqual(r, [('{"a":1}', rev, 1)])

    def test_change_bumps_rev(self):
        st, path = new_store(self)
        r1 = st.put_block("host", {"a": 1}, 1)
        r2 = st.put_block("host", {"a": 2}, 2)
        self.assertGreater(r2, r1)

    def test_same_returns_none_but_updates_pass_seq_and_time(self):
        st, path = new_store(self)
        rev = st.put_block("host", {"a": 1, "b": 2}, 1)
        before = rows(path, "SELECT updated_at FROM blocks WHERE name='host'")[0][0]
        time.sleep(0.02)
        self.assertIsNone(st.put_block("host", {"b": 2, "a": 1}, 7))
        r = rows(path, "SELECT rev, pass_seq, updated_at FROM blocks WHERE name='host'")[0]
        self.assertEqual((r[0], r[1]), (rev, 7))
        self.assertGreater(r[2], before)

    def test_older_pass_is_refused_without_write_or_bump(self):
        st, path = new_store(self)
        rev = st.put_block("ci_gate", {"local": True}, 9)
        self.assertIsNone(st.put_block("ci_gate", {"local": False}, 7))
        r = rows(path, "SELECT json, rev, pass_seq FROM blocks WHERE name='ci_gate'")
        self.assertEqual(r, [('{"local":true}', rev, 9)])

    def test_revs_are_global_across_blocks(self):
        st, path = new_store(self)
        a = st.put_block("a", 1, 1)
        b = st.put_block("b", 1, 1)
        self.assertGreater(b, a)

    def test_put_error_bumps_rev_and_records_error(self):
        st, path = new_store(self)
        rev = st.put_block("host", {"a": 1}, 1)
        st.put_error("host", "boom")
        r = rows(path, "SELECT rev, error, error_at FROM blocks WHERE name='host'")[0]
        self.assertGreater(r[0], rev)
        self.assertIn("boom", r[1])
        self.assertIsNotNone(r[2])


class TestBoot(unittest.TestCase):
    def reopen(self, st, path):
        getattr(st, "close", lambda: None)()
        return new_store(self, path)

    def test_boot_resets_pass_seq_so_new_passes_count(self):
        st, path = new_store(self)
        st.put_block("host", {"a": 1}, 50)
        st2, _ = self.reopen(st, path)
        self.assertEqual(rows(path, "SELECT pass_seq FROM blocks WHERE name='host'")[0][0], 0)
        self.assertIsNotNone(st2.put_block("host", {"a": 2}, 1))

    def test_boot_fails_running_commands_only(self):
        st, path = new_store(self)
        con = sqlite3.connect(path)
        for i, state in enumerate(("running", "ok")):
            con.execute("INSERT INTO commands(id,route,body,zones,waiting,state,created_at,deadline_at,rev)"
                        " VALUES(?,?,?,?,?,?,1,2,?)", ("c%d" % i, "/api/ci", "{}", "[]", "[]", state, i + 1))
        con.commit()
        con.close()
        self.reopen(st, path)
        got = dict(rows(path, "SELECT id, state FROM commands"))
        self.assertEqual(got, {"c0": "failed", "c1": "ok"})
        msg = rows(path, "SELECT message FROM commands WHERE id='c0'")[0][0]
        self.assertIn("server restarted", msg)

    def test_boot_id_changes_each_boot(self):
        st, path = new_store(self)
        first = rows(path, "SELECT value FROM meta WHERE key='boot'")[0][0]
        self.reopen(st, path)
        second = rows(path, "SELECT value FROM meta WHERE key='boot'")[0][0]
        self.assertNotEqual(first, second)

    def test_rev_counter_continues_after_restart(self):
        st, path = new_store(self)
        last = st.put_block("host", {"a": 1}, 1)
        st2, _ = self.reopen(st, path)
        self.assertGreater(st2.put_block("host", {"a": 2}, 1), last)

    def test_store_boot_stores_done_at(self):
        st, path = new_store(self)
        st.save_command(_cmd("running01"))
        st2, _ = self.reopen(st, path)
        row = st2.get_command("running01")
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["done_at"], "the booted-failed command row must carry done_at")


def _cmd(cid, state="running", rev=0, **kw):
    c = {"id": cid, "route": "/api/x", "body": "{}", "zones": [], "waiting": [], "state": state, "message": None,
         "created_at": 1.0, "deadline_at": 2.0, "finished_seq": None, "done_at": None, "rev": rev}
    c.update(kw)
    return c


class TestPutErrorUnchanged(unittest.TestCase):
    def test_same_error_text_bumps_no_rev_and_publishes_nothing(self):
        st, _ = new_store(self)
        frames = []
        st.publish = lambda ev, payload, rev: frames.append((ev, rev))
        st.put_error("ci", "gh: boom")
        rev, n = st.rev, len(frames)
        st.put_error("ci", "gh: boom")
        self.assertEqual(st.rev, rev, "an unchanged error must not bump the rev")
        self.assertEqual(len(frames), n, "an unchanged error must not publish a frame")

    def test_changed_error_text_still_publishes(self):
        st, _ = new_store(self)
        frames = []
        st.publish = lambda ev, payload, rev: frames.append((ev, rev))
        st.put_error("ci", "gh: boom")
        rev = st.rev
        st.put_error("ci", "gh: other")
        self.assertGreater(st.rev, rev)
        self.assertEqual(len(frames), 2)


class _FailingCommit:
    """Stands in for the sqlite3 connection: the body runs, then the commit fails."""

    def __init__(self, real):
        self.real = real

    def __getattr__(self, name):
        return getattr(self.real, name)

    def __enter__(self):
        self.real.__enter__()
        return self.real

    def __exit__(self, *exc):
        self.real.rollback()
        raise sqlite3.OperationalError("commit failed")


class TestPutBlockPublishesAfterCommit(unittest.TestCase):
    def test_failing_commit_publishes_nothing(self):
        st, _ = new_store(self)
        frames = []
        st.publish = lambda ev, payload, rev: frames.append((ev, rev))
        st.con = _FailingCommit(st.con)
        with self.assertRaises(sqlite3.Error):
            st.put_block("host", {"a": 1}, 1)
        self.assertEqual(frames, [], "a frame went out for a write that never committed")

    def test_row_is_committed_when_the_frame_is_published(self):
        st, path = new_store(self)
        seen = []

        def publish(ev, payload, rev):
            con = sqlite3.connect(path)  # another connection sees only what is committed
            try:
                seen.append(con.execute("SELECT rev FROM blocks WHERE name='host'").fetchall())
            finally:
                con.close()

        st.publish = publish
        rev = st.put_block("host", {"a": 1}, 1)
        self.assertEqual(seen, [[(rev,)]], "the frame must be published after COMMIT, not before")


class TestPrunedIdIsNotReusable(unittest.TestCase):
    def test_pruned_id_with_another_body_is_409(self):
        st, _ = new_store(self)
        routes = {"/api/x": RouteSpec(validate=lambda b: b, run=lambda a: (0, "ok"), zones=lambda a: (), deadline_s=5)}
        cs = Commands(st, MagicMock(), routes)
        for i in range(205):
            st.save_command(_cmd("old%05d" % i, state="ok", body=core.canonical({"id": "old%05d" % i, "v": 1})))
        self.assertIsNone(st.get_command("old00000"), "precondition: the oldest terminal command was pruned")
        code, _ = cs.submit("/api/x", {"id": "old00000", "v": 2})
        self.assertEqual(code, 409)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class FakeWatchers:
    def __init__(self):
        self.woken = []
        self.woke_event = threading.Event()

    def wake(self, name):
        self.woken.append(name)
        self.woke_event.set()


def _make_commands(case, deadline_s=-1, rc=0, zones=("z",), wakes=(), blocking=True, start_time=1000.0):
    sig = inspect.signature(cmds.Commands.__init__)
    case.assertIn("clock", sig.parameters, "Commands.__init__ must accept clock=time.time (issue #33)")
    st, _ = new_store(case)
    clock = FakeClock(start_time)
    watchers = FakeWatchers()
    started = threading.Event()
    gate = threading.Event()
    if not blocking:
        gate.set()

    def run(_args):
        started.set()
        gate.wait(10)
        return rc, "done"

    spec = cmds.RouteSpec(
        validate=lambda b: b,
        run=run,
        zones=lambda a: zones,
        deadline_s=deadline_s,
        wakes=lambda a: wakes,
    )
    c = cmds.Commands(st, watchers, {"/api/x": spec}, clock=clock)
    case.addCleanup(lambda: watchers.woke_event.wait(5))
    case.addCleanup(gate.set)
    return c, st, gate, started, clock, watchers


class TestDeadlineWaitsForTheAction(unittest.TestCase):
    def test_sweep_skips_a_command_whose_action_is_still_running(self):
        c, st, gate, started, clock, watchers = _make_commands(self, deadline_s=5)
        code, _ = c.submit("/api/x", {"id": "cmd-0001"})
        self.assertEqual(code, 202)
        self.assertTrue(started.wait(5), "command worker did not start")
        clock.advance(100)
        c.sweep()
        self.assertEqual(st.get_command("cmd-0001")["state"], "running")
        gate.set()
        self.assertTrue(watchers.woke_event.wait(5))

    def test_finish_extends_the_deadline_to_a_full_window(self):
        c, st, gate, started, clock, watchers = _make_commands(self, deadline_s=5)
        c.submit("/api/x", {"id": "cmd-0001"})
        self.assertTrue(started.wait(5), "command worker did not start")
        t_finish = clock.advance(20)
        gate.set()
        self.assertTrue(watchers.woke_event.wait(5), "command did not finish")
        cmd = st.get_command("cmd-0001")
        self.assertIsNotNone(cmd["finished_seq"])
        self.assertEqual(cmd["deadline_at"], t_finish + cmds.CONFIRM_S)

    def test_finished_command_without_a_pass_still_fails_after_the_extension(self):
        c, st, gate, started, clock, watchers = _make_commands(self, deadline_s=5)
        c.submit("/api/x", {"id": "cmd-0001"})
        self.assertTrue(started.wait(5), "command worker did not start")
        t_finish = clock.advance(10)
        gate.set()
        self.assertTrue(watchers.woke_event.wait(5), "command did not finish")
        self.assertNotIn("cmd-0001", getattr(c, "inflight", set()))

        clock.now = t_finish + cmds.CONFIRM_S  # boundary: now <= deadline_at
        c.sweep()
        self.assertEqual(st.get_command("cmd-0001")["state"], "running")

        t_expired = clock.advance(1)
        c.sweep()
        cmd = st.get_command("cmd-0001")
        self.assertEqual(cmd["state"], "failed")
        self.assertEqual(cmd["done_at"], t_expired)
        self.assertIn("not confirmed", cmd["message"])

    def test_clock_drives_submit_finish_on_pass_and_default_clock(self):
        c, st, gate, started, clock, watchers = _make_commands(self, deadline_s=10, start_time=500.0)
        c.submit("/api/x", {"id": "cmd-pass"})
        created = st.get_command("cmd-pass")
        self.assertEqual(created["created_at"], 500.0)
        self.assertEqual(created["deadline_at"], 510.0)
        self.assertTrue(started.wait(5))
        clock.advance(2)
        gate.set()
        self.assertTrue(watchers.woke_event.wait(5))
        finished = st.get_command("cmd-pass")
        t_pass = clock.advance(5)
        c.on_pass("z", finished["finished_seq"] + 1)
        confirmed = st.get_command("cmd-pass")
        self.assertEqual(confirmed["state"], "ok")
        self.assertEqual(confirmed["done_at"], t_pass)

        c2, st2, gate2, started2, clock2, watchers2 = _make_commands(
            self, deadline_s=10, zones=(), wakes=("done",), start_time=700.0
        )
        c2.submit("/api/x", {"id": "cmd-nozone"})
        self.assertTrue(started2.wait(5))
        t_done = clock2.advance(3)
        gate2.set()
        self.assertTrue(watchers2.woke_event.wait(5))
        done_cmd = st2.get_command("cmd-nozone")
        self.assertEqual(done_cmd["state"], "ok")
        self.assertEqual(done_cmd["done_at"], t_done)

        c_default = cmds.Commands(st, FakeWatchers(), {})
        self.assertTrue(callable(c_default.clock))


class TestWatcherSurvivesPutError(unittest.TestCase):
    def _require_step(self):
        self.assertTrue(hasattr(watch.Watchers, "step"), "Watchers must expose step(spec) (issue #33)")

    def test_step_survives_failing_put_error_and_returns_capped_retry_wait(self):
        self._require_step()

        class Bad:
            def put_block(self, *a):
                raise OSError("disk full")

            def put_error(self, *a):
                raise OSError("disk full")

        def read(store):
            raise RuntimeError("boom")

        w = watch.Watchers(Bad(), lambda n, s: None)
        short_spec = watch.BlockSpec(name="b", period=0.05, read=read)
        long_spec = watch.BlockSpec(name="c", period=60, read=read)
        self.assertEqual(w.step(short_spec), 0.05)
        self.assertEqual(w.step(long_spec), 15)

    def test_step_runs_pass_calls_on_pass_and_wakes_dependents(self):
        self._require_step()
        calls = []

        class RecordingStore:
            def __init__(self, rev_to_return):
                self.rev_to_return = rev_to_return
                self.errors = []

            def put_block(self, name, data, seq, period):
                calls.append(("put_block", name, data, seq, period))
                return self.rev_to_return

            def put_error(self, name, err):
                self.errors.append((name, err))

        passes = []
        store = RecordingStore(rev_to_return=7)
        w = watch.Watchers(store, lambda name, s: passes.append((name, s)))
        src = watch.BlockSpec(name="host", period=12, read=lambda st: {"cpu": 1})
        dep = watch.BlockSpec(name="slots", period=10, read=lambda st: {}, after=("host",))
        other = watch.BlockSpec(name="ci_gate", period=60, read=lambda st: {}, after=("autoreview",))
        for sp in (src, dep, other):
            w.specs[sp.name] = sp
            w.events[sp.name] = threading.Event()

        wait = w.step(src)
        self.assertEqual(wait, 12)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ("put_block", "host", {"cpu": 1}))
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0], ("host", calls[0][3]))
        self.assertTrue(w.events["slots"].is_set(), "dependent block must be woken when rev is not None")
        self.assertFalse(w.events["ci_gate"].is_set(), "unrelated block must not be woken")

        w.events["slots"].clear()
        store.rev_to_return = None
        wait2 = w.step(src)
        self.assertEqual(wait2, 12)
        self.assertEqual(len(passes), 2)
        self.assertFalse(w.events["slots"].is_set())

        bad_spec = watch.BlockSpec(name="ci_gate", period=60, read=lambda st: (_ for _ in ()).throw(RuntimeError("boom")))
        wait3 = w.step(bad_spec)
        self.assertEqual(wait3, 15)
        self.assertEqual(store.errors, [("ci_gate", "RuntimeError: boom")])
        self.assertEqual(len(passes), 2)

    def test_loop_delegates_to_step(self):
        self._require_step()

        class _StopLoop(BaseException):
            pass

        w = watch.Watchers(object(), lambda n, s: None)
        spec = watch.BlockSpec(name="b", period=0.0, read=lambda st: 1)
        w.specs["b"] = spec
        w.events["b"] = threading.Event()
        seen = []

        def fake_step(sp):
            seen.append(sp)
            if len(seen) >= 2:
                raise _StopLoop()
            return 0.0

        w.step = fake_step
        with self.assertRaises(_StopLoop):
            w._loop(spec)
        self.assertEqual(seen, [spec, spec])


if __name__ == "__main__":
    unittest.main()
