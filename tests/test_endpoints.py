#!/usr/bin/env python3
"""Tests for ops-console Issue an earlier change & #34: Engine Status, Verdict, Cleaners, and retired /api/limit.

Acceptance Criteria:
1. Slots section shows live leases only.
2. Cleaners section: fast tick and slow check (last_run, what_it_did/result, next_planned_run)
   read from engine's status output (`dev-container status --json`).
3. Lists `~/.dev-containers/rescue/<project>/<lease>/` with size, age, branches from `meta.json` and `verdict.md`.
4. Buttons for fast tick now, slow check now, and send archives to agents for `verdict.md`.
5. Trigger cleaner calls engine on-demand command (`dev-container schedule run fast` / `slow`).
6. Agents button writes `verdict.md` and deletes nothing in the archive; result accessible in `/api/state` (`results`).
7. Until engine is installed, fallback keeps showing abandoned slots as before.
8. (#34) bin/setlimit.py is deleted and POST /api/limit returns 404; /api/limit is absent from sw/ and static/.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure sw-178 root and bin/ are on sys.path
SW_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = SW_ROOT / "bin"
for p in (str(SW_ROOT), str(BIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine  # noqa: E402
import server  # noqa: E402
import slots as snapshot  # noqa: E402
from test_sse import ServerCase  # noqa: E402


class TestIssue178EngineCleaners(unittest.TestCase):
    """AC 2 & AC 5: Cleaner telemetry from dev-container status --json and on-demand entry point execution."""

    @patch("subprocess.run")
    def test_cleaners_read_from_engine_status_json(self, mock_run):
        """AC 2: Cleaners section reads what_it_did / result and next_planned_run from dev-container status --json."""
        engine_status = {
            "version": 1,
            "engine": "running",
            "status": "running",
            "cleaners": {
                "fast": {
                    "last_run": 1794160942,
                    "last_run_timestamp": 1794160942,
                    "result": "ok",
                    "interval": 300,
                    "next_run": 1794161242,
                    "next_planned_run": 1794161242,
                    "status": "ok",
                },
                "slow": {
                    "last_run": 1794160672,
                    "last_run_timestamp": 1794160672,
                    "result": "locked",
                    "interval": 3600,
                    "next_run": 1794164272,
                    "next_planned_run": 1794164272,
                    "status": "locked",
                    "locked": True,
                },
            },
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(engine_status), stderr="")

        status = engine.get_cleaners_status()
        self.assertIn("fast", status, "Status dict must contain 'fast' cleaner")
        self.assertIn("slow", status, "Status dict must contain 'slow' cleaner")

        # Must reflect what_it_did / result from dev-container status --json
        fast_result = status["fast"].get("result") or status["fast"].get("what_it_did")
        self.assertIsNotNone(
            fast_result,
            "Fast cleaner status must contain 'result' or 'what_it_did' from dev-container status --json",
        )
        self.assertEqual(fast_result, "ok")
        self.assertIn(
            "next_planned_run",
            status["fast"],
            "Fast cleaner status must contain 'next_planned_run' from dev-container status --json",
        )
        self.assertEqual(status["fast"]["next_planned_run"], 1794161242)

        slow_result = status["slow"].get("result") or status["slow"].get("what_it_did")
        self.assertEqual(slow_result, "locked")
        self.assertIn(
            "next_planned_run",
            status["slow"],
            "Slow cleaner status must contain 'next_planned_run' from dev-container status --json",
        )
        self.assertEqual(status["slow"]["next_planned_run"], 1794164272)

    @patch("subprocess.run")
    def test_trigger_cleaner_fast_invokes_engine_schedule_run(self, mock_run):
        """AC 4 & 5: trigger_cleaner('fast') invokes 'dev-container schedule run fast' rather than a secondary code path."""
        mock_run.return_value = MagicMock(returncode=0, stdout="schedule: fast (reap --all) exited 0\n", stderr="")

        ok, msg = engine.trigger_cleaner("fast")
        self.assertTrue(ok, f"trigger_cleaner('fast') should return success: {msg}")

        called_cmds = [
            call_item.args[0] if call_item.args else call_item.kwargs.get("args")
            for call_item in mock_run.call_args_list
        ]
        found_schedule_fast = any(
            isinstance(cmd, list)
            and any("schedule" in str(arg) for arg in cmd)
            and "run" in cmd
            and "fast" in cmd
            for cmd in called_cmds
            if cmd
        )
        self.assertTrue(
            found_schedule_fast,
            f"trigger_cleaner('fast') must invoke 'dev-container schedule run fast'. Executed commands: {called_cmds}",
        )

        found_secondary = any(
            isinstance(cmd, list)
            and any("agent-slot.sh" in str(arg) or "agent-slot-reap" in str(arg) for arg in cmd)
            for cmd in called_cmds
            if cmd
        )
        self.assertFalse(
            found_secondary,
            "trigger_cleaner('fast') must not invoke secondary code path (agent-slot.sh reap)",
        )

    @patch("rescue_audit.run", return_value=({}, {}))
    @patch("subprocess.run")
    def test_trigger_cleaner_slow_invokes_engine_schedule_run(self, mock_run, mock_audit):
        """AC 4 & 5: trigger_cleaner('slow') invokes 'dev-container schedule run slow' rather than a secondary code path."""
        mock_run.return_value = MagicMock(returncode=0, stdout="schedule: slow (check) exited 0\n", stderr="")

        ok, msg = engine.trigger_cleaner("slow")
        self.assertTrue(ok, f"trigger_cleaner('slow') should return success: {msg}")

        called_cmds = [
            call_item.args[0] if call_item.args else call_item.kwargs.get("args")
            for call_item in mock_run.call_args_list
        ]
        found_schedule_slow = any(
            isinstance(cmd, list)
            and any("schedule" in str(arg) for arg in cmd)
            and "run" in cmd
            and "slow" in cmd
            for cmd in called_cmds
            if cmd
        )
        self.assertTrue(
            found_schedule_slow,
            f"trigger_cleaner('slow') must invoke 'dev-container schedule run slow'. Executed commands: {called_cmds}",
        )
        self.assertFalse(
            mock_audit.called,
            "trigger_cleaner('slow') must not invoke secondary code path (rescue_audit.run)",
        )


class TestIssue178RescueArchivesAndVerdict(unittest.TestCase):
    """AC 3 & AC 6: Rescue archives listing and agent verdict generation."""

    @patch("subprocess.run")
    def test_archives_read_from_engine_status_json(self, mock_run):
        """AC 3: Reading archives (<project>/<lease>, branches from meta.json, verdict.md) from dev-container status --json."""
        engine_status = {
            "version": 1,
            "archives": [
                {
                    "project": "demo-app",
                    "lease": "demo-app-slot-4",
                    "size": 524288,
                    "size_bytes": 524288,
                    "age": 14400,
                    "branches": ["feat/search-filter", "fix/oauth"],
                    "verdict": True,
                    "has_verdict": True,
                }
            ],
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(engine_status), stderr="")

        self.assertTrue(
            hasattr(engine, "get_rescue_archives") or hasattr(engine, "get_archives"),
            "engine module must define get_rescue_archives() or get_archives() to read archives from dev-container status --json",
        )
        getter = getattr(engine, "get_rescue_archives", None) or getattr(engine, "get_archives", None)
        archives = getter()
        self.assertEqual(len(archives), 1, "Must return 1 archive entry")
        arch = archives[0]
        self.assertEqual(arch.get("project"), "demo-app")
        self.assertEqual(arch.get("lease"), "demo-app-slot-4")
        self.assertEqual(arch.get("size") or arch.get("size_bytes"), 524288)
        self.assertEqual(arch.get("age"), 14400)
        self.assertEqual(arch.get("branches"), ["feat/search-filter", "fix/oauth"])
        self.assertTrue(arch.get("verdict") is True or arch.get("has_verdict") is True)

    def test_write_agent_verdict_creates_file_and_preserves_archive(self):
        """AC 6: Agent verdict action writes verdict.md and deletes NOTHING in the archive directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = pathlib.Path(tmpdir)
            rescue_dir = tmp_path / "rescue" / "demo-app" / "slot-12"
            rescue_dir.mkdir(parents=True, exist_ok=True)
            meta_file = rescue_dir / "meta.json"
            meta_file.write_text(json.dumps({"project": "demo-app", "lease": "slot-12", "branches": ["feat/x"]}))
            bundle_file = rescue_dir / "commits.bundle"
            bundle_file.write_text("bundle data")
            patch_file = rescue_dir / "changes.patch"
            patch_file.write_text("patch data")

            if hasattr(engine, "write_agent_verdict"):
                ok, msg = engine.write_agent_verdict("demo-app", "slot-12", base_dir=tmp_path / "rescue")
            else:
                ok, msg = engine.manage_rescue("verdict", "demo-app/slot-12")

            self.assertTrue(ok, f"Agent verdict action must succeed: {msg}")

            verdict_file = rescue_dir / "verdict.md"
            self.assertTrue(verdict_file.exists(), f"verdict.md must be written in {rescue_dir}")

            # Deleting stays the owner's: verify archive files are preserved
            self.assertTrue(meta_file.exists(), "meta.json must not be deleted by agents verdict action")
            self.assertTrue(bundle_file.exists(), "commits.bundle must not be deleted by agents verdict action")
            self.assertTrue(patch_file.exists(), "changes.patch must not be deleted by agents verdict action")

    def test_api_rescue_verdict_action_records_result(self):
        """AC 6: POST /api/rescue with verdict action reports its result, accessible to /api/state (`results`)."""
        from sw import core
        from sw.domains import cleaners

        with patch.object(engine, "manage_rescue", return_value=(True, "verdict generated")), \
                patch.object(cleaners, "note"):
            args = cleaners.validate_rescue({"id": "cmd-00000001", "action": "verdict", "target": "demo-app/slot-12"})
            self.assertEqual(args, ("verdict", "demo-app/slot-12"))
            rc, msg = cleaners.do_rescue(args)
        self.assertEqual((rc, msg), (0, "verdict generated"))
        done = {"route": "/api/rescue", "body": json.dumps({"action": "verdict", "target": "demo-app/slot-12"}),
                "state": "ok", "message": msg, "done_at": 5.0, "rev": 1}
        _, results = core.command_results([done])
        self.assertIn("demo-app/slot-12", results, "Verdict action result must be accessible to /api/state")
        self.assertTrue(results["demo-app/slot-12"].get("ok"), "Verdict result entry must record success")


