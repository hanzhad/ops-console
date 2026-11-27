#!/usr/bin/env python3
"""ops-console#18, #31, #33, #34: limits pure core, flock-serialized limits_store, CLI wrappers, and one state dir."""

import copy
import fcntl
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
for p in (str(ROOT), str(BIN)):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine  # noqa: E402
import sessions  # noqa: E402

UUID = "ed951028-1268-42f7-bd1c-7860e2431921"


def _require_limits_core(case):
    try:
        mod = importlib.import_module("sw.limits_core")
    except ImportError as e:
        case.fail(f"sw/limits_core.py is missing (issue #33): {e}")
    for fn in ("parse_limits", "parse_limit_op", "apply_machine_max", "apply_session_limit"):
        case.assertTrue(hasattr(mod, fn), f"sw/limits_core.py lacks {fn} (issue #49)")
    for dead in ("_PROJECT_RE", "validate_project_name", "apply_project_limit"):
        case.assertFalse(hasattr(mod, dead), f"sw/limits_core.py must remove {dead} (issue #49)")
    return mod


def _require_limits_store(case):
    try:
        mod = importlib.import_module("limits_store")
    except ImportError as e:
        case.fail(f"bin/limits_store.py is missing (issue #33): {e}")
    case.assertTrue(hasattr(mod, "update_limits"), "bin/limits_store.py lacks update_limits (issue #33)")
    return mod


def read(p):
    return json.loads(pathlib.Path(p).read_text())


def run_setcap(case, op, home):
    script = BIN / "setcap.py"
    case.assertTrue(script.is_file(), "bin/setcap.py is missing")
    env = {**os.environ, "DEV_CONTAINER_HOME": str(home)}
    r = subprocess.run([sys.executable, str(script), str(op)], env=env, capture_output=True, text=True)
    case.assertEqual(r.returncode, 0, f"setcap.py {op!r} failed: {r.stderr}")
    return int(r.stdout.strip())


def run_setlimit(case, ref, op, home, expect_ok=True):
    script = BIN / "setlimit.py"
    case.assertTrue(script.is_file(), "bin/setlimit.py is missing")
    env = {**os.environ, "DEV_CONTAINER_HOME": str(home)}
    r = subprocess.run([sys.executable, str(script), ref, str(op)], env=env, capture_output=True, text=True)
    if expect_ok:
        case.assertEqual(r.returncode, 0, f"setlimit.py {ref!r} {op!r} failed: {r.stderr}")
        return int(r.stdout.strip())
    case.assertEqual(r.returncode, 2, f"setlimit.py {ref!r} {op!r} expected exit 2, got {r.returncode}: {r.stderr}")
    return r


class TestSetprojectlimitRetired(unittest.TestCase):
    def test_setprojectlimit_script_is_deleted_and_setlimit_exists(self):
        self.assertFalse((BIN / "setprojectlimit.py").exists(), "bin/setprojectlimit.py must be deleted (issue #49)")
        self.assertTrue((BIN / "setlimit.py").is_file(), "bin/setlimit.py must exist (issue #49)")


