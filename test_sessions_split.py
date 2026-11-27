#!/usr/bin/env python3
"""Tests for the sessions.py split, target resolution, and informational held_slots (host_repo#278, #34)."""

import builtins
import contextlib
import dis
import importlib
import inspect
import json
import os
import pathlib
import re
import sqlite3
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

import agy_probe  # noqa: E402
import claude_probe  # noqa: E402
import claude_send  # noqa: E402
import sessions  # noqa: E402

CLAUDE_ROW = {"id": "local_016199fc-aaaa", "ref": "local_016199fc-aaaa", "harness": "claude",
              "title": "My Claude Title"}
CLAUDE_ROW2 = {"id": "local_016199fc-bbbb", "ref": "local_016199fc-bbbb", "harness": "claude",
               "title": "Other"}
AGY_ROW = {"id": "e2dc1234-cid", "ref": "local_e2dc1234-cid", "raw_ref": "e2dc1234-cid",
           "harness": "antigravity", "title": "Agy Title"}
ROWS = [CLAUDE_ROW, CLAUDE_ROW2, AGY_ROW]


class SplitModules(unittest.TestCase):
    def test_modules_exist(self):
        for name in ("claude_probe", "claude_send", "agy_probe", "agy_send"):
            importlib.import_module(name)

    def test_messaging_text_moved_out_of_sessions(self):
        src = (BIN_DIR / "sessions.py").read_text(encoding="utf-8")
        self.assertNotIn("Popup", src)
        self.assertNotIn("session named exactly", src)

    def test_send_message_signature_kept(self):
        params = list(inspect.signature(sessions.send_message).parameters.values())
        self.assertEqual([p.name for p in params], ["session_ref", "message", "harness"])
        self.assertIsNone(params[2].default)

    def test_discover_sessions_liveness_keyword_defaults_false(self):
        param = inspect.signature(sessions.discover_sessions).parameters["liveness"]
        self.assertIs(param.default, False)

    def test_deliver_exists(self):
        self.assertTrue(callable(sessions.deliver))


class ResolveTarget(unittest.TestCase):
    def test_claude_exact_id_with_and_without_local_prefix(self):
        self.assertEqual(sessions.resolve_target(ROWS, "claude", "local_016199fc-aaaa"), (CLAUDE_ROW, None))
        self.assertEqual(sessions.resolve_target(ROWS, "claude", "016199fc-aaaa"), (CLAUDE_ROW, None))

    def test_antigravity_strips_local_prefix(self):
        self.assertEqual(sessions.resolve_target(ROWS, "antigravity", "e2dc1234-cid"), (AGY_ROW, None))
        self.assertEqual(sessions.resolve_target(ROWS, "antigravity", "local_e2dc1234-cid"), (AGY_ROW, None))

    def test_title_does_not_match(self):
        self.assertEqual(sessions.resolve_target(ROWS, "claude", "My Claude Title"),
                         (None, "no claude session My Claude Title"))

    def test_shared_six_char_prefix_does_not_match(self):
        self.assertEqual(sessions.resolve_target(ROWS, "claude", "016199"),
                         (None, "no claude session 016199"))
        self.assertEqual(sessions.resolve_target(ROWS, "antigravity", "e2dc12"),
                         (None, "no antigravity session e2dc12"))

    def test_match_stays_inside_harness(self):
        self.assertEqual(sessions.resolve_target(ROWS, "claude", "e2dc1234-cid"),
                         (None, "no claude session e2dc1234-cid"))
        self.assertEqual(sessions.resolve_target(ROWS, "antigravity", "local_016199fc-aaaa"),
                         (None, "no antigravity session local_016199fc-aaaa"))