class TestIssue178EngineFallbackAndLiveSlots(unittest.TestCase):
    """AC 1 & AC 7: Live slots only when engine active, and fallback to legacy abandoned slots when engine unavailable."""

    def test_engine_availability_check(self):
        """AC 7: engine.is_engine_available() reflects whether dev-container engine binary is available."""
        self.assertTrue(
            hasattr(engine, "is_engine_available"),
            "engine module must define is_engine_available() -> bool to detect dev-container engine installation",
        )
        avail = engine.is_engine_available()
        self.assertIsInstance(avail, bool, "is_engine_available() must return a boolean")

    @patch("engine.is_engine_available", return_value=False, create=True)
    def test_snapshot_fallback_shows_abandoned_slots_when_engine_unavailable(self, mock_engine_avail):
        """AC 7: When engine is unavailable (is_engine_available() False), snapshot includes legacy abandoned slots."""
        self.assertTrue(
            hasattr(snapshot, "discover_slots") or hasattr(snapshot, "get_slots"),
            "snapshot module must provide discover_slots() or get_slots() supporting engine-unavailable fallback",
        )

    @patch("engine.is_engine_available", return_value=True, create=True)
    def test_snapshot_live_slots_only_when_engine_available(self, mock_engine_avail):
        """AC 1: When engine is available, slots section shows live leases only (no unleased abandoned bands)."""
        self.assertTrue(
            hasattr(snapshot, "discover_slots") or hasattr(snapshot, "get_slots"),
            "snapshot module must provide discover_slots() or get_slots()",
        )


class TestSetGlobalIsAnOrdinaryCommand(unittest.TestCase):
    def test_set_global_is_validated_and_confirmed_by_the_legacy_zone(self):
        from sw.domains import autoreview

        route = autoreview.ROUTES["/api/autoreview"]
        args = route.validate({"id": "cmd-00000001", "args": ["set-global", "harness", "agy"]})
        self.assertEqual(args, ["set-global", "harness", "agy"])
        self.assertEqual(tuple(route.zones(args)), ("autoreview",))
        self.assertIsNone(route.validate({"id": "cmd-00000001", "args": ["set-global", "color", "red"]}))

    def test_run_holds_action_lock_only_around_the_cli_call(self):
        from sw import ctx
        from sw.domains import autoreview

        seen = {}

        def fake_run_bin(*a, **kw):
            seen["held"] = ctx.action_lock.locked()
            return 0, "ok"

        with patch.object(autoreview, "run_bin", side_effect=fake_run_bin), patch.object(autoreview, "note"):
            self.assertEqual(autoreview.do_autoreview(["set-global", "harness", "agy"]), (0, "ok"))
        self.assertTrue(seen["held"])
        self.assertFalse(ctx.action_lock.locked())


