#!/usr/bin/env python3
"""Tests for Claude liveness, routing, locked resume launch, and resume worker."""

import contextlib
import fcntl
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SW_ROOT = pathlib.Path(__file__).resolve().parent
BIN_DIR = SW_ROOT / "bin"
for p in (str(SW_ROOT), str(BIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import claude_probe  # noqa: E402
import claude_registry  # noqa: E402
import claude_resume_worker as worker  # noqa: E402
import claude_send  # noqa: E402
import sessions  # noqa: E402
import winproc  # noqa: E402
import xmsg_envelope as xe  # noqa: E402


def _rec(pid=100, proc_start="500", host="L1", cli=None, status="busy", domain=None):
    r = {"pid": pid, "procStart": proc_start, "hostSessionId": host,
         "sessionId": cli, "status": status}
    if domain is not None:
        r["pidDomain"] = domain
    return r


def _sess(sid="L1", cli="c1"):
    return {"id": sid, "cliSessionId": cli}


class TestClaudeLiveness(unittest.TestCase):
    def live(self, records, sess_list, proc, **kw):
        return claude_registry.claude_liveness(records, sess_list, proc, **kw)

    def test_01_matching_claude_process_is_alive(self):
        out = self.live([_rec()], [_sess("L1")], {100: (500, "claude")}, tol_ticks=0)
        self.assertIs(out["L1"], True)

    def test_02_idle_status_is_still_alive(self):
        out = self.live([_rec(status="idle")], [_sess("L1")], {100: (500, "claude")}, tol_ticks=0)
        self.assertIs(out["L1"], True)

    def test_03_no_record_is_dead(self):
        out = self.live([_rec()], [_sess("L1"), _sess("L2", "c2")], {100: (500, "claude")}, tol_ticks=0)
        self.assertIs(out["L2"], False)

    def test_04_pid_not_running_is_dead(self):
        out = self.live([_rec()], [_sess("L1")], {}, tol_ticks=0)
        self.assertIs(out["L1"], False)

    def test_05_reused_pid_other_image_is_dead(self):
        out = self.live([_rec()], [_sess("L1")], {100: (999, "notepad")}, tol_ticks=0)
        self.assertIs(out["L1"], False)

    def test_06_claude_image_start_mismatch_is_unknown(self):
        out = self.live([_rec()], [_sess("L1")], {100: (999, "claude")}, tol_ticks=0)
        self.assertIsNone(out["L1"])

    def test_07_no_process_table_is_unknown_but_no_record_is_dead(self):
        out = self.live([_rec()], [_sess("L1"), _sess("L2", "c2")], None, tol_ticks=0)
        self.assertIsNone(out["L1"])
        self.assertIs(out["L2"], False)

    def test_08_no_registry_is_all_unknown(self):
        out = self.live(None, [_sess("L1"), _sess("L2", "c2")], {100: (500, "claude")}, tol_ticks=0)
        self.assertEqual(out, {"L1": None, "L2": None})

    def test_09_match_by_cli_session_id(self):
        r = _rec(host=None, cli="cli-3")
        del r["hostSessionId"]
        out = self.live([r], [_sess("L3", "cli-3")], {100: (500, "claude")}, tol_ticks=0)
        self.assertIs(out["L3"], True)

    def test_10_stale_and_alive_records_combine_to_alive(self):
        records = [_rec(pid=100, proc_start="500"), _rec(pid=200, proc_start="700")]
        proc = {200: (700, "claude")}
        out = self.live(records, [_sess("L1")], proc, tol_ticks=0)
        self.assertIs(out["L1"], True)

    def test_11_unparsable_proc_start_claude_image_is_unknown(self):
        out = self.live([_rec(proc_start="abc")], [_sess("L1")], {100: (500, "claude")}, tol_ticks=0)
        self.assertIsNone(out["L1"])

    def test_12_non_windows_pid_domain_is_unknown(self):
        out = self.live([_rec(domain="linux:x")], [_sess("L1")], {100: (500, "claude")}, tol_ticks=0)
        self.assertIsNone(out["L1"])

    def test_13_start_within_tolerance_is_alive(self):
        out = self.live([_rec()], [_sess("L1")], {100: (505, "claude")}, tol_ticks=10)
        self.assertIs(out["L1"], True)


class TestReadRegistry(unittest.TestCase):
    def test_reads_only_digit_named_json(self):
        with tempfile.TemporaryDirectory() as d:
            dp = pathlib.Path(d)
            (dp / "123.json").write_text(json.dumps({"pid": 123, "hostSessionId": "L1"}))
            (dp / "456.json").write_text(json.dumps({"pid": 456}))
            (dp / "789.abcdef.key").write_text("secret")
            (dp / "notes.json").write_text(json.dumps({"pid": 1}))
            (dp / "999.json").write_text("{not json")
            out = claude_probe.read_registry(d)
        self.assertIsInstance(out, list)
        self.assertEqual(sorted(r["pid"] for r in out), [123, 456])

    def test_unreadable_directory_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            missing = str(pathlib.Path(d) / "nope")
        self.assertIsNone(claude_probe.read_registry(missing))


class TestProbeWiring(unittest.TestCase):
    ROWS = [{"id": "L1", "cliSessionId": "c1", "harness": "claude"},
            {"id": "L2", "cliSessionId": "c2", "harness": "claude"}]

    def _run(self, liveness):
        records = [_rec(pid=100, proc_start="500", host="L1")]
        with patch.object(claude_probe, "_probe_claude",
                          return_value=[dict(r) for r in self.ROWS]), \
             patch.object(claude_probe, "read_registry", return_value=records), \
             patch.object(winproc, "query_windows_processes",
                          return_value={100: (500, "claude")}):
            return claude_probe.probe(liveness=liveness)

    def test_liveness_true_sets_live_on_every_row(self):
        rows = self._run(True)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("live", row)
        by_id = {r["id"]: r for r in rows}
        self.assertIs(by_id["L1"]["live"], True)
        self.assertIs(by_id["L2"]["live"], False)

    def test_liveness_false_leaves_no_live_key(self):
        for row in self._run(False):
            self.assertNotIn("live", row)


class TestRoute(unittest.TestCase):
    def test_live_relays(self):
        self.assertEqual(claude_send.claude_route(True), "relay")

    def test_dead_resumes(self):
        self.assertEqual(claude_send.claude_route(False), "resume")

    def test_unknown_refuses(self):
        self.assertEqual(claude_send.claude_route(None), "refuse")


class TestResumeArgv(unittest.TestCase):
    def test_exact_argv(self):
        self.assertEqual(
            claude_send.resume_argv("/bin/claude", "cli-1234"),
            ["/bin/claude", "-p", "--resume", "cli-1234", "--model", "haiku", "--tools", ""],
        )


class _FakeProc:
    def __init__(self, out="worker reply"):
        self.out = out
        self.returncode = 0
        self.waited = False
        self.communicated = False
        self.stdin = None

    def communicate(self, input=None, timeout=None):
        self.communicated = True
        return (self.out, "")

    def wait(self, timeout=None):
        self.waited = True
        return 0


class _LaunchRig:
    def __init__(self, tmp, live=(False, False)):
        self.lock_dir = pathlib.Path(tmp) / "locks"
        self.events = []
        self.popens = []
        self.proc = _FakeProc()
        self._live = list(live)

    def popen(self, argv, **kw):
        self.events.append("popen")
        self.popens.append((argv, kw))
        return self.proc

    def flock(self, fd, op):
        self.events.append("flock")
        self.flock_op = op
        self.flock_fd = fd

    def liveness(self):
        self.events.append("liveness")
        return self._live.pop(0) if len(self._live) > 1 else self._live[0]

    def run(self, **extra):
        return claude_send.launch_resume(
            "cli-1", "the letter", claude_exe="/bin/claude", lock_dir=self.lock_dir,
            popen=self.popen, flock=self.flock, liveness=self.liveness, **extra,
        )


class TestLaunchResume(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.rig = _LaunchRig(self._t.name)

    def test_lock_file_is_per_session_and_nonblocking_exclusive(self):
        self.rig.run()
        self.assertTrue((self.rig.lock_dir / "cli-1.lock").exists())
        self.assertEqual(self.rig.flock_op, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_liveness_rechecked_after_lock(self):
        self.rig.run()
        ev = self.rig.events
        self.assertIn("flock", ev)
        self.assertIn("liveness", ev[ev.index("flock"):])

    def test_became_live_aborts_without_worker(self):
        rig = _LaunchRig(self._t.name, live=(True,))
        rig.run()
        self.assertEqual(rig.popens, [])

    def test_worker_gets_lock_fd_and_new_session(self):
        self.rig.run()
        self.assertEqual(len(self.rig.popens), 1)
        kw = self.rig.popens[0][1]
        self.assertIn(self.rig.flock_fd, kw["pass_fds"])
        self.assertIs(kw["start_new_session"], True)

    def test_sender_returns_without_waiting(self):
        ok, detail = self.rig.run()
        self.assertTrue(ok)
        self.assertIsInstance(detail, str)
        self.assertFalse(self.rig.proc.waited)
        self.assertFalse(self.rig.proc.communicated and self.rig.proc.out in detail)

    def test_second_sender_refused_when_lock_held(self):
        def busy(fd, op):
            raise BlockingIOError()
        ok, detail = claude_send.launch_resume(
            "cli-1", "x", claude_exe="/bin/claude", lock_dir=self.rig.lock_dir,
            popen=self.rig.popen, flock=busy, liveness=self.rig.liveness,
        )
        self.assertFalse(ok)
        self.assertIn("already resuming", detail)
        self.assertEqual(self.rig.popens, [])

    def test_wait_returns_worker_reply(self):
        ok, detail = self.rig.run(wait=True)
        self.assertTrue(ok)
        self.assertIn("worker reply", detail)


class _WorkerRig:
    def __init__(self, out="answer text\n"):
        self.out = out
        self.runs = []
        self.sent = []

    def run(self, argv, input_text):
        self.runs.append((argv, input_text))
        return self.out

    def send(self, to, text):
        self.sent.append((to, text))

    def handle(self, letter):
        return worker.handle(letter, "cli-9", claude_exe="/bin/claude", run=self.run, send=self.send)


LETTER = xe.build_envelope("antigravity:conv-1", True, 1, "please answer")


class TestWorker(unittest.TestCase):
    def test_runs_resume_argv_with_letter_on_stdin(self):
        rig = _WorkerRig()
        rig.handle(LETTER)
        self.assertEqual(len(rig.runs), 1)
        argv, stdin = rig.runs[0]
        self.assertEqual(argv, claude_send.resume_argv("/bin/claude", "cli-9"))
        self.assertIn("please answer", stdin)

    def test_captured_stdout_goes_back_to_sender_as_new_letter(self):
        rig = _WorkerRig()
        self.assertTrue(rig.handle(LETTER))
        self.assertEqual(len(rig.sent), 1)
        to, text = rig.sent[0]
        self.assertEqual(to, "antigravity:conv-1")
        parsed = xe.parse_envelope(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["body"].strip(), "answer text")
        self.assertFalse(parsed["reply"])
        self.assertEqual(parsed["hops"], 2)

    def test_letter_at_hop_limit_is_not_answered(self):
        rig = _WorkerRig()
        letter = xe.build_envelope("antigravity:conv-1", True, xe.MAX_HOPS, "late")
        self.assertFalse(rig.handle(letter))
        self.assertEqual(rig.sent, [])
        self.assertEqual(rig.runs, [])

    def test_empty_stdout_sends_nothing(self):
        rig = _WorkerRig(out="  \n")
        self.assertFalse(rig.handle(LETTER))
        self.assertEqual(rig.sent, [])


WORKER_ROW = {"id": "local_abc", "ref": "local_abc", "raw_ref": "local_abc", "harness": "claude",
              "title": "T", "cliSessionId": "cli-1", "held_slots": 0, "is_active": True, "last_active": 1}


class TestWorkerSendLiveness(unittest.TestCase):
    def test_send_relays_to_a_live_claude_row(self):
        def probe_live(liveness=False):
            r = dict(WORKER_ROW)
            if liveness:
                r["live"] = True
            return [r]

        calls = []
        with patch.object(claude_probe, "probe", probe_live), \
                patch.object(sessions, "read_all_leases", lambda: []), \
                patch.object(sessions, "_probe_dropin_harnesses", lambda: ([], {})), \
                patch.object(sessions.agy_probe, "probe", lambda liveness=False: []), \
                patch.object(claude_send, "send", lambda *a, **k: calls.append(a) or (True, "ok", None)):
            worker._send("claude:local_abc", "hello")
        self.assertEqual(len(calls), 1, "a live row must reach claude_send.send, not be refused")


class TestReplyAddress(unittest.TestCase):
    def test_sender_address_resolves_to_the_sender_row(self):
        sent = []
        worker.handle(LETTER, "cli-1", claude_exe="/bin/claude", local_id="local_abc",
                      run=lambda argv, text: "answer",
                      send=lambda to, t: sent.append((to, t)))
        parsed = xe.parse_envelope(sent[0][1])
        sess, err = sessions.resolve_target([dict(WORKER_ROW)], parsed["from_harness"], parsed["from_id"])
        self.assertIsNotNone(sess, err)
        self.assertEqual(sess["id"], "local_abc")


class TestWorkerMain(unittest.TestCase):
    def test_main_prints_the_reply_and_takes_local_id_from_argv(self):
        sent, out = [], io.StringIO()
        worker.main(["/bin/claude", "cli-1", "local_abc"], LETTER, run=lambda a, t: "answer text\n",
                    send=lambda to, t: sent.append((to, t)), out=out)
        self.assertIn("answer text", out.getvalue())
        self.assertEqual(xe.parse_envelope(sent[0][1])["from_id"], "local_abc")


STUB_REPLY = 'import sys; sys.stdin.read(); print("the reply text")\n'
STUB_SLEEP = 'import sys, time; sys.stdin.read(); time.sleep(30)\n'


class TestWaitTrue(unittest.TestCase):
    def launch(self, stub_src, **kw):
        d = tempfile.mkdtemp()
        stub = pathlib.Path(d) / "stub_worker.py"
        stub.write_text(stub_src)
        procs = []

        def popen(*a, **k):
            procs.append(subprocess.Popen(*a, **k))
            return procs[-1]
        try:
            res = claude_send.launch_resume(
                "cli-1", "letter", claude_exe="/bin/claude", lock_dir=pathlib.Path(d) / "locks",
                popen=popen, flock=fcntl.flock, liveness=lambda: False, wait=True,
                worker_path=str(stub), **kw)
        except Exception as e:
            self.fail(f"launch_resume raised {type(e).__name__}: {e}")
        return res, procs

    def test_wait_true_returns_the_reply_text(self):
        (ok, detail), _ = self.launch(STUB_REPLY)
        self.assertTrue(ok)
        self.assertIn("the reply text", detail)

    def test_timeout_returns_false_and_kills_the_child(self):
        (ok, detail), procs = self.launch(STUB_SLEEP, timeout=1)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)
        self.assertIsNotNone(procs[0].poll(), "child still running after timeout")


def _surfaced(fn):
    err = io.StringIO()
    failed = False
    with contextlib.redirect_stderr(err):
        try:
            fn()
        except SystemExit as e:
            failed = e.code not in (0, None)
        except Exception as e:
            failed = True
            err.write(str(e))
    return failed, err.getvalue()


class TestWorkerFailures(unittest.TestCase):
    def test_deliver_failure_is_surfaced(self):
        sess = {"harness": "claude", "ref": "local_x"}
        with patch.object(sessions, "discover_sessions", return_value=[]), \
             patch.object(sessions, "resolve_target", return_value=(sess, "")), \
             patch.object(sessions, "deliver", return_value=(False, "boom", None)):
            _failed, err = _surfaced(lambda: worker._send("claude:local_x", "t"))
        self.assertIn("boom", err)

    def test_unresolved_target_is_surfaced(self):
        with patch.object(sessions, "discover_sessions", return_value=[]), \
             patch.object(sessions, "resolve_target", return_value=(None, "no claude session")):
            _failed, err = _surfaced(lambda: worker._send("claude:local_x", "t"))
        self.assertIn("no claude session", err)

    def _main(self, exc):
        def run(argv, text):
            raise exc
        letter = xe.build_envelope("claude:sender", True, 0, "hello")
        return _surfaced(lambda: worker.main(["claude", "cli1"], letter, run=run,
                                             send=lambda to, t: None, out=io.StringIO()))

    def test_timeout_exits_nonzero_with_reason(self):
        failed, err = self._main(subprocess.TimeoutExpired(["claude"], 600))
        self.assertTrue(failed)
        self.assertIn("claude_resume_worker:", err)
        self.assertIn("600", err)

    def test_missing_exe_exits_nonzero_with_reason(self):
        failed, err = self._main(FileNotFoundError(2, "No such file", "claude"))
        self.assertTrue(failed)
        self.assertIn("claude_resume_worker:", err)
        self.assertIn("claude", err)


class TestWorkerLog(unittest.TestCase):
    SID = "sess-1"
    STUB = "import sys\nsys.stdin.read()\nsys.stderr.write('boom\\n')\nsys.exit(1)\n"

    def _launch(self, tmp, wait):
        stub = pathlib.Path(tmp) / "stub.py"
        stub.write_text(self.STUB)
        lock_dir = pathlib.Path(tmp) / "locks"
        procs = []

        def popen(*args, **kwargs):
            proc = subprocess.Popen(*args, **kwargs)
            procs.append(proc)
            self.addCleanup(lambda p=proc: p.wait(timeout=5) if p.poll() is None else p.wait())
            return proc

        res = claude_send.launch_resume(
            self.SID, "hi", claude_exe="claude", lock_dir=lock_dir,
            popen=popen, flock=fcntl.flock, liveness=lambda: False,
            wait=wait, worker_path=str(stub),
        )
        return res, lock_dir / f"{self.SID}.log", procs

    def test_nonwait_stderr_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            (ok, detail), log, procs = self._launch(tmp, False)
            self.assertTrue(ok)
            self.assertTrue(detail.startswith("resuming"))
            self.assertEqual(len(procs), 1)
            procs[0].wait(timeout=5)
            self.assertTrue(log.exists(), "worker log file was not created")
            self.assertIn("boom", log.read_text(), "worker stderr not in log")
            self.assertIn(str(log), detail)

    def test_wait_returns_reason_and_logs_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            (ok, detail), log, procs = self._launch(tmp, True)
            self.assertFalse(ok)
            self.assertIn("boom", detail)
            self.assertEqual(len(procs), 1)
            procs[0].wait(timeout=5)
            self.assertTrue(log.exists(), "worker log file was not created")
            self.assertIn("boom", log.read_text(), "reason not in log")

    def test_log_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "locks" / f"{self.SID}.log"
            log.parent.mkdir(parents=True)
            log.write_text("x" * (200 * 1024))
            self._launch(tmp, True)
            self.assertLess(log.stat().st_size, 80 * 1024)


if __name__ == "__main__":
    unittest.main()