class AntigravityFirstRowWithoutViewTime(unittest.TestCase):
    """last_view_sec was only set when the annotation had last_user_view_time -> NameError."""

    def test_no_name_error(self):
        # The probe lives in agy_probe after the split; before it, in sessions.
        try:
            mod = importlib.import_module("agy_probe")
        except ImportError:
            mod = sessions
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            (base / "annotations").mkdir()
            (base / "presence").mkdir()
            db = base / "conversation_summaries.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, "
                         "workspace_uris TEXT, last_modified_time TEXT, killed INTEGER, status TEXT)")
            conn.execute("INSERT INTO conversation_summaries VALUES "
                         "('cid1', 'A title', NULL, '2026-01-02 03:04:05', 0, 'ok')")
            conn.commit()
            conn.close()
            (base / "annotations" / "cid1.pbtxt").write_text('title: "A title"\n', encoding="utf-8")
            with patch.object(mod, "AGY_BASE", base), patch.object(mod, "AGY_DB", db), \
                    patch.object(mod, "AGY_ANNOTATIONS", base / "annotations"), \
                    patch.object(mod, "AGY_PRESENCE", base / "presence"):
                rows = mod._probe_antigravity()
        self.assertEqual([r["id"] for r in rows], ["cid1"])
        self.assertGreater(rows[0]["last_active"], 0)
        self.assertEqual(rows[0]["held_slots"], 0)
        self.assertIs(rows[0]["is_over_limit"], False)
        self.assertIsNone(rows[0]["limit"])