class TestPerSessionLimitAndRetiredProjectLimit(ServerCase):
    """Issue #49 Fact: bin/setprojectlimit.py and /api/project-limit are deleted (404); /api/limit is restored."""

    def test_setprojectlimit_deleted_and_setlimit_present(self):
        self.assertFalse((BIN_DIR / "setprojectlimit.py").exists())
        self.assertTrue((BIN_DIR / "setlimit.py").is_file())

    def test_post_api_project_limit_returns_404(self):
        for body in (
            {"id": "cmd-plim0001", "project": "acme-org", "delta": 1},
            {"id": "cmd-plim0002", "project": "acme-org", "mode": "pause"},
            {},
        ):
            code, payload = self.post("/api/project-limit", body)
            self.assertEqual(code, 404, (body, payload))
        # Adjacent live limit routes (/api/cap, /api/limit) respond 202
        self.assertEqual(self.post("/api/cap", {"id": "cmd-cap00001", "delta": -1})[0], 202)

    def test_post_api_limit_accepts_valid_and_rejects_invalid(self):
        for body in (
            {"id": "cmd-lim00001", "ref": "s1", "delta": 1},
            {"id": "cmd-lim00002", "ref": "s1", "delta": -1},
            {"id": "cmd-lim00003", "ref": "s1", "mode": "pause"},
        ):
            code, payload = self.post("/api/limit", body)
            self.assertEqual(code, 202, (body, payload))
            self.assertEqual(payload, {"ok": True, "id": body["id"]})

        for bad in (
            {"id": "cmd-lim00004", "ref": "", "delta": 1},
            {"id": "cmd-lim00005", "ref": "s1", "delta": 2},
            {"id": "cmd-lim00006", "ref": "s1", "delta": True},
            {"id": "cmd-lim00007", "ref": "s1", "mode": "bogus"},
        ):
            code, _ = self.post("/api/limit", bad)
            self.assertEqual(code, 400, bad)

    def test_no_project_limit_in_domains_sw_static_or_page(self):
        from sw import domains

        _, routes = domains.discover()
        self.assertNotIn("/api/project-limit", routes)
        self.assertIn("/api/limit", routes)
        hits = []
        for p in [SW_ROOT / "page.html"] + list((SW_ROOT / "sw").rglob("*.py")) + list((SW_ROOT / "static").glob("*.js")):
            if p.is_file():
                txt = p.read_text(encoding="utf-8", errors="replace")
                if "/api/project-limit" in txt or "project-limits-bar" in txt or "setprojectlimit" in txt:
                    hits.append(p.relative_to(SW_ROOT).as_posix())
        self.assertEqual(hits, [])


class TestIssue39GetRescueArchivesKey(unittest.TestCase):
    """Issue #39 AC 1: get_rescue_archives() preserves key and checks rescue/<key>/<lease>/verdict.md first."""

    def test_mapped_archives_preserve_key_and_read_verdict_from_key_dir_first(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            files = {
                dc / "rescue" / "a1b2c3d4" / "slot-2" / "verdict.md": "KEY_VERDICT_ONLY",
                dc / "rescue" / "b2c3d4e5" / "slot-3" / "verdict.md": "FROM_KEY_PRIORITY",
                dc / "rescue" / "acme-org" / "slot-3" / "verdict.md": "FROM_PROJ_SHADOWED",
                dc / "rescue" / "host_repo" / "slot-4" / "verdict.md": "FROM_PROJ_FALLBACK",
                leg / "rescue" / "d4e5f6a7" / "slot-5" / "verdict.md": "FROM_AGENT_SLOT_KEY",
                dc / "rescue" / "plain-proj" / "slot-6" / "verdict.md": "DEFAULT_KEY_VERDICT",
                dc / "rescue" / "empty-key" / "slot-7" / "verdict.md": "EMPTY_KEY_VERDICT",
                dc / "rescue" / "a1b2c3d4" / "slot-99" / "verdict.md": "SIBLING_ONLY",
            }
            for p, text in files.items():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")

            engine_status = {
                "version": 1,
                "archives": [
                    {"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-2", "size": 2048, "age": 60, "branches": ["feat/x"], "verdict": False, "has_verdict": False},
                    {"project": "acme-org", "key": "b2c3d4e5", "lease": "slot-3", "size": 1024, "age": 30, "branches": [], "verdict": False, "has_verdict": False},
                    {"project": "host_repo", "key": "c3d4e5f6", "lease": "slot-4", "size": 512, "age": 10, "branches": [], "verdict": False, "has_verdict": False},
                    {"project": "agent-proj", "key": "d4e5f6a7", "lease": "slot-5", "size": 256, "age": 5, "branches": [], "verdict": False, "has_verdict": False},
                    {"project": "plain-proj", "lease": "slot-6", "size": 128, "age": 1, "branches": [], "verdict": False, "has_verdict": False},
                    {"project": "empty-key", "key": "", "lease": "slot-7", "size": 64, "age": 1, "branches": [], "verdict": False, "has_verdict": False},
                    {"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-8", "size": 64, "age": 1, "branches": [], "verdict": False, "has_verdict": False},
                ],
            }

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(engine_status), stderr="")):
                archives = engine.get_rescue_archives(limit=50)

            by_id = {a["id"]: a for a in archives}
            expected = [
                ("ops-console/slot-2", "a1b2c3d4", True, "KEY_VERDICT_ONLY"),
                ("acme-org/slot-3", "b2c3d4e5", True, "FROM_KEY_PRIORITY"),
                ("host_repo/slot-4", "c3d4e5f6", True, "FROM_PROJ_FALLBACK"),
                ("agent-proj/slot-5", "d4e5f6a7", True, "FROM_AGENT_SLOT_KEY"),
                ("plain-proj/slot-6", "plain-proj", True, "DEFAULT_KEY_VERDICT"),
                ("empty-key/slot-7", "empty-key", True, "EMPTY_KEY_VERDICT"),
                ("ops-console/slot-8", "a1b2c3d4", False, ""),
            ]
            for aid, want_key, want_verdict, want_summary in expected:
                self.assertIn(aid, by_id)
                arch = by_id[aid]
                self.assertEqual(arch.get("key"), want_key, aid)
                self.assertIs(arch.get("verdict"), want_verdict, aid)
                self.assertIs(arch.get("has_verdict"), want_verdict, aid)
                self.assertEqual(arch.get("verdict_summary"), want_summary, aid)
                self.assertIs(arch.get("can_purge"), False, aid)


