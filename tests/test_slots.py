#!/usr/bin/env python3
"""Tests for bin/slots.py, bin/projects.py, and bin/evict.py: lease dating, project discovery, evict CLI, and slot telemetry."""

import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import engine  # noqa: E402
import evict  # noqa: E402
import projects  # noqa: E402
import rescue_archives  # noqa: E402
import rescue_audit  # noqa: E402
import slots  # noqa: E402


class TestWithSince(unittest.TestCase):
    def test_keeps_a_since_the_lease_has(self):
        lease = {"since": 5.0}
        self.assertIs(slots.with_since(lease, 99.0), lease)

    def test_uses_the_file_mtime_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d, "p-slot-1.json")
            f.write_text(json.dumps({}))
            os.utime(f, (1234.0, 1234.0))
            self.assertEqual(slots.with_since({"_file": str(f)}, 99.0)["since"], 1234.0)

    def test_falls_back_to_the_pass_time(self):
        self.assertEqual(slots.with_since({}, 99.0)["since"], 99.0)
        self.assertEqual(slots.with_since({"_file": "/nonexistent/x.json"}, 99.0)["since"], 99.0)

    def test_leased_entry_is_dated_abandoned_entry_is_not(self):
        e = slots.build_slot(slots.with_since({"project": "p-slot-1", "ttl_seconds": 10}, 100.0), None, {}, None, None)
        self.assertEqual((e["since"], e["ttl_until"]), (100.0, 110.0))
        self.assertIsNone(slots.abandoned_slot("p", 1, {}, None, None)["since"])


class TestDiscoverSlotsDatesLeases(unittest.TestCase):
    def test_a_lease_without_since_is_dated_from_its_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d, "p-slot-1.json")
            f.write_text("{}")
            os.utime(f, (1234.0, 1234.0))
            lease = {"project": "p-slot-1", "ttl_seconds": 10, "_file": str(f)}
            with patch.object(slots.projects_mod, "discover", return_value={}), \
                    patch.object(slots.engine_mod, "is_engine_available", return_value=True):
                out = slots.discover_slots({}, [], leases=[lease])
        self.assertEqual((out[0]["since"], out[0]["ttl_until"]), (1234.0, 1244.0))