class TestSessionsPerSessionLimitsAndTree(unittest.TestCase):
    """Issue #49: per-session limit, 0 pause preservation, is_over_limit, and tree_over_limit."""

    def _discover(self, *, agy_rows=(), claude_rows=(), dropin_rows=(), leases=(), limits=None):
        with patch.object(sessions, "_probe_dropin_harnesses", return_value=([dict(r) for r in dropin_rows], {})), \
                patch.object(sessions.agy_probe, "probe", return_value=[dict(r) for r in agy_rows]), \
                patch.object(sessions.claude_probe, "probe", return_value=[dict(r) for r in claude_rows]):
            return sessions.discover_sessions(leases=[dict(l) for l in leases], limits=limits)

    def test_per_session_limits_and_zero_pause_preserved(self):
        agy_cid = "e2dc1234-aaaa-bbbb"
        agy_row = {
            "id": agy_cid, "ref": f"local_{agy_cid}", "raw_ref": agy_cid, "harness": "antigravity",
            "title": "Meta Session", "is_active": True, "last_active": 1000, "held_slots": 0,
        }
        paused_id = "local_016199fc-5555"
        paused_row = {
            "id": paused_id, "ref": paused_id, "raw_ref": "016199fc-5555", "harness": "claude",
            "title": "Paused Worker", "is_active": True, "last_active": 1000, "held_slots": 0,
        }
        exact_id = "local_016199fc-6666"
        exact_row = {
            "id": exact_id, "ref": exact_id, "raw_ref": "016199fc-6666", "harness": "claude",
            "title": "Exact Worker", "is_active": True, "last_active": 1000, "held_slots": 0,
        }
        leases = [
            {"project": "acme-org-slot-1", "holder": "issue-1", "owner": f"meta [{agy_cid}]", "_slot_id": "acme-org-slot-1"},
            {"project": "acme-org-slot-2", "holder": "issue-2", "owner": f"meta [{agy_cid}]", "_slot_id": "acme-org-slot-2"},
            {"project": "host_repo-slot-1", "holder": "issue-3", "owner": f"w [{exact_id}]", "_slot_id": "host_repo-slot-1"},
        ]
        limits = {
            "default": 2,
            "sessions": {
                f"local_{agy_cid}": 1,   # toggled spelling matches agy_cid -> 2 > 1 -> over
                "016199fc-5555": 0,       # toggled spelling with 0 pause -> preserved as 0, 0 > 0 is False
                exact_id: 1,             # exact match -> 1 == 1 -> not over
            },
        }
        res = self._discover(agy_rows=[agy_row], claude_rows=[paused_row, exact_row], leases=leases, limits=limits)
        by_id = {r["id"]: r for r in res}
        self.assertEqual((by_id[agy_cid]["held_slots"], by_id[agy_cid]["limit"], by_id[agy_cid]["is_over_limit"]), (2, 1, True))
        self.assertEqual((by_id[paused_id]["held_slots"], by_id[paused_id]["limit"], by_id[paused_id]["is_over_limit"]), (0, 0, False))
        self.assertEqual((by_id[exact_id]["held_slots"], by_id[exact_id]["limit"], by_id[exact_id]["is_over_limit"]), (1, 1, False))

    def test_parent_child_grandchild_tree_sets_tree_over_limit(self):
        root_row = {
            "id": "root-1", "ref": "local_root-1", "raw_ref": "root-1", "harness": "claude",
            "title": "Root", "is_active": True, "last_active": 2000, "held_slots": 0,
        }
        child_row = {
            "id": "child-1", "ref": "local_child-1", "raw_ref": "child-1", "harness": "claude",
            "title": "Child", "parent_id": "root-1", "is_active": True, "last_active": 1900, "held_slots": 0,
        }
        grandchild_row = {
            "id": "grand-1", "ref": "local_grand-1", "raw_ref": "grand-1", "harness": "claude",
            "title": "Grandchild", "parent_id": "child-1", "is_active": True, "last_active": 1800, "held_slots": 0,
        }
        idle_root = {
            "id": "idle-0", "ref": "local_idle-0", "raw_ref": "idle-0", "harness": "antigravity",
            "title": "Zero Slots", "is_active": True, "last_active": 1700, "held_slots": 0,
        }
        leases = [
            {"project": "acme-org-slot-1", "owner": "r [root-1]", "_slot_id": "acme-org-slot-1"},
            {"project": "host_repo-slot-1", "owner": "r [root-1]", "_slot_id": "host_repo-slot-1"},
            {"project": "acme-org-slot-2", "owner": "c [child-1]", "_slot_id": "acme-org-slot-2"},
            {"project": "demo-app-slot-1", "owner": "c [child-1]", "_slot_id": "demo-app-slot-1"},
            {"project": "ops-console-slot-1", "owner": "c [child-1]", "_slot_id": "ops-console-slot-1"},
            {"project": "ops-console-slot-2", "owner": "g [grand-1]", "_slot_id": "ops-console-slot-2"},
        ]
        with tempfile.TemporaryDirectory() as dc_dir:
            (pathlib.Path(dc_dir) / "limits.json").write_text(
                json.dumps({"default": 2, "sessions": {"child-1": 1}}),
                encoding="utf-8",
            )
            with patch.object(sessions, "DEV_CONTAINER_STATE", pathlib.Path(dc_dir)):
                rows = self._discover(
                    claude_rows=[root_row, child_row, grandchild_row],
                    agy_rows=[idle_root],
                    leases=leases,
                    limits=None,
                )

        by_id = {r["id"]: r for r in rows}
        self.assertEqual((by_id["root-1"]["held_slots"], by_id["root-1"]["tree_held"], by_id["root-1"]["tree_over_limit"]), (2, 6, True))
        self.assertEqual((by_id["child-1"]["held_slots"], by_id["child-1"]["is_over_limit"]), (3, True))
        self.assertEqual((by_id["grand-1"]["held_slots"], by_id["grand-1"]["is_over_limit"]), (1, False))
        self.assertEqual((by_id["idle-0"]["held_slots"], by_id["idle-0"]["tree_over_limit"]), (0, False))
        self.assertEqual(by_id["child-1"]["parent_id"], "root-1")
        self.assertEqual(by_id["child-1"]["root_id"], "root-1")
        self.assertEqual(by_id["grand-1"]["root_id"], "root-1")

        # When default covers all held_slots, tree_over_limit is False
        ok_rows = self._discover(
            claude_rows=[root_row, child_row, grandchild_row],
            agy_rows=[idle_root],
            leases=leases,
            limits={"default": 5},
        )
        ok_by_id = {r["id"]: r for r in ok_rows}
        self.assertIs(ok_by_id["root-1"]["tree_over_limit"], False)

    def test_probes_emit_limit_and_is_over_limit_defaults(self):
        for rel in ("bin/agy_probe.py", "bin/claude_probe.py"):
            src = (SW_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn('"limit": None', src, rel)
            self.assertIn('"is_over_limit": False', src, rel)


def _global_loads(code):
    names = set()
    for ins in dis.get_instructions(code):
        if ins.opname == "LOAD_GLOBAL":
            names.add(ins.argval)
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            names |= _global_loads(const)
    return names


class LeasesAndLimits(unittest.TestCase):
    def test_read_all_leases_and_limits_do_not_raise_on_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(sessions, "AGENT_SLOT_STATE", pathlib.Path(d)), \
                 patch.object(sessions, "DEV_CONTAINER_STATE", pathlib.Path(d)):
                self.assertEqual(sessions.read_all_leases(), [])
                self.assertIsInstance(sessions.read_limits(), dict)

    def test_every_global_a_function_reads_is_defined(self):
        src = (BIN_DIR / "sessions.py").read_text(encoding="utf-8")
        code = compile(src, "sessions.py", "exec")
        missing = sorted(n for n in _global_loads(code)
                         if not hasattr(sessions, n) and not hasattr(builtins, n))
        self.assertEqual(missing, [])


class SendMessageResolves(unittest.TestCase):
    def test_bare_title_is_refused_not_forwarded(self):
        with patch.object(sessions, "discover_sessions", return_value=ROWS), \
                patch.object(claude_send, "send", return_value=(True, "ok", None)) as send:
            result = sessions.send_message("My Claude Title", "x", harness="claude")
        send.assert_not_called()
        self.assertEqual(result, (False, "no claude session My Claude Title"))

    def test_unknown_id_is_refused_not_forwarded(self):
        with patch.object(sessions, "discover_sessions", return_value=ROWS), \
                patch.object(claude_send, "send", return_value=(True, "ok", None)) as send:
            result = sessions.send_message("local_nope", "x", harness="claude")
        send.assert_not_called()
        self.assertEqual(result, (False, "no claude session local_nope"))

    def test_resolving_id_is_delivered_to_the_resolved_row(self):
        live_claude = {**CLAUDE_ROW, "live": True}
        with patch.object(sessions, "discover_sessions", return_value=[live_claude, AGY_ROW]), \
                patch.object(claude_send, "send", return_value=(True, "ok", None)) as send:
            result = sessions.send_message("016199fc-aaaa", "x", harness="claude")
        self.assertEqual(result, (True, "ok"))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], live_claude)