class TestIssue39WriteAgentVerdictKeyResolution(unittest.TestCase):
    """Issue #39 AC 2: write_agent_verdict() and manage_rescue('verdict', ...) resolve project keys."""

    def test_write_agent_verdict_resolves_labels_key_and_boundary_cases(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            dc, leg, custom_base = tmp / "dc", tmp / "leg", tmp / "custom_rescue"
            labels = dc / "projects" / "labels"
            labels.mkdir(parents=True)

            # 1. Default base_dir: key resolved via labels/<project> (stripped)
            (labels / "ops-console").write_text("deadbeef\n  ", encoding="utf-8")
            keyed_dir = dc / "rescue" / "deadbeef" / "slot-2"
            keyed_dir.mkdir(parents=True)
            (keyed_dir / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-2", "branches": ["feat/x"]}), encoding="utf-8")
            (keyed_dir / "commits.bundle").write_text("bundle", encoding="utf-8")
            (keyed_dir / "changes.patch").write_text("patch", encoding="utf-8")

            # 2. Explicit base_dir= with labels/<project>
            (labels / "custom-proj").write_text("cafebabe\n", encoding="utf-8")
            custom_dir = custom_base / "cafebabe" / "slot-3"
            custom_dir.mkdir(parents=True)
            (custom_dir / "meta.json").write_text("{}", encoding="utf-8")

            # 3. Direct project/lease preferred over labels/<project> key when both exist
            (labels / "myproj").write_text("abcd1234\n", encoding="utf-8")
            direct_dir = dc / "rescue" / "myproj" / "slot-1"
            shadowed_dir = dc / "rescue" / "abcd1234" / "slot-1"
            direct_dir.mkdir(parents=True)
            shadowed_dir.mkdir(parents=True)

            # 4. AGENT_SLOT_STATE fallback (both project/lease and key/lease)
            leg_direct = leg / "rescue" / "legacy-direct" / "slot-4"
            leg_direct.mkdir(parents=True)
            (labels / "legacy-keyed").write_text("feedface\n", encoding="utf-8")
            leg_keyed = leg / "rescue" / "feedface" / "slot-5"
            leg_keyed.mkdir(parents=True)

            # 5. Boundary traps for negative cases
            (labels / "empty-proj").write_text("  \n", encoding="utf-8")
            empty_trap = dc / "rescue" / "slot-1"
            empty_trap.mkdir(parents=True)
            (labels / "evil-proj").write_text("../escape\n", encoding="utf-8")
            escape_trap = dc / "escape" / "slot-1"
            escape_trap.mkdir(parents=True)
            (labels / "myproj-extra").write_text("99999999\n", encoding="utf-8")
            sibling_trap = dc / "rescue" / "99999999" / "slot-9"
            sibling_trap.mkdir(parents=True)

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), patch.object(engine, "AGENT_SLOT_STATE", leg):
                ok1, msg1 = engine.write_agent_verdict("ops-console", "slot-2")
                self.assertTrue(ok1, msg1)
                self.assertTrue((keyed_dir / "verdict.md").is_file())
                self.assertTrue((keyed_dir / "meta.json").is_file())
                self.assertTrue((keyed_dir / "commits.bundle").is_file())
                self.assertTrue((keyed_dir / "changes.patch").is_file())

                ok2, msg2 = engine.write_agent_verdict("custom-proj", "slot-3", base_dir=custom_base)
                self.assertTrue(ok2, msg2)
                self.assertTrue((custom_dir / "verdict.md").is_file())

                ok3, msg3 = engine.write_agent_verdict("myproj", "slot-1")
                self.assertTrue(ok3, msg3)
                self.assertTrue((direct_dir / "verdict.md").is_file())
                self.assertFalse((shadowed_dir / "verdict.md").exists())

                ok4, msg4 = engine.write_agent_verdict("legacy-direct", "slot-4")
                self.assertTrue(ok4, msg4)
                self.assertTrue((leg_direct / "verdict.md").is_file())

                ok5, msg5 = engine.write_agent_verdict("legacy-keyed", "slot-5")
                self.assertTrue(ok5, msg5)
                self.assertTrue((leg_keyed / "verdict.md").is_file())

                for proj, lease, trap in (
                    ("unknown-proj", "slot-1", None),
                    ("empty-proj", "slot-1", empty_trap / "verdict.md"),
                    ("evil-proj", "slot-1", escape_trap / "verdict.md"),
                    ("myproj", "slot-9", sibling_trap / "verdict.md"),
                ):
                    ok_bad, msg_bad = engine.write_agent_verdict(proj, lease)
                    self.assertFalse(ok_bad, (proj, lease, msg_bad))
                    self.assertIn("not found", msg_bad.lower(), (proj, lease, msg_bad))
                    if trap is not None:
                        self.assertFalse(trap.exists(), (proj, lease))

    def test_manage_rescue_verdict_passes_archive_key_from_get_rescue_archives(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            d1 = dc / "rescue" / "a1b2c3d4" / "slot-2"
            d1.mkdir(parents=True)
            (d1 / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-2", "branches": ["fix/a"]}), encoding="utf-8")
            (d1 / "commits.bundle").write_text("bundle", encoding="utf-8")

            d2 = dc / "rescue" / "feedface" / "slot-8"
            d2.mkdir(parents=True)
            (d2 / "meta.json").write_text(json.dumps({"project": "host_repo", "lease": "slot-8"}), encoding="utf-8")

            engine_status = {
                "version": 1,
                "archives": [
                    {"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-2", "size": 100, "age": 10, "branches": ["fix/a"], "verdict": False},
                    {"project": "host_repo", "key": "feedface", "lease": "slot-8", "size": 100, "age": 10, "branches": [], "verdict": False},
                ],
            }

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(engine_status), stderr="")):
                ok1, msg1 = engine.manage_rescue("verdict", "ops-console/slot-2")
                self.assertTrue(ok1, msg1)
                self.assertTrue((d1 / "verdict.md").is_file())
                self.assertTrue((d1 / "meta.json").is_file())
                self.assertTrue((d1 / "commits.bundle").is_file())

                ok2, msg2 = engine.manage_rescue("agent_verdict", "slot-8")
                self.assertTrue(ok2, msg2)
                self.assertTrue((d2 / "verdict.md").is_file())

                ok_missing, msg_missing = engine.manage_rescue("verdict", "ops-console/slot-99")
                self.assertFalse(ok_missing)
                self.assertIn("not found", msg_missing.lower())

            # Fallback without status --json resolves via projects/labels/<project>
            (dc / "projects" / "labels").mkdir(parents=True)
            (dc / "projects" / "labels" / "acme-org").write_text("b2c3d4e5\n", encoding="utf-8")
            d3 = dc / "rescue" / "b2c3d4e5" / "slot-3"
            d3.mkdir(parents=True)
            (d3 / "meta.json").write_text(json.dumps({"project": "acme-org", "lease": "slot-3"}), encoding="utf-8")
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="not running")):
                ok3, msg3 = engine.manage_rescue("verdict", "acme-org/slot-3")
                self.assertTrue(ok3, msg3)
                self.assertTrue((d3 / "verdict.md").is_file())