class TestReadLimitsFallback(unittest.TestCase):
    def test_legacy_only_key_is_not_shown(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp, lp = pathlib.Path(h), pathlib.Path(lg)
            (hp / "limits.json").write_text(json.dumps({"default": 2, "sessions": {"a": 4}}))
            (lp / "limits.json").write_text(json.dumps({"default": 1, "sessions": {"a": 1, "b": 3}}))
            with patch.object(sessions, "DEV_CONTAINER_STATE", hp), patch.object(sessions, "AGENT_SLOT_STATE", lp):
                lim = sessions.read_limits()
            self.assertEqual(lim["sessions"], {"a": 4})
            self.assertEqual(lim["default"], 2)

    def test_legacy_only_keys_listed_as_notice(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp, lp = pathlib.Path(h), pathlib.Path(lg)
            (hp / "limits.json").write_text(json.dumps({"default": 1, "sessions": {"local_" + UUID: 4}}))
            (lp / "limits.json").write_text(json.dumps({"default": 1, "sessions": {UUID: 1, "b": 3}}))
            with patch.object(sessions, "DEV_CONTAINER_STATE", hp), patch.object(sessions, "AGENT_SLOT_STATE", lp):
                self.assertEqual(sessions.legacy_only_limit_keys(), ["b"])

    def test_missing_engine_file_ignores_legacy(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp, lp = pathlib.Path(h), pathlib.Path(lg)
            (lp / "limits.json").write_text(json.dumps({"default": 1, "sessions": {"b": 3}}))
            with patch.object(sessions, "DEV_CONTAINER_STATE", hp), patch.object(sessions, "AGENT_SLOT_STATE", lp):
                lim = sessions.read_limits()
            self.assertEqual(lim["sessions"], {})

    def test_setcap_and_setlimit_leave_legacy_untouched(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp, lp = pathlib.Path(h), pathlib.Path(lg)
            legacy_doc = json.dumps({"default": 1, "sessions": {"s1": 3}})
            (lp / "limits.json").write_text(legacy_doc)
            env = {**os.environ, "DEV_CONTAINER_HOME": h, "AGENT_SLOT_STATE": lg}
            subprocess.run([sys.executable, str(BIN / "setcap.py"), "-2"], env=env, check=True, capture_output=True)
            subprocess.run([sys.executable, str(BIN / "setlimit.py"), "s1", "+1"], env=env, check=True, capture_output=True)
            self.assertEqual((lp / "limits.json").read_text(), legacy_doc)
            with patch.object(sessions, "DEV_CONTAINER_STATE", hp), patch.object(sessions, "AGENT_SLOT_STATE", lp):
                lim = sessions.read_limits()
            self.assertEqual(lim["machine_max"], 18)
            self.assertEqual(lim["sessions"], {"s1": 2})

    def test_engine_active_state_dir_is_always_new(self):
        with tempfile.TemporaryDirectory() as lg:
            missing = pathlib.Path(tempfile.gettempdir()) / "no-such-dc-home-18"
            with patch.object(engine, "DEV_CONTAINER_STATE", missing), \
                    patch.object(engine, "AGENT_SLOT_STATE", pathlib.Path(lg)):
                self.assertEqual(engine._get_active_state_dir(), missing)

    def test_setcap_and_setlimit_do_not_create_legacy_limits_file(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            env = {**os.environ, "DEV_CONTAINER_HOME": dc_dir, "AGENT_SLOT_STATE": leg_dir}
            subprocess.run([sys.executable, str(BIN / "setlimit.py"), "s1", "+1"], env=env, check=True)
            subprocess.run([sys.executable, str(BIN / "setcap.py"), "-2"], env=env, check=True)
            dc_data = json.loads((pathlib.Path(dc_dir) / "limits.json").read_text())
            self.assertEqual(dc_data["sessions"]["s1"], 2)
            self.assertEqual(dc_data["machine_max"], 18)
            self.assertFalse((pathlib.Path(leg_dir) / "limits.json").exists())


class TestSessionsPriority(unittest.TestCase):
    def test_dev_container_state_overrides_legacy_limits_and_leases(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            (dc / "limits.json").write_text(json.dumps({"default": 2, "sessions": {"s1": 4}}))
            (leg / "limits.json").write_text(json.dumps({"default": 1, "sessions": {"s1": 1}}))
            (dc / "leases").mkdir()
            (leg / "leases").mkdir()
            (dc / "leases" / "p-slot-1.json").write_text(json.dumps({"holder": "new"}))
            (leg / "leases" / "p-slot-1.json").write_text(json.dumps({"holder": "old"}))
            (leg / "leases" / "broken-slot-2.json").symlink_to(dc / "leases" / "missing.json")
            with patch.object(sessions, "DEV_CONTAINER_STATE", dc), patch.object(sessions, "AGENT_SLOT_STATE", leg):
                limits = sessions.read_limits()
                leases = sessions.read_all_leases()
            self.assertEqual(limits["sessions"]["s1"], 4)
            self.assertEqual(len(leases), 1)
            self.assertEqual(leases[0]["holder"], "new")


class TestEnginePriority(unittest.TestCase):
    def test_ensure_schedule_dirs_never_creates_legacy_dir(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir) / "missing-legacy"
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), patch.object(engine, "AGENT_SLOT_STATE", leg):
                engine._ensure_schedule_dirs()
                self.assertEqual(engine._get_active_state_dir(), dc)
            self.assertTrue((dc / "schedule" / "jobs").is_file())
            self.assertFalse(leg.exists())


class TestSessionsPerSessionLimits(unittest.TestCase):
    def _discover(self, leases, table, **sess):
        s = {"id": UUID, "ref": "local_" + UUID, "raw_ref": "local_" + UUID, "held_slots": 0,
             "is_active": True, "harness": "claude", "last_active": 1, "title": "t"}
        s.update(sess)
        with patch.object(sessions, "_probe_dropin_harnesses", return_value=([dict(s)], {})), \
                patch.object(sessions.agy_probe, "probe", return_value=[]), \
                patch.object(sessions.claude_probe, "probe", return_value=[]):
            return sessions.discover_sessions(leases=leases, limits={"default": 1, "sessions": table})

    def test_sessions_rows_resolve_limits_and_preserve_zero_pause(self):
        five_leases = [
            {"project": p, "owner": f"agent [{UUID}]", "_slot_id": p}
            for p in ("acme-org-slot-1", "acme-org-slot-2", "host_repo-slot-1", "demo-app-slot-1", "ops-console-slot-3")
        ]
        # Exact/toggled limit = 2: 1 slot -> not over; 5 slots -> over
        for leases, want_held, want_over in ((five_leases[:1], 1, False), (five_leases, 5, True)):
            res = self._discover(leases, {"local_" + UUID: 2})
            self.assertEqual(len(res), 1)
            row = res[0]
            self.assertEqual(row["held_slots"], want_held)
            self.assertEqual(row["limit"], 2)
            self.assertIs(row["is_over_limit"], want_over)

        # Paused session (limit = 0): 0 slots -> not over; 1 slot -> over
        for leases, want_over in (([], False), (five_leases[:1], True)):
            res = self._discover(leases, {UUID: 0})
            self.assertEqual(res[0]["limit"], 0)
            self.assertIs(res[0]["is_over_limit"], want_over)


class TestSetcapPauseAndBounds(unittest.TestCase):
    def test_decrements_to_zero_and_clamps_bounds(self):
        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            f.write_text(json.dumps({"default": 1, "machine_max": 1}))
            self.assertEqual(run_setcap(self, -1, h), 0)
            self.assertEqual(read(f)["machine_max"], 0)
            self.assertEqual(run_setcap(self, -1, h), 0)
            self.assertEqual(read(f)["machine_max"], 0)
            self.assertEqual(run_setcap(self, 1, h), 1)
            self.assertEqual(read(f)["machine_max"], 1)

            f.write_text(json.dumps({"default": 1, "machine_max": 20}))
            self.assertEqual(run_setcap(self, 1, h), 20)
            self.assertEqual(read(f)["machine_max"], 20)

    def test_pause_sets_zero_and_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            initial = {
                "default": 2,
                "machine_max": 12,
                "project_default": 5,
                "sessions": {"s1": 3},
                "projects": {"acme-org": 2},
            }
            f.write_text(json.dumps(initial))
            self.assertEqual(run_setcap(self, "pause", h), 0)
            self.assertEqual(read(f), {**initial, "machine_max": 0})


class TestSetlimit(unittest.TestCase):
    def test_unset_session_starts_from_default_or_1_and_handles_pause(self):
        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            self.assertEqual(run_setlimit(self, "s1", "+1", h), 2)
            self.assertEqual(read(f)["sessions"]["s1"], 2)

        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            self.assertEqual(run_setlimit(self, "s1", "1", h), 2)
            self.assertEqual(read(f)["sessions"]["s1"], 2)

        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            self.assertEqual(run_setlimit(self, "s1", "-1", h), 0)
            self.assertEqual(read(f)["sessions"]["s1"], 0)

        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            self.assertEqual(run_setlimit(self, "s1", "pause", h), 0)
            self.assertEqual(read(f)["sessions"]["s1"], 0)

        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            f.write_text(json.dumps({"default": 3}))
            self.assertEqual(run_setlimit(self, "s1", "+1", h), 4)
            self.assertEqual(run_setlimit(self, "s2", "-1", h), 2)
            self.assertEqual(read(f)["sessions"], {"s1": 4, "s2": 2})

    def test_clamps_within_0_to_20_and_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            initial = {
                "default": 2,
                "machine_max": 8,
                "sessions": {"s0": 0, "s20": 20, "other": 7},
            }
            f.write_text(json.dumps(initial))
            self.assertEqual(run_setlimit(self, "s0", "-1", h), 0)
            self.assertEqual(run_setlimit(self, "s20", "+1", h), 20)
            self.assertEqual(run_setlimit(self, "s0", "1", h), 1)
            got = read(f)
            self.assertEqual(got["default"], 2)
            self.assertEqual(got["machine_max"], 8)
            self.assertEqual(got["sessions"], {"s0": 1, "s20": 20, "other": 7})

    def test_invalid_ref_or_op_exits_2_without_touching_file(self):
        with tempfile.TemporaryDirectory() as h:
            f = pathlib.Path(h) / "limits.json"
            before = json.dumps({"default": 1, "sessions": {"s1": 3}})
            f.write_text(before)
            for ref, op in (("", "+1"), ("   ", "+1"), ("s1", "bogus"), ("s1", ""), ("s1", "PAUSE")):
                run_setlimit(self, ref, op, h, expect_ok=False)
                self.assertEqual(f.read_text(), before, (ref, op))


class TestLimitsCorePure(unittest.TestCase):
    def test_parse_limits_empty_valid_and_invalid(self):
        lc = _require_limits_core(self)
        for raw in (None, "", "   ", "\n\t  "):
            got = lc.parse_limits(raw)
            self.assertEqual(got, {"default": 1}, repr(raw))
            got["x"] = 9
            self.assertEqual(lc.parse_limits(raw), {"default": 1})

        src = '{"default": 2, "machine_max": 5, "sessions": {"s1": 3}}'
        self.assertEqual(lc.parse_limits(src), {"default": 2, "machine_max": 5, "sessions": {"s1": 3}})

        for bad in ("{bad", "[]", "[1]", "123", '"str"', "true", "false", "null"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                lc.parse_limits(bad)

    def test_apply_machine_max_table_clamping_and_immutability(self):
        lc = _require_limits_core(self)
        cases = [
            ({}, "-1", 19),
            ({}, "+1", 20),
            ({}, "0", 20),
            ({}, "pause", 0),
            ({"machine_max": 10}, "+3", 13),
            ({"machine_max": 10}, "-3", 7),
            ({"machine_max": 10}, "0", 10),
            ({"machine_max": 1}, "-5", 0),
            ({"machine_max": 18}, "+5", 20),
            ({"machine_max": 0}, "+1", 1),
            ({"machine_max": 0}, "-1", 0),
            ({"machine_max": 20}, "1", 20),
            ({"machine_max": 12}, "pause", 0),
            ({"machine_max": True}, "-1", 19),
            ({"machine_max": False}, "-1", 19),
            ({"machine_max": "5"}, "-1", 19),
            ({"machine_max": None}, "-1", 19),
        ]
        for initial, op, want in cases:
            lim = {"default": 2, "sessions": {"s1": 4}, "projects": {"p": 3}, **initial}
            snap = copy.deepcopy(lim)
            updated, new_val = lc.apply_machine_max(lim, op)
            self.assertEqual(new_val, want, (initial, op))
            self.assertEqual(updated["machine_max"], want)
            self.assertEqual(updated["default"], 2)
            self.assertEqual(updated["sessions"], {"s1": 4})
            self.assertEqual(updated["projects"], {"p": 3})
            self.assertEqual(lim, snap, "apply_machine_max must never mutate caller dict")

        for bad_op in ("", "bogus", "PAUSE", "pause ", " 1", "1 ", "1\n", "+", "-", "1.5", "--1", "+-1"):
            with self.assertRaises(ValueError, msg=repr(bad_op)):
                lc.apply_machine_max({"machine_max": 5}, bad_op)

    def test_apply_session_limit_resolution_pause_clamp_pop_and_immutability(self):
        lc = _require_limits_core(self)
        # Exact match wins over toggled spelling
        lim = {"default": 1, "machine_max": 10, "sessions": {UUID: 2, "local_" + UUID: 7}}
        snap = copy.deepcopy(lim)
        updated, new_val = lc.apply_session_limit(lim, UUID, 1)
        self.assertEqual(new_val, 3)
        self.assertEqual(updated["sessions"], {UUID: 3, "local_" + UUID: 7})
        self.assertEqual(updated["machine_max"], 10)
        self.assertEqual(lim, snap)

        # Toggled spelling updates existing key without creating duplicate key
        lim2 = {"default": 1, "sessions": {"local_" + UUID: 3}}
        updated2, new_val2 = lc.apply_session_limit(lim2, UUID, "+1")
        self.assertEqual(new_val2, 4)
        self.assertEqual(updated2["sessions"], {"local_" + UUID: 4})

        lim3 = {"default": 1, "sessions": {UUID: 3}}
        updated3, new_val3 = lc.apply_session_limit(lim3, "local_" + UUID, 1)
        self.assertEqual(new_val3, 4)
        self.assertEqual(updated3["sessions"], {UUID: 4})

        # Legacy lookup (exact and toggled) when absent from sessions; keeps ref as key; leaves legacy untouched
        legacy = {UUID: 5, "other": 3}
        legacy_snap = copy.deepcopy(legacy)
        updated4, new_val4 = lc.apply_session_limit({"default": 1}, "local_" + UUID, 1, legacy_sessions=legacy)
        self.assertEqual(new_val4, 6)
        self.assertEqual(updated4["sessions"], {"local_" + UUID: 6})
        self.assertEqual(legacy, legacy_snap)

        # Returning to limits.get("default") pops the key; 0 pause is preserved when != default
        updated5, new_val5 = lc.apply_session_limit({"default": 1, "sessions": {"s1": 2}}, "s1", "-1")
        self.assertEqual(new_val5, 1)
        self.assertEqual(updated5["sessions"], {})

        updated_pause, new_pause = lc.apply_session_limit({"default": 1, "sessions": {"s1": 3}}, "s1", "pause")
        self.assertEqual(new_pause, 0)
        self.assertEqual(updated_pause["sessions"], {"s1": 0})

        # Non-1 default: stepping away adds key, stepping back to default pops key
        updated5b, new_val5b = lc.apply_session_limit({"default": 2}, "s1", -1)
        self.assertEqual(new_val5b, 1)
        self.assertEqual(updated5b["sessions"], {"s1": 1})
        updated5c, new_val5c = lc.apply_session_limit(updated5b, "s1", 1)
        self.assertEqual(new_val5c, 2)
        self.assertEqual(updated5c["sessions"], {})

        updated6, new_val6 = lc.apply_session_limit({"default": 1}, "s1", -5)
        self.assertEqual(new_val6, 0)
        self.assertEqual(updated6["sessions"], {"s1": 0})

        # Upper cap of 20 on sessions; default clamped to 0..20 with fallback 1 when missing/invalid
        updated7, new_val7 = lc.apply_session_limit({}, "s1", 25)
        self.assertEqual(new_val7, 20)
        self.assertEqual(updated7["sessions"], {"s1": 20})

        for bad_default in (True, False, "3", None):
            u, v = lc.apply_session_limit({"default": bad_default}, "s1", "+1")
            self.assertEqual(v, 2, bad_default)
            self.assertEqual(u["sessions"], {"s1": 2}, bad_default)
        self.assertEqual(lc.apply_session_limit({"default": -5}, "s1", "+1")[1], 1)
        self.assertEqual(lc.apply_session_limit({"default": 99}, "s1", "-1")[1], 19)

        for bad_op in (True, False, 1.5, None, "", "bogus", "PAUSE", "1.5", "+-1"):
            with self.assertRaises(ValueError, msg=repr(bad_op)):
                lc.apply_session_limit({"default": 1}, "s1", bad_op)
        for bad_ref in ("", "   ", None, 123):
            with self.assertRaises(ValueError, msg=repr(bad_ref)):
                lc.apply_session_limit({"default": 1}, bad_ref, 1)

    def test_apply_session_limit_delegates_key_resolution_to_limit_keys(self):
        import limit_keys

        lc = _require_limits_core(self)
        with patch.object(limit_keys, "PREFIX", "remote_"):
            lim = {"default": 1, "sessions": {"remote_" + UUID: 3}}
            self.assertEqual(limit_keys.find_key(lim["sessions"], UUID), "remote_" + UUID)
            updated, new_val = lc.apply_session_limit(lim, UUID, 1)
            self.assertEqual(new_val, 4)
            self.assertEqual(updated["sessions"], {"remote_" + UUID: 4})

            legacy = {"remote_" + UUID: 5}
            updated_leg, new_leg = lc.apply_session_limit({"default": 1}, UUID, 1, legacy_sessions=legacy)
            self.assertEqual(new_leg, 6)
            self.assertEqual(updated_leg["sessions"], {UUID: 6})

        def custom_norm(s):
            return s[len("custom_"):] if isinstance(s, str) and s.startswith("custom_") else s

        with patch.object(limit_keys, "norm", custom_norm):
            lim2 = {"default": 1, "sessions": {UUID: 2}}
            self.assertEqual(limit_keys.find_key(lim2["sessions"], "custom_" + UUID), UUID)
            updated2, new_val2 = lc.apply_session_limit(lim2, "custom_" + UUID, 1)
            self.assertEqual(new_val2, 3)
            self.assertEqual(updated2["sessions"], {UUID: 3})

        with tempfile.TemporaryDirectory() as tmpdir:
            probe = (
                "import sys; "
                f"sys.path.insert(0, {str(ROOT)!r}); "
                "from sw import limits_core; "
                "import limit_keys; "
                "assert getattr(limits_core, 'limit_keys', None) is limit_keys; "
                f"u, v = limits_core.apply_session_limit({{'default': 1, 'sessions': {{'local_{UUID}': 2}}}}, {UUID!r}, 1); "
                f"assert v == 3 and u['sessions'] == {{'local_{UUID}': 3}}, (u, v)"
            )
            r = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmpdir, capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, r.stderr)


class TestLimitsStoreFlock(unittest.TestCase):
    def test_creates_dir_reads_default_writes_formatted_json_with_fsync(self):
        ls = _require_limits_store(self)
        with tempfile.TemporaryDirectory() as d:
            state_dir = pathlib.Path(d) / "nested" / "state"
            with patch.object(os, "fsync", wraps=os.fsync) as fsync_spy:
                res = ls.update_limits(state_dir, lambda doc: ({**doc, "machine_max": 12}, 12))
            self.assertEqual(res, 12)
            self.assertTrue(fsync_spy.called, "update_limits must call os.fsync before os.replace")
            target = state_dir / "limits.json"
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), json.dumps({"default": 1, "machine_max": 12}, indent=2) + "\n")
            self.assertEqual(list(state_dir.glob("*.tmp")), [])

    def test_holds_exclusive_flock_during_mutate_and_releases_after(self):
        ls = _require_limits_store(self)
        with tempfile.TemporaryDirectory() as d:
            state_dir = pathlib.Path(d)
            lock_path = state_dir / "limits.lock"
            observations = []

            def mutate(doc):
                self.assertTrue(lock_path.exists(), "limits.lock must exist while mutate runs")
                with open(lock_path, "r+", encoding="utf-8") as probe:
                    with self.assertRaises(BlockingIOError, msg="limits.lock must be exclusively flocked during mutate"):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observations.append("locked")
                return {**doc, "machine_max": 7}, 7

            self.assertEqual(ls.update_limits(state_dir, mutate), 7)
            self.assertEqual(observations, ["locked"])
            with open(lock_path, "r+", encoding="utf-8") as probe:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)

    def test_mutate_exception_and_invalid_json_leave_target_untouched(self):
        ls = _require_limits_store(self)
        with tempfile.TemporaryDirectory() as d:
            state_dir = pathlib.Path(d)
            target = state_dir / "limits.json"

            def boom(_doc):
                raise ValueError("bad op")

            with self.assertRaises(ValueError):
                ls.update_limits(state_dir, boom)
            self.assertFalse(target.exists(), "failed mutate must not create limits.json")
            self.assertEqual(list(state_dir.glob("*.tmp")), [])

            before = json.dumps({"default": 1, "machine_max": 9}) + "\n"
            target.write_text(before, encoding="utf-8")
            with self.assertRaises(ValueError):
                ls.update_limits(state_dir, boom)
            self.assertEqual(target.read_text(encoding="utf-8"), before)

            # Corrupt JSON or non-dict JSON raises ValueError without modifying file
            for bad in ("{not json", "[]"):
                target.write_text(bad, encoding="utf-8")
                with self.assertRaises(ValueError):
                    ls.update_limits(state_dir, lambda doc: (doc, 0))
                self.assertEqual(target.read_text(encoding="utf-8"), bad)

    def test_replace_failure_cleans_up_temp_file(self):
        ls = _require_limits_store(self)
        with tempfile.TemporaryDirectory() as d:
            state_dir = pathlib.Path(d)
            target = state_dir / "limits.json"
            target.write_text(json.dumps({"default": 1}) + "\n", encoding="utf-8")
            with patch.object(os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    ls.update_limits(state_dir, lambda doc: ({**doc, "machine_max": 4}, 4))
            self.assertEqual(read(target), {"default": 1})
            self.assertEqual(list(state_dir.glob("*.tmp")), [], "temp file must be removed when os.replace fails")


class TestLimitsCliWrappers(unittest.TestCase):
    def test_each_cli_writer_uses_limits_lock_and_writes_no_limits_tmp(self):
        for script, args in (
            ("setcap.py", ["-1"]),
            ("setlimit.py", ["s1", "+1"]),
        ):
            with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
                hp = pathlib.Path(h)
                env = {**os.environ, "DEV_CONTAINER_HOME": h, "AGENT_SLOT_STATE": lg}
                r = subprocess.run([sys.executable, str(BIN / script), *args], env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{script} failed: {r.stderr}")
                self.assertTrue((hp / "limits.lock").exists(), f"{script} must serialize via limits.lock")
                self.assertFalse((hp / "limits.tmp").exists(), f"{script} must not use limits.tmp")
                self.assertEqual(list(hp.glob("*.tmp")), [])
                src = (BIN / script).read_text(encoding="utf-8")
                self.assertNotIn("limits.tmp", src)
                self.assertNotIn('.with_suffix(".tmp")', src)
                self.assertNotIn(".with_suffix('.tmp')", src)

    def test_concurrent_writers_serialize_under_flock_without_dropping_updates_or_sibling_keys(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp = pathlib.Path(h)
            target = hp / "limits.json"
            initial = {
                "default": 1,
                "machine_max": 20,
                "sessions": {"keep-sess": 9},
                "extra": "keep-me",
            }
            target.write_text(json.dumps(initial), encoding="utf-8")
            env = {**os.environ, "DEV_CONTAINER_HOME": h, "AGENT_SLOT_STATE": lg}

            cmds = (
                [[sys.executable, str(BIN / "setcap.py"), "-1"] for _ in range(5)]
                + [[sys.executable, str(BIN / "setlimit.py"), "sess-a", "+1"] for _ in range(5)]
                + [[sys.executable, str(BIN / "setlimit.py"), "sess-b", "2"] for _ in range(3)]
            )
            procs = [subprocess.Popen(c, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for c in cmds]
            results = [(p, c, *p.communicate(timeout=30)) for p, c in zip(procs, cmds)]
            for p, c, out, err in results:
                self.assertEqual(p.returncode, 0, f"{c} exited {p.returncode}: {err}")
                self.assertEqual(err, "", f"{c} unexpected stderr: {err}")
                self.assertTrue(out.strip().isdigit(), f"{c} bad stdout: {out!r}")

            got = read(target)
            self.assertEqual(got["machine_max"], 15)
            self.assertEqual(got["sessions"]["sess-a"], 6)
            self.assertEqual(got["sessions"]["sess-b"], 7)
            self.assertEqual(got["sessions"]["keep-sess"], 9)
            self.assertEqual(got["extra"], "keep-me")
            self.assertFalse((hp / "limits.tmp").exists())
            self.assertEqual(list(hp.glob("*.tmp")), [])

    def test_concurrent_cold_start_without_limits_file(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp = pathlib.Path(h) / "missing-dc"
            env = {**os.environ, "DEV_CONTAINER_HOME": str(hp), "AGENT_SLOT_STATE": lg}
            cmds = (
                [[sys.executable, str(BIN / "setcap.py"), "-1"] for _ in range(3)]
                + [[sys.executable, str(BIN / "setlimit.py"), "sess", "1"] for _ in range(3)]
            )
            procs = [subprocess.Popen(c, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for c in cmds]
            results = [(p, c, *p.communicate(timeout=30)) for p, c in zip(procs, cmds)]
            for p, c, _out, err in results:
                self.assertEqual(p.returncode, 0, f"{c} failed: {err}")

            got = read(hp / "limits.json")
            self.assertEqual(got["machine_max"], 17)
            self.assertEqual(got["sessions"]["sess"], 4)
            self.assertEqual(list(hp.glob("*.tmp")), [])

    def test_invalid_cli_args_exit_2_without_creating_or_modifying_limits_json(self):
        invalid_invocations = [
            ("setcap.py", []),
            ("setcap.py", ["1", "2"]),
            ("setcap.py", ["bogus"]),
            ("setcap.py", [""]),
            ("setcap.py", ["1.5"]),
            ("setcap.py", ["PAUSE"]),
            ("setlimit.py", []),
            ("setlimit.py", ["s1"]),
            ("setlimit.py", ["s1", "1", "extra"]),
            ("setlimit.py", ["", "+1"]),
            ("setlimit.py", ["   ", "+1"]),
            ("setlimit.py", ["s1", "bogus"]),
            ("setlimit.py", ["s1", "1.5"]),
            ("setlimit.py", ["s1", "PAUSE"]),
        ]
        # Case 1: limits.json does not exist -> must not be created
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp = pathlib.Path(h)
            target = hp / "limits.json"
            env = {**os.environ, "DEV_CONTAINER_HOME": h, "AGENT_SLOT_STATE": lg}
            for script, args in invalid_invocations:
                r = subprocess.run([sys.executable, str(BIN / script), *args], env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 2, f"{script} {args!r} expected exit 2, got {r.returncode}: {r.stderr}")
                self.assertTrue(r.stderr.strip(), f"{script} {args!r} should print an error to stderr")
                self.assertFalse(target.exists(), f"{script} {args!r} created limits.json")

        # Case 2: limits.json exists -> must not be modified
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as lg:
            hp = pathlib.Path(h)
            target = hp / "limits.json"
            before = json.dumps({"default": 1, "machine_max": 11, "sessions": {"s1": 3}, "projects": {"p": 2}})
            target.write_text(before, encoding="utf-8")
            env = {**os.environ, "DEV_CONTAINER_HOME": h, "AGENT_SLOT_STATE": lg}
            for script, args in invalid_invocations:
                r = subprocess.run([sys.executable, str(BIN / script), *args], env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 2, f"{script} {args!r} expected exit 2, got {r.returncode}: {r.stderr}")
                self.assertTrue(r.stderr.strip(), f"{script} {args!r} should print an error to stderr")
                self.assertEqual(target.read_text(encoding="utf-8"), before, f"{script} {args!r} modified limits.json")


if __name__ == "__main__":
    unittest.main()