class DeliverClaude(unittest.TestCase):
    def _row(self, live):
        return {"id": "local_016199fc-aaaa", "ref": "local_016199fc-aaaa", "harness": "claude",
                "title": "T", "cliSessionId": "cli-1234", "live": live}

    def test_live_uses_relay(self):
        with patch.object(claude_send, "send", return_value=(True, "relayed", None)) as send, \
                patch.object(claude_send, "launch_resume", return_value=(True, "x")) as resume:
            result = sessions.deliver(self._row(True), "hello")
        send.assert_called_once()
        resume.assert_not_called()
        self.assertEqual(result, (True, "relayed", None))

    def test_dead_uses_resume_with_cli_id_and_letter(self):
        with patch.object(claude_send, "send", return_value=(True, "relayed", None)) as send, \
                patch.object(claude_send, "launch_resume", return_value=(True, "resuming")) as resume:
            result = sessions.deliver(self._row(False), "hello")
        send.assert_not_called()
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[0], "cli-1234")
        self.assertEqual(resume.call_args.args[1], "hello")
        self.assertTrue(result[0])

    def test_unknown_refuses_naming_liveness_unknown(self):
        with patch.object(claude_send, "send", return_value=(True, "relayed", None)) as send, \
                patch.object(claude_send, "launch_resume", return_value=(True, "x")) as resume:
            result = sessions.deliver(self._row(None), "hello")
        send.assert_not_called()
        resume.assert_not_called()
        self.assertFalse(result[0])
        self.assertIn("liveness unknown", result[1])