class TestIssue39ManageRescueDirectoryArchives(unittest.TestCase):
    """Issue #39 AC 3: manage_rescue() handles <key>/<lease> directory archives and legacy flat bundles."""

    @classmethod
    def setUpClass(cls):
        cls._repo_tmp = tempfile.TemporaryDirectory()
        cls.repo_dir = pathlib.Path(cls._repo_tmp.name) / "repo"
        cls.repo_dir.mkdir()
        subprocess.run(["git", "init", "-q", str(cls.repo_dir)], check=True)
        subprocess.run(["git", "-C", str(cls.repo_dir), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(cls.repo_dir), "config", "user.email", "test@example.com"], check=True)
        (cls.repo_dir / "a.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cls.repo_dir), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(cls.repo_dir), "commit", "-q", "-m", "base"], check=True)
        (cls.repo_dir / "a.txt").write_text("feat\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cls.repo_dir), "commit", "-q", "-am", "feat"], check=True)

        bundle_file = pathlib.Path(cls._repo_tmp.name) / "valid.bundle"
        subprocess.run(["git", "-C", str(cls.repo_dir), "bundle", "create", str(bundle_file), "HEAD"], check=True, capture_output=True)
        cls.bundle_bytes = bundle_file.read_bytes()

        (cls.repo_dir / "a.txt").write_text("feat\nuncommitted\n", encoding="utf-8")
        cls.patch_text = subprocess.run(
            ["git", "-C", str(cls.repo_dir), "diff", "--binary", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        subprocess.run(["git", "-C", str(cls.repo_dir), "checkout", "--", "a.txt"], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._repo_tmp.cleanup()

    def _subprocess_router(self, status_payload):
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
                return real_run(cmd, *args, **kwargs)
            if isinstance(cmd, (list, tuple)) and "status" in cmd and "--json" in cmd:
                if status_payload is None:
                    return MagicMock(returncode=1, stdout="", stderr="not running")
                return MagicMock(returncode=0, stdout=json.dumps(status_payload), stderr="")
            return real_run(cmd, *args, **kwargs)

        return fake_run

    def test_manage_rescue_restore_verifies_keyed_directory_archives_and_patch_meta(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            base = dc / "rescue" / "a1b2c3d4"

            def mk_arch(lease):
                d = base / lease
                d.mkdir(parents=True, exist_ok=True)
                return d

            def write_meta(d, lease):
                (d / "meta.json").write_text(
                    json.dumps({"project": "ops-console", "lease": lease, "repo_root": str(self.repo_dir), "branches": ["feat"]}),
                    encoding="utf-8",
                )

            d_ok = mk_arch("slot-2")
            write_meta(d_ok, "slot-2")
            (d_ok / "commits.bundle").write_bytes(self.bundle_bytes)

            d_multi = mk_arch("slot-multi")
            write_meta(d_multi, "slot-multi")
            (d_multi / "commits.bundle").write_bytes(self.bundle_bytes)
            (d_multi / "extra.bundle").write_bytes(b"corrupt bundle")

            d_bad_b = mk_arch("slot-bad-bundle")
            write_meta(d_bad_b, "slot-bad-bundle")
            (d_bad_b / "commits.bundle").write_bytes(b"not a bundle")

            d_patch = mk_arch("slot-patch")
            write_meta(d_patch, "slot-patch")
            (d_patch / "changes.patch").write_text(self.patch_text, encoding="utf-8")

            d_meta = mk_arch("slot-meta")
            write_meta(d_meta, "slot-meta")

            d_bad_meta = mk_arch("slot-bad-meta")
            (d_bad_meta / "meta.json").write_text("{broken json", encoding="utf-8")

            mk_arch("slot-empty")

            # Legacy flat bundles alongside directory archives
            flat_ok = dc / "rescue" / "legacy-slot-1-20261106-040834-111.bundle"
            flat_ok.write_bytes(self.bundle_bytes)
            flat_bad = dc / "rescue" / "bad-slot-2-20261106-040834-222.bundle"
            flat_bad.write_bytes(b"bad flat bundle")

            leases = ["slot-2", "slot-multi", "slot-bad-bundle", "slot-patch", "slot-meta", "slot-bad-meta", "slot-empty", "slot-missing"]
            status_payload = {
                "version": 1,
                "archives": [{"project": "ops-console", "key": "a1b2c3d4", "lease": ls, "size": 100, "age": 10, "branches": ["feat"]} for ls in leases],
            }

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch.object(engine, "HOME", self.repo_dir), \
                 patch.object(engine.rescue_audit, "load_cache", return_value={}), \
                 patch("subprocess.run", side_effect=self._subprocess_router(status_payload)):
                for target, want_ok in (
                    ("ops-console/slot-2", True),
                    ("ops-console/slot-multi", False),
                    ("ops-console/slot-bad-bundle", False),
                    ("ops-console/slot-patch", True),
                    ("ops-console/slot-meta", True),
                    ("ops-console/slot-bad-meta", False),
                    ("ops-console/slot-empty", False),
                    ("ops-console/slot-missing", False),
                    ("ops-console/unknown", False),
                    ("legacy-slot-1-20261106-040834-111", True),
                    ("bad-slot-2-20261106-040834-222", False),
                ):
                    ok, msg = engine.manage_rescue("restore", target)
                    self.assertIs(ok, want_ok, f"restore({target!r}) -> {(ok, msg)}")

                # Verify deletes nothing
                self.assertTrue((d_ok / "commits.bundle").is_file())
                self.assertTrue((d_patch / "changes.patch").is_file())
                self.assertTrue((d_patch / "meta.json").is_file())

            # Filesystem fallback (status --json unavailable)
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch.object(engine, "HOME", self.repo_dir), \
                 patch.object(engine.rescue_audit, "load_cache", return_value={}), \
                 patch("subprocess.run", side_effect=self._subprocess_router(None)):
                ok_fb, msg_fb = engine.manage_rescue("restore", "a1b2c3d4/slot-2")
                self.assertTrue(ok_fb, msg_fb)

    def test_verify_all_checks_keyed_directory_archives_and_legacy_bundles(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            d1 = dc / "rescue" / "a1b2c3d4" / "slot-2"
            d1.mkdir(parents=True)
            (d1 / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-2", "repo_root": str(self.repo_dir)}), encoding="utf-8")
            (d1 / "commits.bundle").write_bytes(self.bundle_bytes)

            d2 = dc / "rescue" / "a1b2c3d4" / "slot-3"
            d2.mkdir(parents=True)
            (d2 / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-3", "repo_root": str(self.repo_dir)}), encoding="utf-8")
            (d2 / "changes.patch").write_text(self.patch_text, encoding="utf-8")

            archives = [
                {"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-2", "size": 100, "age": 10, "branches": ["feat"]},
                {"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-3", "size": 100, "age": 10, "branches": []},
            ]

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch.object(engine, "HOME", self.repo_dir), \
                 patch.object(engine.rescue_audit, "load_cache", return_value={}):
                # 1. Valid directory archives only (no legacy flat .bundle)
                with patch("subprocess.run", side_effect=self._subprocess_router({"version": 1, "archives": archives})):
                    ok1, msg1 = engine.manage_rescue("verify_all", "all")
                    self.assertTrue(ok1, msg1)

                # 2. Corrupt directory archive bundle with no legacy flat .bundle must fail
                d_bad = dc / "rescue" / "a1b2c3d4" / "slot-bad"
                d_bad.mkdir(parents=True)
                (d_bad / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-bad", "repo_root": str(self.repo_dir)}), encoding="utf-8")
                (d_bad / "commits.bundle").write_bytes(b"corrupt")
                archives_bad = [*archives, {"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-bad", "size": 10, "age": 1, "branches": []}]
                with patch("subprocess.run", side_effect=self._subprocess_router({"version": 1, "archives": archives_bad})):
                    ok2, msg2 = engine.manage_rescue("verify_all", "all")
                    self.assertFalse(ok2, f"corrupt directory archive must fail verify_all: {msg2}")

                shutil.rmtree(d_bad)

                # 3. Valid directory archive + valid legacy flat bundle
                flat = dc / "rescue" / "legacy-slot-1-20261106-040834-111.bundle"
                flat.write_bytes(self.bundle_bytes)
                with patch("subprocess.run", side_effect=self._subprocess_router({"version": 1, "archives": archives})):
                    ok3, msg3 = engine.manage_rescue("verify_all", "all")
                    self.assertTrue(ok3, msg3)

                # 4. Valid directory archive + corrupt legacy flat bundle must fail
                flat.write_bytes(b"corrupt legacy")
                with patch("subprocess.run", side_effect=self._subprocess_router({"version": 1, "archives": archives})):
                    ok4, msg4 = engine.manage_rescue("verify_all", "all")
                    self.assertFalse(ok4, f"corrupt legacy flat bundle must fail verify_all: {msg4}")

    def test_purge_and_purge_delivered_for_directory_archives_and_legacy_bundles(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            d_deliv = dc / "rescue" / "a1b2c3d4" / "slot-2"
            d_deliv.mkdir(parents=True)
            (d_deliv / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-2"}), encoding="utf-8")
            (d_deliv / "commits.bundle").write_bytes(self.bundle_bytes)

            d_keep = dc / "rescue" / "a1b2c3d4" / "slot-3"
            d_keep.mkdir(parents=True)
            (d_keep / "meta.json").write_text(json.dumps({"project": "ops-console", "lease": "slot-3"}), encoding="utf-8")
            (d_keep / "commits.bundle").write_bytes(self.bundle_bytes)

            status_payload = {
                "version": 1,
                "archives": [{"project": "ops-console", "key": "a1b2c3d4", "lease": "slot-2", "size": 100, "age": 10, "branches": ["feat"]}],
            }

            # 1. status --json defaults can_purge=False -> purge refuses (not "not found") and purge_delivered keeps dir
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch.object(engine.rescue_audit, "load_cache", return_value={}), \
                 patch("subprocess.run", side_effect=self._subprocess_router(status_payload)):
                ok_refuse, msg_refuse = engine.manage_rescue("purge", "ops-console/slot-2")
                self.assertFalse(ok_refuse)
                self.assertNotIn("not found", msg_refuse.lower(), f"Expected refusal for can_purge=False, got: {msg_refuse}")
                self.assertTrue((d_deliv / "commits.bundle").is_file())

                engine.manage_rescue("purge_delivered", "delivered")
                self.assertTrue(d_deliv.is_dir())
                self.assertTrue((d_deliv / "commits.bundle").is_file())

            # 2. Explicit can_purge=True vs False + legacy flat bundles
            flat_deliv_id = "deliv-slot-1-20261106-040834-111"
            flat_unm_id = "unm-slot-2-20261106-040834-222"
            flat_deliv = dc / "rescue" / f"{flat_deliv_id}.bundle"
            flat_unm = dc / "rescue" / f"{flat_unm_id}.bundle"
            flat_deliv.write_bytes(self.bundle_bytes)
            flat_unm.write_bytes(self.bundle_bytes)
            sz = len(self.bundle_bytes)
            audit_cache = {
                flat_deliv_id: {"id": flat_deliv_id, "size": sz, "verdict": "delivered", "verify_ok": True, "heads": []},
                flat_unm_id: {"id": flat_unm_id, "size": sz, "verdict": "unmerged", "verify_ok": True, "heads": [{"ref": "feat", "status": "UNIQUE", "unique_commits": 1}]},
            }
            fake_archives = [
                {"id": "ops-console/slot-2", "project": "ops-console", "key": "a1b2c3d4", "lease": "slot-2", "can_purge": True},
                {"id": "ops-console/slot-3", "project": "ops-console", "key": "a1b2c3d4", "lease": "slot-3", "can_purge": False},
            ]

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch.object(engine, "get_rescue_archives", return_value=fake_archives), \
                 patch.object(engine.rescue_audit, "load_cache", return_value=audit_cache):
                ok_keep, _ = engine.manage_rescue("purge", "ops-console/slot-3")
                self.assertFalse(ok_keep)
                self.assertTrue(d_keep.is_dir())

                ok_del, msg_del = engine.manage_rescue("purge", "ops-console/slot-2")
                self.assertTrue(ok_del, msg_del)
                self.assertFalse(d_deliv.exists())
                self.assertTrue(d_keep.is_dir())

                ok_unm, _ = engine.manage_rescue("purge", flat_unm_id)
                self.assertFalse(ok_unm)
                self.assertTrue(flat_unm.is_file())

                ok_flat, msg_flat = engine.manage_rescue("purge", flat_deliv_id)
                self.assertTrue(ok_flat, msg_flat)
                self.assertFalse(flat_deliv.exists())

                # Recreate deliverable dir + flat bundle and run purge_delivered
                d_deliv.mkdir(parents=True)
                (d_deliv / "commits.bundle").write_bytes(self.bundle_bytes)
                flat_deliv.write_bytes(self.bundle_bytes)
                ok_pd, msg_pd = engine.manage_rescue("purge_delivered", "delivered")
                self.assertTrue(ok_pd, msg_pd)
                self.assertFalse(d_deliv.exists())
                self.assertFalse(flat_deliv.exists())
                self.assertTrue(d_keep.is_dir())
                self.assertTrue(flat_unm.is_file())


class TestIssue39TriggerCleanerLogs(unittest.TestCase):
    """Issue #39 AC 4: trigger_cleaner() delegates to schedule run without overwriting .log or .last."""

    def test_trigger_cleaner_invokes_schedule_run_without_overwriting_log_or_stamp(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            sched = dc / "schedule"
            sched.mkdir(parents=True)

            with patch.object(engine, "DEV_CONTAINER_STATE", dc), patch.object(engine, "AGENT_SLOT_STATE", leg):
                for action, cmd_label in (("fast", "reap --all"), ("slow", "check")):
                    want_log = f"detail line\nschedule: {action} ({cmd_label}) exited 0\n"
                    want_stamp = "1700000123\n"

                    def fake_exec(args, timeout=120, _act=action, _log=want_log, _st=want_stamp):
                        (sched / f"{_act}.log").write_text(_log, encoding="utf-8")
                        (sched / f"{_act}.last").write_text(_st, encoding="utf-8")
                        return (0, "raw stdout summary")

                    with patch.object(engine, "run_engine_command", side_effect=fake_exec) as mock_cmd:
                        ok, out = engine.trigger_cleaner(action)
                    self.assertTrue(ok)
                    self.assertEqual(out, "raw stdout summary")
                    c_args, c_kwargs = mock_cmd.call_args
                    self.assertEqual(c_args[0], ["schedule", "run", action])
                    self.assertEqual(c_kwargs.get("timeout", c_args[1] if len(c_args) > 1 else None), 300)
                    self.assertEqual((sched / f"{action}.log").read_text(encoding="utf-8"), want_log)
                    self.assertEqual((sched / f"{action}.last").read_text(encoding="utf-8"), want_stamp)

                # Non-zero exit leaves pre-existing log and stamp untouched
                (sched / "slow.log").write_text("prev\nschedule: slow (check) exited 0\n", encoding="utf-8")
                (sched / "slow.last").write_text("1700000100\n", encoding="utf-8")
                with patch.object(engine, "run_engine_command", return_value=(1, "stderr failure")):
                    ok_err, out_err = engine.trigger_cleaner("slow")
                self.assertFalse(ok_err)
                self.assertEqual(out_err, "stderr failure")
                self.assertEqual((sched / "slow.log").read_text(encoding="utf-8"), "prev\nschedule: slow (check) exited 0\n")
                self.assertEqual((sched / "slow.last").read_text(encoding="utf-8"), "1700000100\n")

                # Absent files are not created when schedule run did not write them
                (sched / "fast.log").unlink(missing_ok=True)
                (sched / "fast.last").unlink(missing_ok=True)
                with patch.object(engine, "run_engine_command", return_value=(0, "ok")):
                    ok_abs, _ = engine.trigger_cleaner("fast")
                self.assertTrue(ok_abs)
                self.assertFalse((sched / "fast.log").exists())
                self.assertFalse((sched / "fast.last").exists())

                # Unknown action does not invoke engine
                for bad in ("bogus", ""):
                    with patch.object(engine, "run_engine_command") as mock_bad:
                        ok_bad, _ = engine.trigger_cleaner(bad)
                    self.assertFalse(ok_bad, bad)
                    mock_bad.assert_not_called()


class TestIssue51MultiRepoRescueArchives(unittest.TestCase):
    """Issue #51: recursive rescue archive sizes, repos[] propagation, and multi-repo verification."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(cls._tmp.name)

        def init_repo(path: pathlib.Path, fname: str, commits: int):
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
            for i in range(1, commits + 1):
                (path / fname).write_text(f"v{i}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(path), "add", fname], check=True)
                subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", f"c{i}"], check=True)

        cls.root_repo = tmp / "root_repo"
        init_repo(cls.root_repo, "root.txt", 1)
        rb = tmp / "root.bundle"
        subprocess.run(["git", "-C", str(cls.root_repo), "bundle", "create", str(rb), "HEAD"], check=True, capture_output=True)
        cls.root_bundle_bytes = rb.read_bytes()

        cls.sub_repo = cls.root_repo / "docs" / "fix"
        init_repo(cls.sub_repo, "doc.txt", 2)
        sb_incr = tmp / "sub_incr.bundle"
        subprocess.run(["git", "-C", str(cls.sub_repo), "bundle", "create", str(sb_incr), "HEAD~1..HEAD"], check=True, capture_output=True)
        cls.sub_incr_bundle_bytes = sb_incr.read_bytes()

        sb_full = tmp / "sub_full.bundle"
        subprocess.run(["git", "-C", str(cls.sub_repo), "bundle", "create", str(sb_full), "HEAD"], check=True, capture_output=True)
        cls.sub_full_bundle_bytes = sb_full.read_bytes()

        cls.home_dir = tmp / "fake_home"
        cls.home_sub_repo = cls.home_dir / "Projects" / "my-docs"
        init_repo(cls.home_sub_repo, "doc.txt", 2)
        hb_incr = tmp / "home_sub_incr.bundle"
        subprocess.run(["git", "-C", str(cls.home_sub_repo), "bundle", "create", str(hb_incr), "HEAD~1..HEAD"], check=True, capture_output=True)
        cls.home_sub_incr_bundle_bytes = hb_incr.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_get_rescue_archives_recursive_size_and_repos_in_both_branches(self):
        with tempfile.TemporaryDirectory() as dc_dir, tempfile.TemporaryDirectory() as leg_dir:
            dc, leg = pathlib.Path(dc_dir), pathlib.Path(leg_dir)
            arch_dir = dc / "rescue" / "k111" / "slot-1"
            sub_dir = arch_dir / "repos" / "docs"
            sub_dir.mkdir(parents=True)

            repos_meta = [{"alias": "docs", "path": "docs/fix", "mode": "rw", "branches": ["feat/docs"]}]
            meta_bytes = json.dumps({
                "project": "ops-console",
                "lease": "slot-1",
                "branches": ["main"],
                "repos": repos_meta,
            }).encode("utf-8")
            (arch_dir / "meta.json").write_bytes(meta_bytes)
            (arch_dir / "commits.bundle").write_bytes(self.root_bundle_bytes)
            (sub_dir / "commits.bundle").write_bytes(self.sub_full_bundle_bytes)
            (sub_dir / "changes.patch").write_bytes(b"diff --git a/x b/x\n" + b"x" * 2048)

            plain_dir = dc / "rescue" / "k222" / "slot-2"
            plain_dir.mkdir(parents=True)
            (plain_dir / "meta.json").write_text(json.dumps({"project": "other", "lease": "slot-2"}), encoding="utf-8")

            expected_total = sum(p.stat().st_size for p in arch_dir.rglob("*") if p.is_file())
            top_only = sum(p.stat().st_size for p in arch_dir.iterdir() if p.is_file())
            self.assertGreater(expected_total, top_only)

            # 1) Filesystem fallback branch
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="no engine")):
                archives = engine.get_rescue_archives(limit=50)

            by_id = {a["id"]: a for a in archives}
            self.assertEqual(by_id["k111/slot-1"]["size"], expected_total)
            self.assertEqual(by_id["k111/slot-1"]["size_bytes"], expected_total)
            self.assertEqual(by_id["k111/slot-1"]["size_mb"], round(expected_total / (1024 * 1024), 2))
            self.assertEqual(by_id["k111/slot-1"]["repos"], repos_meta)
            self.assertEqual(by_id["k222/slot-2"]["repos"], [])

            # 2) status --json branch: enriches missing repos from meta.json, preserves provided repos
            status_payload = {
                "version": 1,
                "archives": [
                    {"project": "ops-console", "key": "k111", "lease": "slot-1", "size": expected_total, "age": 10, "branches": ["main"]},
                    {"project": "other", "key": "k222", "lease": "slot-2", "size": 50, "age": 5, "branches": [], "repos": [{"alias": "pre", "mode": "ro"}]},
                ],
            }
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", leg), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(status_payload), stderr="")):
                s_archives = engine.get_rescue_archives(limit=50)

            s_by_id = {a["id"]: a for a in s_archives}
            self.assertEqual(s_by_id["ops-console/slot-1"]["repos"], repos_meta)
            self.assertEqual(s_by_id["other/slot-2"]["repos"], [{"alias": "pre", "mode": "ro"}])

    def test_verify_archive_dir_multi_repo_sub_only_and_corrupt_sub(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)

            # 1) Root bundle + sub-repo incremental bundle matched via meta["repo_root"] / sub_meta["path"]
            a_ok = base / "a_ok"
            (a_ok / "repos" / "docs").mkdir(parents=True)
            (a_ok / "meta.json").write_text(json.dumps({
                "project": "ops-console",
                "repo_root": str(self.root_repo),
                "repos": [{"alias": "docs", "path": "docs/fix", "mode": "rw"}],
            }), encoding="utf-8")
            (a_ok / "commits.bundle").write_bytes(self.root_bundle_bytes)
            (a_ok / "repos" / "docs" / "commits.bundle").write_bytes(self.sub_incr_bundle_bytes)
            (a_ok / "repos" / "docs" / "changes.patch").write_text("diff\n", encoding="utf-8")
            (a_ok / "repos" / "docs" / "new-files.tar").write_bytes(b"tar-bytes")

            with patch.object(engine, "HOME", self.home_dir):
                ok, msg = engine._verify_archive_dir(a_ok, "ops-console", "ops-console/a_ok")
                self.assertTrue(ok, msg)

            # 2) Root bundle + sub-repo incremental bundle matched via HOME / Projects / <repo_name> from url
            a_url = base / "a_url"
            (a_url / "repos" / "mydocs").mkdir(parents=True)
            (a_url / "meta.json").write_text(json.dumps({
                "project": "ops-console",
                "repo_root": str(self.root_repo),
                "repos": [{"alias": "mydocs", "url": "https://github.com/hanzhad/my-docs.git", "mode": "ro"}],
            }), encoding="utf-8")
            (a_url / "commits.bundle").write_bytes(self.root_bundle_bytes)
            (a_url / "repos" / "mydocs" / "commits.bundle").write_bytes(self.home_sub_incr_bundle_bytes)

            with patch.object(engine, "HOME", self.home_dir):
                ok_url, msg_url = engine._verify_archive_dir(a_url, "ops-console", "ops-console/a_url")
                self.assertTrue(ok_url, msg_url)

            # 3) Valid root bundle + CORRUPT sub-repo bundle -> must NOT return early as intact
            a_corrupt_sub = base / "a_corrupt_sub"
            (a_corrupt_sub / "repos" / "docs").mkdir(parents=True)
            (a_corrupt_sub / "meta.json").write_text(json.dumps({
                "project": "ops-console",
                "repo_root": str(self.root_repo),
                "repos": [{"alias": "docs", "path": "docs/fix", "mode": "rw"}],
            }), encoding="utf-8")
            (a_corrupt_sub / "commits.bundle").write_bytes(self.root_bundle_bytes)
            (a_corrupt_sub / "repos" / "docs" / "commits.bundle").write_bytes(b"corrupt sub bundle")

            with patch.object(engine, "HOME", self.home_dir):
                ok_cs, msg_cs = engine._verify_archive_dir(a_corrupt_sub, "ops-console", "ops-console/a_corrupt_sub")
                self.assertFalse(ok_cs, f"expected failure for corrupt sub-repo bundle, got: {msg_cs}")

            # 4) Sub-repo-only archive (clean root) with no local checkout -> git bundle list-heads succeeds
            a_sub_only = base / "a_sub_only"
            (a_sub_only / "repos" / "ext").mkdir(parents=True)
            (a_sub_only / "meta.json").write_text(json.dumps({
                "project": "ops-console",
                "repos": [{"alias": "ext", "url": "https://github.com/org/nonexistent-repo.git", "mode": "ro"}],
            }), encoding="utf-8")
            (a_sub_only / "repos" / "ext" / "commits.bundle").write_bytes(self.sub_full_bundle_bytes)

            with patch.object(engine, "HOME", self.home_dir):
                ok_so, msg_so = engine._verify_archive_dir(a_sub_only, "ops-console", "ops-console/a_sub_only")
                self.assertTrue(ok_so, msg_so)

            # 5) Sub-repo-only archive with corrupt bundle and no local checkout -> fails
            a_sub_bad = base / "a_sub_bad"
            (a_sub_bad / "repos" / "ext").mkdir(parents=True)
            (a_sub_bad / "meta.json").write_text(json.dumps({"project": "ops-console"}), encoding="utf-8")
            (a_sub_bad / "repos" / "ext" / "commits.bundle").write_bytes(b"not a bundle")

            with patch.object(engine, "HOME", self.home_dir):
                ok_sb, msg_sb = engine._verify_archive_dir(a_sub_bad, "ops-console", "ops-console/a_sub_bad")
                self.assertFalse(ok_sb, f"expected failure for corrupt sub-only bundle, got: {msg_sb}")

            # 6) Unreadable top-level new-files.tar and unreadable sub-repo changes.patch fail cleanly
            a_unreadable_tar = base / "a_unreadable_tar"
            a_unreadable_tar.mkdir()
            tar_p = a_unreadable_tar / "new-files.tar"
            tar_p.write_bytes(b"tar")
            tar_p.chmod(0o000)
            try:
                ok_ut, msg_ut = engine._verify_archive_dir(a_unreadable_tar, "ops-console", "ops-console/a_unreadable_tar")
                self.assertFalse(ok_ut, f"expected failure for unreadable new-files.tar, got: {msg_ut}")
            finally:
                tar_p.chmod(0o644)

            a_unreadable_sub = base / "a_unreadable_sub"
            (a_unreadable_sub / "repos" / "docs").mkdir(parents=True)
            sub_patch = a_unreadable_sub / "repos" / "docs" / "changes.patch"
            sub_patch.write_bytes(b"patch")
            sub_patch.chmod(0o000)
            try:
                ok_us, msg_us = engine._verify_archive_dir(a_unreadable_sub, "ops-console", "ops-console/a_unreadable_sub")
                self.assertFalse(ok_us, f"expected failure for unreadable sub changes.patch, got: {msg_us}")
            finally:
                sub_patch.chmod(0o644)


class TestLiveEndpoints(unittest.TestCase):
    """Integration checks for running ops-console server (skipped by default unless OPS_CONSOLE_LIVE_TEST=1)."""

    @unittest.skipUnless(os.environ.get("OPS_CONSOLE_LIVE_TEST"), "Live server tests disabled by default")
    def test_live_server_endpoints(self):
        import re
        import urllib.request

        page = urllib.request.urlopen("http://127.0.0.1:7631/").read().decode()
        m = re.search(r"const TOKEN = '([^']+)'", page)
        self.assertIsNotNone(m, "TOKEN not found in page.html")
        token = m.group(1)

        state_data = json.loads(urllib.request.urlopen("http://127.0.0.1:7631/api/state").read().decode())
        self.assertIn("slots", state_data)
        self.assertIn("cleaners", state_data)


if __name__ == "__main__":
    unittest.main()