class TestProjectsDiscovery(unittest.TestCase):
    def test_discovers_registered_project_env(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            (state / "projects").mkdir(parents=True)
            repo = tmp / "myrepo"
            (repo / "bin").mkdir(parents=True)
            (repo / "bin" / "dev-container").write_text("#!/bin/sh\n")
            (repo / "project.env").write_text("PROJECT=myproj\nSLOT_PORTS=SSH_PORT DB_PORT\n")
            (state / "projects" / "abc.json").write_text(json.dumps({"root": str(repo), "label": "myproj"}))
            with patch.object(projects, "STATE", state), patch.object(projects, "HOME", tmp), \
                 patch.object(projects, "main_of", return_value=repo), patch.object(projects, "_repo_of", return_value="o/r"):
                out = projects.discover()
            self.assertIn("myproj", out)
            self.assertEqual(out["myproj"]["repo"], "o/r")
            self.assertEqual(sorted(out["myproj"]["ports"]), ["db", "ssh"])
            self.assertEqual(out["myproj"]["script"], repo / "bin" / "dev-container")

    def test_read_engine_project_returns_none_without_project_env(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            (state / "projects").mkdir(parents=True)

            phantom = tmp / "dev-container-engine"
            (phantom / "bin").mkdir(parents=True)
            (phantom / "bin" / "dev-container").write_text("#!/bin/sh\n")

            real = tmp / "agent-dev-slots"
            (real / "bin").mkdir(parents=True)
            (real / "bin" / "dev-container").write_text("#!/bin/sh\n")
            (real / "project.env").write_text("PROJECT=agent-dev-slots\nSLOT_PORTS=SSH_PORT\n")

            with patch.object(projects, "_repo_of", return_value="o/r"):
                self.assertIsNone(
                    projects.read_engine_project(phantom, meta={"root": str(phantom), "label": "dev-container-engine"})
                )
                (phantom / "project.env").mkdir()
                self.assertIsNone(projects.read_engine_project(phantom))

            (state / "projects" / "21ebd893.json").write_text(
                json.dumps({"root": str(phantom), "label": "dev-container-engine"})
            )
            (state / "projects" / "aaaa1111.json").write_text(
                json.dumps({"root": str(real), "label": "agent-dev-slots"})
            )
            with patch.object(projects, "STATE", state), patch.object(projects, "HOME", tmp), \
                 patch.object(projects, "main_of", side_effect=lambda p: p), \
                 patch.object(projects, "_repo_of", return_value="o/r"):
                out = projects.discover()
            self.assertNotIn("dev-container-engine", out)
            self.assertIn("agent-dev-slots", out)

    def test_read_engine_project_selects_thin_engine_for_thin_wrapper_or_engine_root(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            thin = tmp / "shared-engine" / "scripts" / "dev-container-thin"

            repo_thin = tmp / "proj-with-thin"
            (repo_thin / "bin").mkdir(parents=True)
            (repo_thin / "scripts").mkdir(parents=True)
            (repo_thin / "bin" / "dev-container").write_text("#!/usr/bin/env bash\n")
            (repo_thin / "scripts" / "dev-container-thin").write_text("#!/bin/sh\n")
            (repo_thin / "project.env").write_text("PROJECT=ads\nSLOT_PORTS=SSH_PORT\n")

            eng_root = tmp / "engine-root"
            (eng_root / "bin").mkdir(parents=True)
            (eng_root / "bin" / "dev-container").write_text("#!/usr/bin/env bash\n")
            (eng_root / "project.env").write_text("PROJECT=eng\nSLOT_PORTS=SSH_PORT\n")
            eng_link = tmp / "engine-link"
            eng_link.symlink_to(eng_root)
            eng_thin = eng_root / "scripts" / "dev-container-thin"

            normal = tmp / "normal-proj"
            (normal / "bin").mkdir(parents=True)
            (normal / "bin" / "dev-container").write_text("#!/bin/sh\n")
            (normal / "project.env").write_text("PROJECT=normal\nSLOT_PORTS=SSH_PORT\n")

            with patch.object(projects, "_repo_of", return_value="o/r"):
                with patch.object(projects, "THIN_ENGINE", thin):
                    got_thin = projects.read_engine_project(repo_thin)
                    got_normal = projects.read_engine_project(normal)
                self.assertEqual(got_thin["script"], thin)
                self.assertEqual(got_normal["script"], normal / "bin" / "dev-container")

                with patch.object(projects, "THIN_ENGINE", eng_thin):
                    got_root = projects.read_engine_project(eng_link)
                self.assertEqual(got_root["script"], eng_thin)


class TestEvictCommand(unittest.TestCase):
    def test_evict_uses_project_dev_container_cli(self):
        with tempfile.TemporaryDirectory() as d:
            repo = pathlib.Path(d)
            script = repo / "bin" / "dev-container"
            proj = {"myproj": {"prefix": "myproj", "main": repo, "slots_dir": repo / "dev-containers", "script": script}}
            res = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
            with patch.object(evict.projects, "discover", return_value=proj), \
                 patch.object(evict.subprocess, "run", return_value=res) as run_mock, \
                 patch.object(sys, "argv", ["evict.py", "myproj-slot-2", "release"]):
                with self.assertRaises(SystemExit) as cm:
                    evict.main()
            self.assertEqual(cm.exception.code, 0)
            cmd = run_mock.call_args.args[0][2]
            self.assertIn(f"{script} evict slot-2 --yes", cmd)

    def test_resolve_slot_selects_thin_engine_for_thin_wrapper_or_engine_root(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            (state / "leases").mkdir(parents=True)
            thin = tmp / "shared-engine" / "scripts" / "dev-container-thin"

            repo_thin = tmp / "proj-with-thin"
            (repo_thin / "bin").mkdir(parents=True)
            (repo_thin / "scripts").mkdir(parents=True)
            (repo_thin / "bin" / "dev-container").write_text("#!/usr/bin/env bash\n")
            (repo_thin / "scripts" / "dev-container-thin").write_text("#!/bin/sh\n")
            (state / "leases" / "ads-slot-2.json").write_text(json.dumps({"repo_root": str(repo_thin), "holder": "h"}))

            eng_root = tmp / "engine-root"
            (eng_root / "bin").mkdir(parents=True)
            (eng_root / "bin" / "dev-container").write_text("#!/usr/bin/env bash\n")
            eng_link = tmp / "engine-link"
            eng_link.symlink_to(eng_root)
            eng_thin = eng_root / "scripts" / "dev-container-thin"
            (state / "leases" / "eng-slot-1.json").write_text(json.dumps({"repo_root": str(eng_link), "holder": "h"}))

            normal = tmp / "normal-proj"
            (normal / "bin").mkdir(parents=True)
            (normal / "bin" / "dev-container").write_text("#!/bin/sh\n")
            (state / "leases" / "norm-slot-3.json").write_text(json.dumps({"repo_root": str(normal), "holder": "h"}))

            with patch.object(evict.projects, "discover", return_value={}), \
                 patch.object(evict, "STATE", state):
                with patch.object(evict, "THIN_ENGINE", thin):
                    p_thin, n_thin = evict._resolve_slot("ads-slot-2")
                    p_norm, n_norm = evict._resolve_slot("norm-slot-3")
                self.assertEqual((n_thin, p_thin["script"]), (2, thin))
                self.assertEqual((n_norm, p_norm["script"]), (3, normal / "bin" / "dev-container"))

                with patch.object(evict, "THIN_ENGINE", eng_thin):
                    p_eng, n_eng = evict._resolve_slot("eng-slot-1")
                self.assertEqual((n_eng, p_eng["script"]), (1, eng_thin))

    def test_release_with_live_lease_invokes_release_for_holder(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            (state / "leases").mkdir(parents=True)
            repo = tmp / "repo"
            for script in (repo / "bin" / "dev-container", repo / "scripts" / "dev-container-thin"):
                proj = {"myproj": {"prefix": "myproj", "main": repo, "slots_dir": tmp / "dev-containers", "script": script}}
                res = subprocess.CompletedProcess([], 0, stdout="released", stderr="")
                for holder in ("issue-38", "agent one;id"):
                    (state / "leases" / "myproj-slot-2.json").write_text(json.dumps({"holder": holder}))
                    with patch.object(evict, "STATE", state), \
                         patch.object(evict.projects, "discover", return_value=proj), \
                         patch.object(evict.subprocess, "run", return_value=res) as run_mock, \
                         patch.object(sys, "argv", ["evict.py", "myproj-slot-2", "release"]):
                        with self.assertRaises(SystemExit) as cm:
                            evict.main()
                    self.assertEqual(cm.exception.code, 0)
                    cmd = run_mock.call_args.args[0][2]
                    self.assertIn(f"{shlex.quote(str(script))} release slot-2 --for {shlex.quote(holder)}", cmd)
                    self.assertNotIn("evict slot-2", cmd)

    def test_release_fallback_force_rescue_remove_and_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            (state / "leases").mkdir(parents=True)
            repo = tmp / "repo"
            script = repo / "bin" / "dev-container"
            proj = {"myproj": {"prefix": "myproj", "main": repo, "slots_dir": tmp / "dev-containers", "script": script}}
            res = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
            lease_file = state / "leases" / "myproj-slot-2.json"

            for payload in (None, "", "{bad", json.dumps({}), json.dumps({"holder": ""}), json.dumps({"holder": None}), json.dumps({"holder": 123})):
                if payload is None:
                    lease_file.unlink(missing_ok=True)
                else:
                    lease_file.write_text(payload)
                with patch.object(evict, "STATE", state), \
                     patch.object(evict.projects, "discover", return_value=proj), \
                     patch.object(evict.subprocess, "run", return_value=res) as run_mock, \
                     patch.object(sys, "argv", ["evict.py", "myproj-slot-2", "release"]):
                    with self.assertRaises(SystemExit) as cm:
                        evict.main()
                self.assertEqual(cm.exception.code, 0)
                cmd = run_mock.call_args.args[0][2]
                self.assertIn(f"{shlex.quote(str(script))} evict slot-2 --yes", cmd)
                self.assertNotIn("release slot-2", cmd)

            lease_file.write_text(json.dumps({"holder": "issue-38"}))
            for verb, expected in (
                ("force", f"{shlex.quote(str(script))} evict slot-2 --yes --force"),
                ("rescue", f"{shlex.quote(str(script))} evict slot-2 --yes"),
                ("remove", f"{shlex.quote(str(script))} down slot-2"),
            ):
                with patch.object(evict, "STATE", state), \
                     patch.object(evict.projects, "discover", return_value=proj), \
                     patch.object(evict.subprocess, "run", return_value=res) as run_mock, \
                     patch.object(sys, "argv", ["evict.py", "myproj-slot-2", verb]):
                    with self.assertRaises(SystemExit) as cm:
                        evict.main()
                self.assertEqual(cm.exception.code, 0)
                cmd = run_mock.call_args.args[0][2]
                self.assertIn(expected, cmd)

            with patch.object(evict, "STATE", state), \
                 patch.object(evict.projects, "discover", return_value=proj), \
                 patch.object(evict.subprocess, "run", return_value=res) as run_mock, \
                 patch.object(sys, "argv", ["evict.py", "myproj-slot-2", "release"]):
                with self.assertRaises(SystemExit) as cm:
                    evict.main()
            self.assertEqual(cm.exception.code, 0)
            cmd = run_mock.call_args.args[0][2]
            self.assertIn(f"{shlex.quote(str(script))} release slot-2 --for issue-38", cmd)


class TestSlotsTelemetry(unittest.TestCase):
    def test_reads_ports_activity_and_gateway_stats(self):
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "ports").mkdir()
            (state / "activity").mkdir()
            (state / "ports" / "acme-org-slot-1.ports").write_text("SSH_PORT=42016\nDB_PORT=42017\n")
            (state / "activity" / "acme-org-slot-1.run").write_text("2500\n")
            (state / "activity" / "acme-org-slot-1.cpu").write_text("2600\n")
            lease = {"project": "acme-org-slot-1", "holder": "99", "since": 1000, "ttl_seconds": 60, "ssh_port": 42016}
            stats = {
                "acme-org-slot-1-app-1": [5.0, 100.0],
                "acme-org-slot-1-db-1": [2.0, 50.0],
                "acme-org-slot-1-gateway-1": [1.5, 12.5],
            }
            with patch.object(slots, "DEV_CONTAINER_STATE", state):
                s = slots.build_slot(lease, None, stats, "main", "abc1234", now=5000.0)
            self.assertEqual(s["ports"], {"ssh": 42016, "db": 42017})
            self.assertEqual(s["last_activity"], 2600)
            self.assertEqual((s["cpu"], s["mem_mb"], s["running"]), (8.5, 162, True))

    def test_leased_reads_branch_and_head_from_container_when_volume_backed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            lease = {"project": "acme-org-slot-1", "holder": "99", "since": 1000, "ttl_seconds": 60}
            projs = {"acme-org": {"prefix": "acme-org", "slots_dir": tmp / "missing", "repo": "o/r", "main": tmp}}
            stats = {"acme-org-slot-1-app-1": [1.0, 64.0]}
            with patch.object(slots, "DEV_CONTAINER_STATE", tmp), \
                 patch.object(slots, "sh", return_value="feat-branch\n---\nabc1234\n") as sh_mock:
                res = slots._leased(lease, None, stats, projs)
            self.assertEqual((res["branch"], res["head"]), ("feat-branch", "abc1234"))
            cmd_args = sh_mock.call_args.args[0]
            self.assertIn("safe.directory=/workspace", cmd_args[-1])

    def test_claims_for_runs_gh_issue_view_from_home_dir(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(slots, "CACHE", pathlib.Path(d)), \
                patch.object(slots, "sh", return_value="") as sh_mock:
            slots.claims_for("hanzhad/ops-console", "/some/checkout/path", 63)
        self.assertTrue(sh_mock.called)
        cmd_str = sh_mock.call_args.args[0][2]
        self.assertIn("cd ~ && gh issue view 63 -R hanzhad/ops-console", cmd_str)
        self.assertNotIn("/some/checkout/path", cmd_str)


class TestParseSlotId(unittest.TestCase):
    """Issue #61: pure parse_slot_id(sid, lease=None) across structured, 0-slot-<N>-<project>, and legacy formats."""

    def test_structured_lease_fields_take_precedence(self):
        self.assertTrue(hasattr(projects, "parse_slot_id"), "bin/projects.py must expose parse_slot_id")
        cases = [
            ("0-slot-5-host_repo", {"project_label": "host_repo", "slot_num": 5}, ("host_repo", 5)),
            ("0-slot-5-other", {"project_label": "custom-proj", "slot_num": 7}, ("custom-proj", 7)),
            ("arbitrary-id", {"project_label": "ops-console", "slot_num": 0}, ("ops-console", 0)),
            ("legacy-slot-2", {"project_label": "newproj", "slot_num": 9}, ("newproj", 9)),
        ]
        for sid, lease, want in cases:
            with self.subTest(sid=sid, lease=lease):
                self.assertEqual(projects.parse_slot_id(sid, lease=lease), want)

    def test_invalid_or_partial_structured_fields_fall_back_to_sid(self):
        self.assertTrue(hasattr(projects, "parse_slot_id"), "bin/projects.py must expose parse_slot_id")
        cases = [
            ("0-slot-5-host_repo", None, ("host_repo", 5)),
            ("0-slot-5-host_repo", "not-a-dict", ("host_repo", 5)),
            ("0-slot-5-host_repo", {}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": "host_repo"}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"slot_num": 5}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": "", "slot_num": 5}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": "   ", "slot_num": 5}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": None, "slot_num": 5}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": "host_repo", "slot_num": None}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": "host_repo", "slot_num": "5"}, ("host_repo", 5)),
            ("0-slot-5-host_repo", {"project_label": "host_repo", "slot_num": True}, ("host_repo", 5)),
            ("host_repo-slot-5", {"project_label": "", "slot_num": 9}, ("host_repo", 5)),
        ]
        for sid, lease, want in cases:
            with self.subTest(sid=sid, lease=lease):
                self.assertEqual(projects.parse_slot_id(sid, lease=lease), want)

    def test_new_and_legacy_sid_formats_and_negative_boundaries(self):
        self.assertTrue(hasattr(projects, "parse_slot_id"), "bin/projects.py must expose parse_slot_id")
        valid = [
            ("0-slot-5-host_repo", ("host_repo", 5)),
            ("0-slot-12-demo-app-ai", ("demo-app-ai", 12)),
            ("0-slot-1-ops-console", ("ops-console", 1)),
            ("0-slot-0-proj", ("proj", 0)),
            ("host_repo-slot-5", ("host_repo", 5)),
            ("demo-app-ai-slot-16", ("demo-app-ai", 16)),
            ("ops-console-slot-3", ("ops-console", 3)),
        ]
        for sid, want in valid:
            with self.subTest(sid=sid):
                self.assertEqual(projects.parse_slot_id(sid), want)

        invalid = [
            "",
            None,
            "slot-5",
            "0-slot--proj",
            "0-slot-abc-proj",
            "0-slot-5-",
            "-slot-5",
            "host_repo-slot-abc",
            "host_repo-slot-",
            "random-id",
        ]
        for sid in invalid:
            with self.subTest(invalid_sid=sid):
                self.assertIsNone(projects.parse_slot_id(sid))


class TestProjectsDualFormatAndLegacyRemoval(unittest.TestCase):
    """Issue #61: projects.slot_of, can_take, and removal of agent-slot.sh / $STATE/idle."""

    def test_slot_of_accepts_new_and_legacy_formats(self):
        projs = {"myproj": {"prefix": "myproj"}, "ops-console": {"prefix": "ops-console"}}
        self.assertEqual(projects.slot_of(projs, "0-slot-5-myproj"), (projs["myproj"], 5))
        self.assertEqual(projects.slot_of(projs, "myproj-slot-5"), (projs["myproj"], 5))
        self.assertEqual(projects.slot_of(projs, "0-slot-2-ops-console"), (projs["ops-console"], 2))
        self.assertIsNone(projects.slot_of(projs, "0-slot-5-unknown"))
        self.assertIsNone(projects.slot_of(projs, "unknown-slot-5"))
        self.assertIsNone(projects.slot_of(projs, "not-a-slot"))

    def test_can_take_counts_both_new_and_legacy_leases(self):
        with tempfile.TemporaryDirectory() as d:
            p = {
                "prefix": "myproj",
                "slots_dir": pathlib.Path(d) / "missing",
                "numbers": 5,
                "model": "engine",
                "ports": {},
                "port_file": ".ports",
            }
            leases = {"0-slot-2-myproj", "myproj-slot-3", "0-slot-4-other", "other-slot-1"}
            res = projects.can_take(p, leases, folders={}, running=set(), cap=20, taken_ports=set())
        self.assertEqual(res["free"], [1, 4, 5])
        self.assertEqual(res["count"], 3)

    def test_discover_ignores_legacy_agent_slot_sh_checkouts(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            state.mkdir()
            legacy = tmp / "Projects" / "legacy-proj"
            (legacy / "scripts" / "manual").mkdir(parents=True)
            (legacy / "scripts" / "manual" / "agent-slot.sh").write_text(
                "SLOT_PROJECT_PREFIX=${SLOT_PROJECT_PREFIX:-legacy-proj}\nSLOT_INDEX_CEILING=5\n"
            )
            with patch.object(projects, "STATE", state), patch.object(projects, "HOME", tmp), \
                 patch.object(projects, "main_of", return_value=legacy), patch.object(projects, "_repo_of", return_value="o/r"):
                out = projects.discover()
            self.assertEqual(out, {})

    def test_projects_module_has_no_idle_or_agent_slot_fallback(self):
        src = (ROOT / "bin" / "projects.py").read_text(encoding="utf-8")
        self.assertNotIn("agent-slot.sh", src)
        self.assertNotRegex(src, r"""STATE\s*/\s*["']idle["']""")


class TestSlotsDualFormatAndSorting(unittest.TestCase):
    """Issue #61: bin/slots.py parses both formats + structured leases and sorts by (slot, project)."""

    def test_build_slot_handles_new_legacy_and_structured_leases(self):
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            with patch.object(slots, "DEV_CONTAINER_STATE", state):
                s_new = slots.build_slot({"project": "0-slot-5-host_repo", "holder": "h", "since": 100}, None, {}, "main", "abc")
                s_leg = slots.build_slot({"project": "host_repo-slot-4", "holder": "h", "since": 100}, None, {}, "main", "abc")
                s_str = slots.build_slot(
                    {"project": "opaque-id", "project_label": "ops-console", "slot_num": 7, "holder": "h", "since": 100},
                    None, {}, "main", "abc",
                )
        self.assertEqual((s_new["id"], s_new["project"], s_new["slot"]), ("0-slot-5-host_repo", "host_repo", 5))
        self.assertEqual((s_leg["id"], s_leg["project"], s_leg["slot"]), ("host_repo-slot-4", "host_repo", 4))
        self.assertEqual((s_str["id"], s_str["project"], s_str["slot"]), ("opaque-id", "ops-console", 7))

    def test_leased_resolves_project_and_slot_dir_for_new_format(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            slot_dir = tmp / "dev-containers" / "slot-5"
            slot_dir.mkdir(parents=True)
            lease = {"project": "0-slot-5-host_repo", "holder": "h", "since": 100}
            projs = {"host_repo": {"prefix": "host_repo", "slots_dir": tmp / "dev-containers", "repo": "o/r", "main": tmp}}
            with patch.object(slots, "DEV_CONTAINER_STATE", tmp), \
                 patch.object(slots, "_git", side_effect=["feat-x", "deadbeef"]) as git_mock:
                res = slots._leased(lease, None, {}, projs)
            self.assertEqual((res["project"], res["slot"], res["branch"], res["head"]), ("host_repo", 5, "feat-x", "deadbeef"))
            self.assertEqual(git_mock.call_args_list[0].args[0], slot_dir)

    def test_discover_slots_sorts_by_slot_then_project(self):
        leases = [
            {"project": "0-slot-10-alpha", "holder": "h1", "since": 100},
            {"project": "beta-slot-2", "holder": "h2", "since": 100},
            {"project": "0-slot-2-alpha", "holder": "h3", "since": 100},
            {"project": "0-slot-1-zeta", "holder": "h4", "since": 100},
        ]
        with patch.object(slots.projects_mod, "discover", return_value={}), \
             patch.object(slots.engine_mod, "is_engine_available", return_value=True):
            out = slots.discover_slots({}, [], leases=leases)
        self.assertEqual([(s["slot"], s["project"]) for s in out], [(1, "zeta"), (2, "alpha"), (2, "beta"), (10, "alpha")])


class TestEvictDualFormatAndLegacyRemoval(unittest.TestCase):
    """Issue #61: bin/evict.py resolves 0-slot-<N>-<project> and drops agent-slot.sh legacy handling."""

    def test_resolve_slot_and_main_accept_new_format_and_structured_lease(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            state = tmp / "state"
            (state / "leases").mkdir(parents=True)
            repo = tmp / "host_repo"
            (repo / "bin").mkdir(parents=True)
            script = repo / "bin" / "dev-container"
            script.write_text("#!/bin/sh\n")
            (state / "leases" / "0-slot-5-host_repo.json").write_text(
                json.dumps({"repo_root": str(repo), "holder": "issue-61", "project_label": "host_repo", "slot_num": 5})
            )
            res = subprocess.CompletedProcess([], 0, stdout="released", stderr="")
            with patch.object(evict, "STATE", state), \
                 patch.object(evict.projects, "discover", return_value={}):
                found = evict._resolve_slot("0-slot-5-host_repo")
                self.assertIsNotNone(found, "evict._resolve_slot must resolve 0-slot-<N>-<project>")
                p, num = found
                self.assertEqual((p["prefix"], num), ("host_repo", 5))
                with patch.object(evict.subprocess, "run", return_value=res) as run_mock, \
                     patch.object(sys, "argv", ["evict.py", "0-slot-5-host_repo", "release"]):
                    with self.assertRaises(SystemExit) as cm:
                        evict.main()
                self.assertEqual(cm.exception.code, 0)
                self.assertIn(f"{shlex.quote(str(script))} release slot-5 --for issue-61", run_mock.call_args.args[0][2])

    def test_evict_module_has_no_agent_slot_legacy_branch(self):
        src = (ROOT / "bin" / "evict.py").read_text(encoding="utf-8")
        self.assertNotIn("agent-slot.sh", src)
        self.assertNotIn(".agent-slots/env", src)


class TestEngineAndRescueDualFormat(unittest.TestCase):
    """Issue #61: engine, rescue_archives, and rescue_audit handle both slot formats and drop stale agent-slot text."""

    def test_get_rescue_bundles_parses_new_and_legacy_bundle_names(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            dc = tmp / "dc"
            (dc / "rescue").mkdir(parents=True)
            (dc / "rescue" / "0-slot-5-host_repo-20261106-040834-123.bundle").write_bytes(b"b1")
            (dc / "rescue" / "demo-app-slot-16-20261106-040834-299439.bundle").write_bytes(b"b2")
            with patch.object(engine, "DEV_CONTAINER_STATE", dc), \
                 patch.object(engine, "AGENT_SLOT_STATE", tmp / "none"), \
                 patch.object(engine.rescue_audit, "load_cache", return_value={}):
                bundles = engine.get_rescue_bundles()
        by_id = {b["id"]: b for b in bundles}
        self.assertEqual(by_id["0-slot-5-host_repo-20261106-040834-123"]["project"], "host_repo")
        self.assertEqual(by_id["0-slot-5-host_repo-20261106-040834-123"]["lease_id"], "0-slot-5-host_repo")
        self.assertEqual(by_id["demo-app-slot-16-20261106-040834-299439"]["project"], "demo-app")
        self.assertEqual(by_id["demo-app-slot-16-20261106-040834-299439"]["lease_id"], "demo-app-slot-16")

    def test_engine_has_no_stale_agent_slot_reap_text(self):
        src = (ROOT / "bin" / "engine.py").read_text(encoding="utf-8")
        self.assertNotIn("agent-slot.sh", src)
        self.assertNotIn("agent-slot-reap", src)
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with patch.object(engine, "DEV_CONTAINER_STATE", tmp / "dc"), \
                 patch.object(engine, "AGENT_SLOT_STATE", tmp / "legacy"), \
                 patch.object(engine, "get_engine_status_json", return_value=None):
                st = engine.get_cleaners_status()
        self.assertNotIn("agent-slot", st["fast"]["command"])

    def test_rescue_archives_verdict_parses_new_and_legacy_bundle_id(self):
        eng = unittest.mock.MagicMock()
        eng.get_rescue_archives.return_value = []
        eng._find_archive_entry.return_value = None
        eng.write_agent_verdict.return_value = (True, "ok")

        rescue_archives.manage_rescue(eng, "verdict", "0-slot-5-host_repo-20261106-040834")
        eng.write_agent_verdict.assert_called_with("host_repo", "slot-5")

        rescue_archives.manage_rescue(eng, "verdict", "host_repo-slot-5-20261106-040834")
        eng.write_agent_verdict.assert_called_with("host_repo", "slot-5")

    def test_rescue_audit_extracts_prefix_from_new_and_legacy_bundle_names(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            dc = tmp / "dc"
            (dc / "rescue").mkdir(parents=True)
            (dc / "rescue" / "0-slot-5-host_repo-20261106-040834-123.bundle").write_bytes(b"b1")
            (dc / "rescue" / "demo-app-slot-16-20261106-040834-299439.bundle").write_bytes(b"b2")
            seen_prefixes = []

            def fake_repo_for(prefix, fetch):
                seen_prefixes.append(prefix)
                return None

            with patch.object(rescue_audit, "RESCUE_DIRS", [dc / "rescue"]), \
                 patch.object(rescue_audit, "CACHE_DIR", tmp / "cache"), \
                 patch.object(rescue_audit, "CACHE", tmp / "cache" / "audit.json"), \
                 patch.object(rescue_audit, "_delivered_markers", return_value=set()), \
                 patch.object(rescue_audit, "_repo_for", side_effect=fake_repo_for):
                rescue_audit.run(fetch=False, refresh=True)
        self.assertEqual(sorted(seen_prefixes), ["demo-app", "host_repo"])


if __name__ == "__main__":
    unittest.main()