SID = "local_016199fc-aaaa"
POWERSHELL_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
DEFAULT_AGY_BIN = "/mnt/c/Users/user/AppData/Local/Programs/antigravity/resources/bin/language_server.exe"


@contextlib.contextmanager
def _patch_paths(present, managed):
    orig_exists = os.path.exists
    orig_isfile = os.path.isfile
    orig_access = os.access
    orig_path_exists = pathlib.Path.exists
    orig_path_isfile = pathlib.Path.is_file

    def _exists(p):
        s = os.fspath(p)
        return (s in present) if s in managed else orig_exists(p)

    def _isfile(p):
        s = os.fspath(p)
        return (s in present) if s in managed else orig_isfile(p)

    def _access(p, mode):
        s = os.fspath(p)
        return (s in present) if s in managed else orig_access(p, mode)

    def _path_exists(self):
        s = str(self)
        return (s in present) if s in managed else orig_path_exists(self)

    def _path_isfile(self):
        s = str(self)
        return (s in present) if s in managed else orig_path_isfile(self)

    with patch("os.path.exists", side_effect=_exists), \
            patch("os.path.isfile", side_effect=_isfile), \
            patch("os.access", side_effect=_access), \
            patch.object(pathlib.Path, "exists", _path_exists), \
            patch.object(pathlib.Path, "is_file", _path_isfile):
        yield


def _fake_claude_probe(live_value):
    def probe(liveness=False):
        r = {"id": SID, "ref": SID, "harness": "claude", "title": "T", "cliSessionId": "cli-1234",
             "held_slots": 0, "is_active": True, "last_active": 1}
        if liveness:
            r["live"] = live_value
        return [r]
    return probe


def _run_send(live_value):
    with patch.object(claude_probe, "probe", _fake_claude_probe(live_value)), \
            patch.object(sessions.agy_probe, "probe", return_value=[]), \
            patch.object(sessions, "_probe_dropin_harnesses", return_value=([], {})), \
            patch.object(sessions, "read_all_leases", return_value=[]), \
            patch.object(sessions, "read_limits", return_value={}), \
            patch.object(claude_send, "send", return_value=(True, "relayed", None)) as send, \
            patch.object(claude_send, "launch_resume", return_value=(True, "resuming")) as resume:
        result = sessions.send_message(SID, "hi", harness="claude")
    return result, send, resume


class SendLiveness(unittest.TestCase):
    def test_live_relays(self):
        result, send, resume = _run_send(True)
        send.assert_called_once()
        resume.assert_not_called()
        self.assertTrue(result[0])

    def test_dead_resumes(self):
        result, send, resume = _run_send(False)
        send.assert_not_called()
        resume.assert_called_once()
        self.assertTrue(result[0])

    def test_unknown_refuses(self):
        result, send, resume = _run_send(None)
        send.assert_not_called()
        resume.assert_not_called()
        self.assertFalse(result[0])
        self.assertIn("liveness unknown", result[1])


class SendSystemdPathFallback(unittest.TestCase):
    def _claude_row(self):
        return {
            "id": SID,
            "ref": SID,
            "harness": "claude",
            "title": "T",
            "cliSessionId": "cli-1234",
            "held_slots": 0,
            "is_active": True,
            "last_active": 1,
        }

    def _registry_records(self):
        return [{
            "pid": 100,
            "procStart": "500",
            "hostSessionId": SID,
            "sessionId": "cli-1234",
            "status": "busy",
        }]

    def test_claude_send_succeeds_under_systemd_path_via_powershell_fallback(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "powershell.exe":
                raise FileNotFoundError("powershell.exe")
            return subprocess.CompletedProcess(argv, 0, stdout="100 500 claude\n", stderr="")

        with patch.object(claude_probe, "_probe_claude", return_value=[self._claude_row()]), \
                patch.object(claude_probe, "read_registry", return_value=self._registry_records()), \
                patch.object(sessions.agy_probe, "probe", return_value=[]), \
                patch.object(sessions, "_probe_dropin_harnesses", return_value=([], {})), \
                patch.object(sessions, "read_all_leases", return_value=[]), \
                patch.object(sessions, "read_limits", return_value={}), \
                patch("shutil.which", return_value=None), \
                _patch_paths({POWERSHELL_FALLBACK}, {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", side_effect=fake_run) as run, \
                patch.object(claude_send, "send", return_value=(True, "relayed", None)) as send, \
                patch.object(claude_send, "launch_resume", return_value=(True, "resuming")) as resume:
            result = sessions.send_message(SID, "hi", harness="claude")

        self.assertEqual(result, (True, "relayed"))
        send.assert_called_once()
        resume.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], POWERSHELL_FALLBACK)

    def test_claude_send_refuses_when_powershell_missing_everywhere(self):
        completed = subprocess.CompletedProcess([], 0, stdout="100 500 claude\n", stderr="")
        with patch.object(claude_probe, "_probe_claude", return_value=[self._claude_row()]), \
                patch.object(claude_probe, "read_registry", return_value=self._registry_records()), \
                patch.object(sessions.agy_probe, "probe", return_value=[]), \
                patch.object(sessions, "_probe_dropin_harnesses", return_value=([], {})), \
                patch.object(sessions, "read_all_leases", return_value=[]), \
                patch.object(sessions, "read_limits", return_value={}), \
                patch("shutil.which", return_value=None), \
                _patch_paths(set(), {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", return_value=completed) as run, \
                patch.object(claude_send, "send", return_value=(True, "relayed", None)) as send, \
                patch.object(claude_send, "launch_resume", return_value=(True, "resuming")) as resume:
            result = sessions.send_message(SID, "hi", harness="claude")

        run.assert_not_called()
        send.assert_not_called()
        resume.assert_not_called()
        self.assertFalse(result[0])
        self.assertIn("liveness unknown", result[1])


class SendAntigravityWiring(unittest.TestCase):
    def test_send_message_delivers_to_antigravity_via_agentapi(self):
        agy_row = {
            "id": "e2dc1234-cid",
            "ref": "local_e2dc1234-cid",
            "raw_ref": "e2dc1234-cid",
            "harness": "antigravity",
            "title": "Meta",
            "held_slots": 0,
            "is_active": True,
            "last_active": 1,
            "can_message": True,
        }
        env = dict(os.environ)
        env.pop("AGY_AGENTAPI_BIN", None)
        completed = subprocess.CompletedProcess([], 0, stdout="delivered\n", stderr="")
        with patch.dict(os.environ, env, clear=True), \
                patch.object(agy_probe, "probe", return_value=[agy_row]), \
                patch.object(claude_probe, "probe", return_value=[]), \
                patch.object(sessions, "_probe_dropin_harnesses", return_value=([], {})), \
                patch.object(sessions, "read_all_leases", return_value=[]), \
                patch.object(sessions, "read_limits", return_value={}), \
                _patch_paths({DEFAULT_AGY_BIN}, {DEFAULT_AGY_BIN}), \
                patch("subprocess.run", return_value=completed) as run:
            result = sessions.send_message("local_e2dc1234-cid", "ping", harness="antigravity")

        self.assertIs(result[0], True)
        self.assertIsInstance(result[1], str)
        self.assertTrue(result[1].strip())
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [DEFAULT_AGY_BIN, "agentapi", "send-message", "e2dc1234-cid", "ping"],
        )


if __name__ == "__main__":
    unittest.main()
