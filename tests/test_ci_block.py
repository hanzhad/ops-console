#!/usr/bin/env python3
"""Tests for issue #9 part B: the CI block end to end.

Layers: pure (sw.core, the ci domain registration, the command lifecycle) and structural
(source text of bin/ci.py, sw/http.py, static/*.js, page.html). Nothing here starts a thread,
a server or a subprocess.

Assumed names (the implementer must match):
  sw.core.sse_ping_frame() -> bytes, a real named event ping with data: {}
  sw.domains.ci.BLOCKS: BlockSpec ci_gate (60 s) and gh_queue (60 s, after=("ci_gate",))
  sw.domains.ci.ROUTES["/api/ci"]: zones(args) == ("ci_gate",), deadline_s == 150
  bin/ci.py: gate_status(), queue(); no CACHE
  static/ci.js: renderCIGate, renderCIQueue, renderCIRepos; keys ci.local:<repo>, ci.budget, ci.paused
"""

import importlib
import pathlib
import re
import shlex
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"


def load(case, name, criterion):
    try:
        return importlib.import_module(name)
    except ImportError as e:
        case.fail("%s: cannot import %s (%s)" % (criterion, name, e))


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def ci_domain(case):
    return load(case, "sw.domains.ci", "part B: ci domain")


def blocks_by_name(case):
    return {b.name: b for b in ci_domain(case).BLOCKS}


def ci_cmd(zones, **kw):
    c = {"id": "cmd-00000001", "route": "/api/ci", "body": "{}", "zones": list(zones), "waiting": list(zones),
         "state": "running", "message": None, "created_at": 100.0, "deadline_at": 250.0,
         "finished_seq": None, "done_at": None, "rev": 1}
    c.update(kw)
    return c


class TestKeepAlivePing(unittest.TestCase):
    """Extends the issue: an SSE comment never reaches EventSource, so the page needs a named event."""

    def test_ping_frame_is_a_named_event_with_empty_object(self):
        core = load(self, "sw.core", "keep-alive")
        if not hasattr(core, "sse_ping_frame"):
            self.fail("keep-alive: sw.core.sse_ping_frame is missing")
        f = core.sse_ping_frame()
        self.assertIsInstance(f, bytes)
        self.assertIn(b"event: ping\n", f)
        self.assertIn(b"data: {}\n", f)
        self.assertTrue(f.endswith(b"\n\n"))
        self.assertFalse(f.startswith(b":"), "a comment line is invisible to EventSource")

    def test_server_stream_sends_the_ping_frame(self):
        self.assertIn("sse_ping_frame", text("sw/http.py"))

    def test_page_store_listens_for_ping(self):
        self.assertRegex(text("static/store.js"), r"""on\(\s*es\s*,\s*['"]ping['"]""",
                         "store.js must register a handler for the named event ping, which counts as a frame")


class TestCiRegistration(unittest.TestCase):
    def test_blocks_ci_gate_and_gh_queue_registered(self):
        self.assertEqual(set(blocks_by_name(self)), {"ci_gate", "gh_queue"})

    def test_periods_are_60_seconds(self):
        by = blocks_by_name(self)
        self.assertEqual(sorted(by), ["ci_gate", "gh_queue"])
        self.assertEqual(by["ci_gate"].period, 60)
        self.assertEqual(by["gh_queue"].period, 60)

    def test_gh_queue_is_woken_by_ci_gate_and_gate_by_nothing(self):
        by = blocks_by_name(self)
        self.assertEqual(sorted(by), ["ci_gate", "gh_queue"])
        self.assertEqual(tuple(by["gh_queue"].after), ("ci_gate",))
        self.assertEqual(tuple(by["ci_gate"].after), ())

    def test_readers_are_callable(self):
        self.assertEqual(len(ci_domain(self).BLOCKS), 2)
        for b in ci_domain(self).BLOCKS:
            self.assertTrue(callable(b.read), b.name)

    def test_route_zone_is_ci_gate_not_legacy(self):
        route = ci_domain(self).ROUTES["/api/ci"]
        for args in (["on", "a/b"], ["off", "a/b"], ["pause"], ["resume"], ["reset"], ["budget", "12", "8"]):
            self.assertEqual(tuple(route.zones(args)), ("ci_gate",), args)

    def test_route_deadline_150(self):
        self.assertEqual(ci_domain(self).ROUTES["/api/ci"].deadline_s, 150)

    def test_route_validator_accepts_and_rejects(self):
        v = ci_domain(self).ROUTES["/api/ci"].validate
        self.assertEqual(v({"id": "acc-ci-0001", "args": ["on", "a/b"]}), ["on", "a/b"])
        self.assertIsNone(v({"args": ["on", "a/b;rm"]}))
        self.assertIsNone(v({"args": ["budget", "12", "3"]}))


class TestCacheRaceLifecycle(unittest.TestCase):
    """The original bug: a ci_gate pass that started before the toggle finished must not confirm it."""

    def finished(self):
        core = load(self, "sw.core", "cache race")
        zones = ci_domain(self).ROUTES["/api/ci"].zones(["on", "a/b"])
        return core, core.cmd_on_finish(ci_cmd(zones), 0, "CI on", 12, 200.0)

    def test_pass_started_before_the_command_finished_does_not_confirm(self):
        core, c = self.finished()
        c = core.cmd_on_pass(c, "ci_gate", 10, 201.0)
        self.assertEqual(c["state"], "running")
        c = core.cmd_on_pass(c, "ci_gate", 12, 201.0)
        self.assertEqual(c["state"], "running")

    def test_pass_started_after_finish_confirms(self):
        core, c = self.finished()
        c = core.cmd_on_pass(c, "ci_gate", 10, 201.0)
        c = core.cmd_on_pass(c, "ci_gate", 13, 202.0)
        self.assertEqual(c["state"], "ok")

    def test_legacy_pass_never_confirms_a_ci_command(self):
        core, c = self.finished()
        c = core.cmd_on_pass(c, "legacy", 99, 201.0)
        self.assertEqual(c["state"], "running")


class TestFileCacheGone(unittest.TestCase):
    def test_bin_ci_has_no_cache_or_ci_json(self):
        self.assertNotRegex(text("bin/ci.py"), r"CACHE|ci\.json")

    def test_bin_ci_exposes_gate_status_and_queue(self):
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        mod = load(self, "ci", "bin/ci.py")
        self.assertTrue(hasattr(mod, "gate_status"), "bin/ci.py needs gate_status()")
        self.assertTrue(hasattr(mod, "queue"))
        self.assertTrue(hasattr(mod, "status"), "status() stays for the CLI")

    def test_bin_ci_reuses_the_core_parsers(self):
        src = text("bin/ci.py")
        for name in ("parse_ci_list", "parse_ci_budget", "parse_ci_status_json"):
            self.assertIn(name, src)
        self.assertNotIn("parse_queue_tsv", src)

    def test_actions_no_longer_unlink_a_cache(self):
        self.assertNotIn("unlink", text("bin/ci.py"))

    def test_bin_ci_has_no_ci_gate_docker_exec_fallback(self):
        from unittest.mock import patch
        src = text("bin/ci.py")
        self.assertNotIn("ci-gate", src)
        self.assertNotRegex(src, r"""docker.*exec""")
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        ci_mod = load(self, "ci", "bin/ci.py")
        with patch.object(ci_mod, "run", side_effect=[(1, "err"), (0, ""), (0, "")]), \
                patch.object(ci_mod.subprocess, "run") as sub_mock:
            st = ci_mod.gate_status()
        self.assertFalse(sub_mock.called, "gate_status fallback must not invoke docker exec ci-gate")
        self.assertNotIn("paused", st)


class TestPageCi(unittest.TestCase):
    def test_gateov_and_gatenow_are_gone(self):
        files = ["page.html"] + ["static/" + p.name for p in sorted((ROOT / "static").glob("*.js"))]
        for rel in files:
            self.assertNotRegex(text(rel), r"gateOv|gateNow", rel)

    def test_ci_js_split_into_three_renderers(self):
        src = text("static/ci.js")
        for fn in ("renderCIGate", "renderCIQueue", "renderCIRepos"):
            self.assertRegex(src, r"function\s+%s\b" % fn)
        self.assertNotRegex(src, r"function\s+renderCI\s*\(")

    def test_ci_js_subscribes_to_the_new_blocks_not_legacy(self):
        src = text("static/ci.js")
        self.assertIn("block:ci_gate", src)
        self.assertIn("block:gh_queue", src)
        self.assertNotIn("block:legacy", src)

    def test_checkbox_goes_through_store_command_with_pending_keys(self):
        src = text("static/ci.js")
        for needle in ("store.command", "ci.local:", "ci.budget", "ci.paused", "pendingFor"):
            self.assertIn(needle, src)

    def test_repos_checkbox_is_a_keyed_update_not_an_inline_api_call(self):
        src = text("static/ci.js")
        self.assertNotRegex(src, r'onchange="api\(')
        self.assertRegex(src, r"\.checked\s*=")
        self.assertRegex(src, r"\.disabled\s*=")


class TestCiStatusJsonAndMega(unittest.TestCase):
    SAMPLE = """{
      "schema": 1,
      "gate": {"state": "running", "locked": false, "paused": false, "commit": "ca18229", "token_expires": "2027-02-03", "owner_tokens": []},
      "status": {"schema": 1, "used": 2, "budget": 12, "big_used": 0, "big_cap": 8, "busy": 1, "class_costs": {"big": 4, "little": 2, "mega": 12}, "draining": {"repo": "acme-org/acme-org", "seconds": 42}, "waiting": null},
      "repositories": [
        {"repo": "hanzhad/career", "ci_runner": 1, "mega": null},
        {"repo": "acme-org/acme-org", "ci_runner": 0, "mega": "local-docker-mega"},
        {"repo": "hanzhad/demo-app-ai-extension", "ci_runner": 1, "mega": null}
      ]
    }"""

    def test_parse_ci_status_json_preserves_shape_and_skips(self):
        core = load(self, "sw.core", "status json")
        out = core.parse_ci_status_json(self.SAMPLE, {"hanzhad/demo-app-ai-extension"})
        self.assertTrue(out["running"])
        self.assertEqual((out["budget"], out["cap"], out["used"]), (12, 8, 2))
        self.assertEqual(out["draining"]["repo"], "acme-org/acme-org")
        self.assertEqual([r["repo"] for r in out["repos"]], ["hanzhad/career", "acme-org/acme-org"])
        self.assertEqual(out["repos"][1]["mega"], "local-docker-mega")

    def test_validate_ci_accepts_mega_actions(self):
        v = ci_domain(self).ROUTES["/api/ci"].validate
        self.assertEqual(v({"args": ["mega-on", "acme-org/acme-org"]}), ["mega-on", "acme-org/acme-org"])
        self.assertEqual(v({"args": ["mega-off", "acme-org/acme-org"]}), ["mega-off", "acme-org/acme-org"])
        self.assertIsNone(v({"args": ["mega-on", "bad;repo"]}))

    def test_bin_ci_passes_owner_repo_directly_and_supports_mega(self):
        from unittest.mock import patch
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        ci_mod = load(self, "ci", "bin/ci.py")
        with patch.object(ci_mod, "CI", ci_mod.CI_TOOLS),              patch.object(ci_mod, "run", return_value=(0, "ok")) as run_mock:
            self.assertEqual(ci_mod.main(["on", "hanzhad/career"]), 0)
            self.assertEqual(run_mock.call_args.args, ("on", "hanzhad/career"))
            self.assertEqual(ci_mod.main(["mega-on", "acme-org/acme-org"]), 0)
            self.assertEqual(run_mock.call_args.args, ("mega", "on", "acme-org/acme-org"))

    def test_bin_ci_legacy_script_falls_back_to_checkout_and_rejects_mega(self):
        from unittest.mock import patch
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        ci_mod = load(self, "ci", "bin/ci.py")
        legacy = pathlib.Path("/tmp/legacy/ci-runner.sh")
        with patch.object(ci_mod, "CI", legacy),              patch.object(ci_mod, "checkout_of", return_value=pathlib.Path("/tmp/proj")),              patch.object(ci_mod, "run", return_value=(0, "ok")) as run_mock:
            self.assertEqual(ci_mod.main(["on", "o/r"]), 0)
            self.assertEqual(run_mock.call_args.args, ("on", "/tmp/proj"))
            self.assertEqual(ci_mod.main(["mega-on", "o/r"]), 2)

    def test_gate_status_uses_status_json(self):
        from unittest.mock import patch
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        ci_mod = load(self, "ci", "bin/ci.py")
        with patch.object(ci_mod, "run", side_effect=[(0, self.SAMPLE), (0, "budget 10 cores, big cap 8\n")]):
            st = ci_mod.gate_status()
        self.assertEqual((st["budget"], st["cap"], st["boot"]), (12, 8, [10, 8]))

    def test_regate_uses_redeploy_and_pin_vault(self):
        src = text("regate.sh")
        self.assertIn("ci-runner.sh redeploy --yes", src)
        self.assertIn("pin-vault unlock", src)

    def test_parse_ci_status_json_parses_queue_non_local_queued_and_rate_limit(self):
        import json
        core = load(self, "sw.core", "status json queue and rate_limit")
        payload = json.loads(self.SAMPLE)
        payload["status"]["queue"] = [
            {"repo": "hanzhad/career", "class": "big", "since": "2026-11-26T00:00:00Z", "run_id": 101, "name": "CI", "branch": "main"},
            {"repo": "acme-org/acme-org", "class": "mega", "since": "2026-11-26T00:02:00Z", "run_id": 103, "name": None, "branch": None},
        ]
        payload["status"]["non_local_queued"] = 3
        payload["status"]["rate_limit"] = {
            "remaining": 1420,
            "limit": 5000,
            "reset_at": "2026-11-26T01:00:00Z",
            "throttled": False,
        }
        out = core.parse_ci_status_json(json.dumps(payload), set())
        self.assertIn("queue", out)
        self.assertEqual(out["queue"], [
            {"repo": "hanzhad/career", "id": 101, "created": "2026-11-26T00:00:00Z", "name": "CI", "branch": "main", "class": "big"},
            {"repo": "acme-org/acme-org", "id": 103, "created": "2026-11-26T00:02:00Z", "name": None, "branch": None, "class": "mega"},
        ])
        self.assertIn("non_local_queued", out)
        self.assertEqual(out["non_local_queued"], 3)
        self.assertIn("rate_limit", out)
        self.assertEqual(out["rate_limit"], {
            "remaining": 1420,
            "limit": 5000,
            "reset_at": "2026-11-26T01:00:00Z",
            "throttled": False,
        })

    def test_parse_ci_status_json_defaults_when_queue_non_local_and_rate_limit_absent(self):
        core = load(self, "sw.core", "status json backward compat")
        out = core.parse_ci_status_json(self.SAMPLE, set())
        self.assertIn("queue", out)
        self.assertEqual(out["queue"], [])
        self.assertIn("non_local_queued", out)
        self.assertEqual(out["non_local_queued"], 0)
        self.assertIn("rate_limit", out)
        self.assertIsNone(out["rate_limit"])

    def test_parse_ci_status_json_sorts_queue_oldest_first(self):
        import json
        core = load(self, "sw.core", "status json queue ordering")
        payload = json.loads(self.SAMPLE)
        payload["status"]["queue"] = [
            {"repo": "acme-org/acme-org", "class": "mega", "since": "2026-11-26T00:02:00Z", "run_id": 103, "name": "CI", "branch": "main"},
            {"repo": "hanzhad/career", "class": "big", "since": "2026-11-26T00:00:00Z", "run_id": 101, "name": "CI", "branch": "main"},
            {"repo": "hanzhad/host_repo", "class": "little", "since": "2026-11-26T00:01:00Z", "run_id": 102, "name": "Lint", "branch": "feat"},
        ]
        out = core.parse_ci_status_json(json.dumps(payload), set())
        self.assertEqual(
            [(q["id"], q["created"]) for q in out["queue"]],
            [
                (101, "2026-11-26T00:00:00Z"),
                (102, "2026-11-26T00:01:00Z"),
                (103, "2026-11-26T00:02:00Z"),
            ],
        )




class TestCiQueueZeroApi(unittest.TestCase):
    """Issue #63: Read queue from ci_gate status --json with 0 GitHub API calls."""

    def setUp(self):
        import unittest.mock
        self.mock = unittest.mock
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        self.ci = load(self, "ci", "bin/ci.py")

    def test_bin_ci_removes_has_queued_jobs_and_gh_api_polling(self):
        src = text("bin/ci.py")
        self.assertFalse(hasattr(self.ci, "has_queued_jobs"), "bin/ci.py must remove has_queued_jobs()")
        self.assertNotIn("gh api", src)
        self.assertNotIn("actions/runs", src)

    def test_bin_ci_queue_and_status_use_gate_status_with_zero_subprocess_calls(self):
        sample_queue = [
            {"repo": "hanzhad/career", "id": 101, "created": "2026-11-26T00:00:00Z", "name": "CI", "branch": "main", "class": "big"},
        ]
        gate_payload = {"running": True, "queue": sample_queue, "repos": []}
        with self.mock.patch.object(self.ci, "gate_status", return_value=gate_payload) as gate_mock, \
                self.mock.patch.object(self.ci.subprocess, "run") as sub_mock:
            q_out = self.ci.queue(["hanzhad/career"])
            st_out = self.ci.status()

        self.assertFalse(sub_mock.called, "queue() and status() must make 0 gh api / subprocess calls")
        self.assertEqual(q_out, sample_queue)
        self.assertEqual(st_out["queue"], sample_queue)
        self.assertGreaterEqual(gate_mock.call_count, 2)

    def test_read_gh_queue_returns_ci_gate_queue_with_zero_api_calls(self):
        class FakeStore:
            def __init__(self, gate_data):
                self._gate_data = gate_data

            def block_data(self, name):
                return self._gate_data if name == "ci_gate" else None

        dom = ci_domain(self)
        sample_queue = [{"repo": "a/b", "id": 42, "created": "2026-11-26T00:00:00Z", "name": "build", "branch": "main", "class": "little"}]
        with self.mock.patch.object(self.ci, "queue", side_effect=AssertionError("must not call bin/ci.py queue()")), \
                self.mock.patch.object(self.ci.subprocess, "run", side_effect=AssertionError("must not shell out")):
            self.assertEqual(dom.read_gh_queue(FakeStore({"queue": sample_queue})), sample_queue)
            self.assertEqual(dom.read_gh_queue(FakeStore({})), [])
            self.assertEqual(dom.read_gh_queue(FakeStore(None)), [])

    def test_read_gh_queue_and_state_preserve_unknown_when_queue_unknown(self):
        class FakeStore:
            def __init__(self, gate_data):
                self._gate_data = gate_data

            def block_data(self, name):
                return self._gate_data if name == "ci_gate" else None

        dom = ci_domain(self)
        gate_unknown = {"running": True, "queue": [], "queue_unknown": True}
        q = dom.read_gh_queue(FakeStore(gate_unknown))
        self.assertIsNone(q)
        self.assertNotEqual(q, [])

        from sw import core as core_mod
        legacy_out = core_mod.assemble_legacy({"ci_gate": gate_unknown, "gh_queue": q}, {})
        self.assertIsNone(legacy_out["gate"]["queue"])
        state_out = core_mod.api_state({"ci_gate": gate_unknown, "gh_queue": q}, [], [], 5.0, None)
        self.assertIsNone(state_out["gate"]["queue"])

    def test_queue_and_read_gh_queue_sort_out_of_order_entries_oldest_first(self):
        class FakeStore:
            def __init__(self, gate_data):
                self._gate_data = gate_data

            def block_data(self, name):
                return self._gate_data if name == "ci_gate" else None

        dom = ci_domain(self)
        unsorted = [
            {"repo": "b/c", "id": 2, "created": "2026-11-26T00:02:00Z", "name": "CI", "branch": "main", "class": "big"},
            {"repo": "a/b", "id": 1, "created": "2026-11-26T00:00:00Z", "name": "CI", "branch": "main", "class": "little"},
        ]
        expected = [unsorted[1], unsorted[0]]
        with self.mock.patch.object(self.ci, "gate_status", return_value={"running": True, "queue": list(unsorted)}):
            self.assertEqual(self.ci.queue(), expected)
            self.assertEqual(self.ci.status()["queue"], expected)
        self.assertEqual(dom.read_gh_queue(FakeStore({"queue": list(unsorted)})), expected)

    def test_fallback_when_status_json_fails_marks_queue_unknown_and_carries_keys(self):
        for bad_status in [(1, "err"), (0, "no json here"), (0, "{bad"), (0, '{"schema": 2}')]:
            with self.subTest(bad_status=bad_status):
                with self.mock.patch.object(
                    self.ci,
                    "run",
                    side_effect=[bad_status, (0, "gate running\n"), (0, "budget 10 cores, big cap 8\n")],
                ):
                    st = self.ci.gate_status()
                self.assertEqual(st["queue"], [])
                self.assertEqual(st["non_local_queued"], 0)
                self.assertIsNone(st["rate_limit"])
                self.assertTrue(st.get("queue_unknown"))

                with self.mock.patch.object(
                    self.ci,
                    "run",
                    side_effect=[bad_status, (0, "gate running\n"), (0, "budget 10 cores, big cap 8\n")],
                ):
                    self.assertIsNone(self.ci.queue())

                with self.mock.patch.object(
                    self.ci,
                    "run",
                    side_effect=[bad_status, (0, "gate running\n"), (0, "budget 10 cores, big cap 8\n")],
                ):
                    status_out = self.ci.status()
                self.assertEqual(status_out["queue"], [])
                self.assertTrue(status_out.get("queue_unknown"))


class TestGateStatusLiveBudgetOverlay(unittest.TestCase):
    """Issue #37: bin/ci.py:gate_status() overlays live budget/reset from run('budget') onto status --json."""

    def setUp(self):
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        self.ci = load(self, "ci", "bin/ci.py")

    def test_overlays_live_budget_and_cap_onto_status_json(self):
        from unittest.mock import call, patch

        empty_status = (
            '{"schema":1,"gate":{"state":"running","locked":false,"paused":false},'
            '"status":{},"repositories":[]}'
        )
        cases = [
            ("live overrides stale status.json", TestCiStatusJsonAndMega.SAMPLE, "budget 10 cores, big cap 8\nlive 16 12\n", 16, 12, [10, 8], 2),
            ("live shrinks budget below status.json", TestCiStatusJsonAndMega.SAMPLE, "budget 14 cores, big cap 8\nlive 8 4\n", 8, 4, [14, 8], 2),
            ("live zero budget and cap uses is-not-None", TestCiStatusJsonAndMega.SAMPLE, "budget 10 cores, big cap 8\nlive 0 0\n", 0, 0, [10, 8], 2),
            ("live without boot line", TestCiStatusJsonAndMega.SAMPLE, "live 20 8\n", 20, 8, None, 2),
            ("live fills empty status object", empty_status, "budget 10 cores, big cap 8\nlive 16 12\n", 16, 12, [10, 8], 0),
        ]
        for label, status_out, budget_out, want_budget, want_cap, want_boot, want_used in cases:
            with self.subTest(label=label):
                with patch.object(self.ci, "run", side_effect=[(0, status_out), (0, budget_out)]) as mock_run:
                    st = self.ci.gate_status()
                self.assertEqual(mock_run.call_args_list, [call("status", "--json"), call("budget")])
                self.assertEqual(
                    (st["budget"], st["cap"], st["live"], st["boot"], st["used"]),
                    (want_budget, want_cap, True, want_boot, want_used),
                )

    def test_overlays_boot_on_live_not_set_and_ignores_non_matching_reset_lines(self):
        from unittest.mock import patch

        cases = [
            ("reset default message", "budget 10 cores, big cap 8\nlive not set (using boot default)\n", 10, 8, False, [10, 8]),
            ("reset multiple whitespace", "budget 14 cores, big cap 8\nlive   not set\n", 14, 8, False, [14, 8]),
            ("reset multiline with header", "gate ok\nbudget 16 cores, big cap 12\nlive not set\ndone\n", 16, 12, False, [16, 12]),
            ("boot only without live not set", "budget 10 cores, big cap 8\n", 12, 8, True, [10, 8]),
            ("live not set not at line start", "budget 10 cores, big cap 8\nnote: live not set\n", 12, 8, True, [10, 8]),
            ("live not settled fails word boundary", "budget 10 cores, big cap 8\nlive not settled\n", 12, 8, True, [10, 8]),
            ("live not set without boot line", "live not set\n", 12, 8, True, None),
            ("unparseable budget output", "nothing useful\n", 12, 8, True, None),
        ]
        for label, budget_out, want_budget, want_cap, want_live, want_boot in cases:
            with self.subTest(label=label):
                with patch.object(self.ci, "run", side_effect=[(0, TestCiStatusJsonAndMega.SAMPLE), (0, budget_out)]):
                    st = self.ci.gate_status()
                self.assertEqual(
                    (st["budget"], st["cap"], st["live"], st["boot"]),
                    (want_budget, want_cap, want_live, want_boot),
                )

    def test_ignores_budget_output_when_budget_command_fails(self):
        from unittest.mock import patch

        for b_rc, b_out in (
            (1, "budget 10 cores, big cap 8\nlive 20 12\n"),
            (2, "budget 10 cores, big cap 8\nlive not set\n"),
            (1, "error\n"),
        ):
            with self.subTest(b_rc=b_rc, b_out=b_out):
                with patch.object(self.ci, "run", side_effect=[(0, TestCiStatusJsonAndMega.SAMPLE), (b_rc, b_out)]):
                    st = self.ci.gate_status()
                self.assertEqual((st["budget"], st["cap"], st["live"], st["boot"]), (12, 8, True, None))


class TestBinCiRunQuoting(unittest.TestCase):
    """Issue #37: bin/ci.py:run() quotes CI script path and all arguments with shlex.quote."""

    def setUp(self):
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        self.ci = load(self, "ci", "bin/ci.py")

    def test_run_quotes_script_path_and_arguments_with_shlex_quote(self):
        from unittest.mock import MagicMock, patch

        cases = [
            (pathlib.Path("/tmp/ci dir/ci-runner's.sh"), ()),
            (pathlib.Path("/tmp/tools/ci-runner.sh"), ("on", "o/r; touch /tmp/pwned", "arg with spaces")),
            (pathlib.Path("/tmp/ci dir/ci-runner's.sh"), ("on", "o/r; touch /tmp/pwned", "arg with spaces", "x'y$(id)`uname`")),
            (pathlib.Path("/tmp/tools/ci-runner.sh"), ("budget", "12", "--big-cap", "8")),
            (pathlib.Path("/tmp/tools/ci-runner.sh"), ("status", "--json")),
        ]
        for ci_path, args in cases:
            with self.subTest(ci_path=str(ci_path), args=args):
                proc = MagicMock(returncode=0, stdout="WARNING: noisy\nok line\n")
                with patch.object(self.ci, "CI", ci_path), \
                        patch.object(self.ci.subprocess, "run", return_value=proc) as mock_run:
                    rc, out = self.ci.run(*args)
                self.assertEqual((rc, out), (0, "ok line"))
                argv = mock_run.call_args.args[0]
                self.assertEqual(argv[:2], ["bash", "-lc"])
                cmd_str = argv[2]
                self.assertIn(shlex.quote(str(ci_path)), cmd_str)
                for a in args:
                    self.assertIn(shlex.quote(a), cmd_str)
                cmd_body = re.sub(r"\s+2>&1\s*$", "", cmd_str)
                self.assertNotEqual(cmd_body, cmd_str, f"missing 2>&1 redirect in {cmd_str!r}")
                self.assertEqual(shlex.split(cmd_body), [str(ci_path), *args])


class TestCiJsClassCosts(unittest.TestCase):
    """Issue #37: static/ci.js derives bigCost and littleCost from g.class_costs in adjustRunners() and renderCIGate()."""

    VM_HARNESS = r"""
const vm = require("vm");
const assert = require("assert");
const [utilSrc, ciSrc] = process.argv.slice(1);

function makeElement(tag, id) {
  const classSet = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    id: id || "",
    type: "",
    checked: false,
    title: "",
    dataset: {},
    style: {},
    children: [],
    listeners: {},
    onclick: null,
    _disabled: false,
    _text: "",
    _html: "",
    get disabled() { return this._disabled; },
    set disabled(v) { this._disabled = Boolean(v); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v ?? ""); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v ?? ""); },
    get className() { return Array.from(classSet).join(" "); },
    set className(v) {
      classSet.clear();
      String(v || "").split(/\s+/).filter(Boolean).forEach((c) => classSet.add(c));
    },
    classList: {
      add(...cs) { cs.forEach((c) => c && classSet.add(c)); },
      remove(...cs) { cs.forEach((c) => classSet.delete(c)); },
      contains(c) { return classSet.has(c); },
      toggle(c, force) {
        if (force === undefined) {
          if (classSet.has(c)) { classSet.delete(c); return false; }
          classSet.add(c); return true;
        }
        if (force) { classSet.add(c); return true; }
        classSet.delete(c); return false;
      },
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    appendChild(child) { this.children.push(child); return child; },
    append(...nodes) {
      for (const n of nodes) {
        if (typeof n !== "string") this.children.push(n);
      }
    },
    querySelector(sel) {
      for (const ch of this.children) {
        if (sel === "input" && ch.tagName === "INPUT") return ch;
        if (sel.startsWith(".") && ch.classList.contains(sel.slice(1))) return ch;
        const sub = ch.querySelector(sel);
        if (sub) return sub;
      }
      return null;
    },
    click() {
      if (this.disabled) return;
      if (typeof this.onclick === "function") this.onclick();
    },
  };
  return el;
}

function createHarness() {
  const elements = {};
  for (const id of [
    "ci-gate-badge", "btn-gate-toggle", "ci-gate-meta",
    "ci-big-busy", "ci-big-cap", "ci-little-busy", "ci-little-cap",
    "btn-big-minus", "btn-big-plus", "btn-little-minus", "btn-little-plus",
    "ci-budget-summary", "btn-ci-reset", "ci-tbody",
    "ci-queue-wrap", "ci-queue-count", "ci-queue-tbody", "ci-repos-list",
  ]) {
    elements[id] = makeElement(id.startsWith("btn-") ? "button" : "div", id);
  }

  const subscribers = [];
  const sent = [];
  const pendingMap = {};

  const storeObj = {
    blocks: {},
    subscribe(keys, fn) { subscribers.push({ keys, fn }); return () => {}; },
    command(path, body, opts) { sent.push({ path, body, opts }); return "cmd-ci-1"; },
    pendingFor(key) { return pendingMap[key] || null; },
  };

  function notify(changedKeys) {
    for (const s of subscribers) {
      if (changedKeys.some((k) => s.keys.includes(k) || (k.startsWith("block:") && s.keys.includes("block:*")))) {
        s.fn();
      }
    }
  }

  const ctxObj = {
    S: { ci: [] },
    Date,
    Math,
    console,
    document: {
      getElementById: (id) => elements[id] || null,
      createElement: (tag) => makeElement(tag, ""),
    },
    store: storeObj,
  };
  const ctx = vm.createContext(ctxObj);
  vm.runInContext(utilSrc, ctx);
  vm.runInContext(ciSrc, ctx);

  return {
    ctx,
    elements,
    sent,
    pendingMap,
    pushGate(data, runners) {
      storeObj.blocks.ci_gate = { data };
      ctxObj.S = { ci: runners || [] };
      notify(["block:ci_gate"]);
    },
    notify,
  };
}
"""

    def run_vm(self, script_body):
        js = self.VM_HARNESS + "\n" + script_body
        p = subprocess.run(
            ["node", "-e", js, text("static/util.js"), text("static/ci.js")],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)

    def test_render_ci_queue_shows_unavailable_when_queue_unknown(self):
        self.run_vm(r"""
const h = createHarness();

h.ctx.store.blocks.ci_gate = { data: { running: true, budget: 12, cap: 8, queue_unknown: true, repos: [] } };
h.ctx.store.blocks.gh_queue = { data: null };
h.notify(["block:ci_gate", "block:gh_queue"]);
assert.strictEqual(h.elements["ci-queue-wrap"].style.display, "block");
assert.strictEqual(h.elements["ci-queue-count"].textContent, "unavailable");
assert.ok(h.elements["ci-queue-tbody"].innerHTML.includes("queue unavailable (legacy status)"));

h.ctx.store.blocks.ci_gate = { data: { running: true, budget: 12, cap: 8, queue_unknown: false, repos: [] } };
h.ctx.store.blocks.gh_queue = { data: [] };
h.notify(["block:ci_gate", "block:gh_queue"]);
assert.strictEqual(h.elements["ci-queue-wrap"].style.display, "none");

h.ctx.store.blocks.gh_queue = { data: null };
h.notify(["block:gh_queue"]);
assert.strictEqual(h.elements["ci-queue-wrap"].style.display, "block");
assert.ok(h.elements["ci-queue-tbody"].innerHTML.includes("queue unavailable (legacy status)"));
""")

    def test_render_ci_gate_uses_class_costs_for_counters_summary_and_reset(self):
        self.run_vm(r"""
const h = createHarness();

// Custom big=6, little=3 costs with boot diff -> 2b+3l, reset 2b+1l
h.pushGate({
  running: true,
  locked: false,
  paused: false,
  budget: 21,
  cap: 12,
  boot: [15, 12],
  used: 6,
  class_costs: { big: 6, little: 3, mega: 24 },
  repos: [],
});
assert.strictEqual(h.elements["ci-big-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "3");
assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 21 cores (big 12, little 9, used 6, mega 24c)");
assert.strictEqual(h.elements["btn-ci-reset"].style.display, "inline-flex");
assert.strictEqual(h.elements["btn-ci-reset"].textContent, "Reset to 2b+1l");
assert.ok(h.elements["ci-tbody"].innerHTML.includes("big #2"));
assert.ok(!h.elements["ci-tbody"].innerHTML.includes("big #3"));
assert.ok(h.elements["ci-tbody"].innerHTML.includes("little #3"));
assert.ok(!h.elements["ci-tbody"].innerHTML.includes("little #4"));

// Different custom costs big=8, little=4 -> 2b+2l, reset 1b+1l
h.pushGate({
  running: true,
  budget: 24,
  cap: 16,
  boot: [12, 8],
  class_costs: { big: 8, little: 4 },
  repos: [],
});
assert.strictEqual(h.elements["ci-big-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 24 cores (big 16, little 8)");
assert.strictEqual(h.elements["btn-ci-reset"].textContent, "Reset to 1b+1l");

// Partial class_costs: only big=6 (little defaults to 2)
h.pushGate({
  running: true,
  budget: 16,
  cap: 12,
  class_costs: { big: 6 },
  repos: [],
});
assert.strictEqual(h.elements["ci-big-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 16 cores (big 12, little 4)");

// Partial class_costs: only little=5 (big defaults to 4)
h.pushGate({
  running: true,
  budget: 18,
  cap: 8,
  class_costs: { little: 5 },
  repos: [],
});
assert.strictEqual(h.elements["ci-big-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 18 cores (big 8, little 10)");

// Zero/missing class_costs fall back to 4 and 2 without Infinity/NaN
for (const cc of [undefined, null, {}, { big: 0, little: 0 }]) {
  h.pushGate({
    running: true,
    budget: 14,
    cap: 8,
    boot: [10, 8],
    class_costs: cc,
    repos: [],
  });
  assert.strictEqual(h.elements["ci-big-cap"].textContent, "2");
  assert.strictEqual(h.elements["ci-little-cap"].textContent, "3");
  assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 14 cores (big 8, little 6)");
  assert.strictEqual(h.elements["btn-ci-reset"].textContent, "Reset to 2b+1l");
}

// Fallback totalBudget when g.budget is null/undefined with custom class_costs
h.pushGate({
  running: true,
  budget: null,
  cap: 12,
  class_costs: { big: 6, little: 3 },
  repos: [],
});
assert.strictEqual(h.elements["ci-big-cap"].textContent, "2");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "0");
assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 12 cores (big 12, little 0)");
""")

    def test_adjust_runners_and_reset_dispatch_with_class_costs(self):
        self.run_vm(r"""
const h = createHarness();

// 2b + 3l at big=6, little=3 -> budget 21, cap 12, boot [15, 12]
h.pushGate({
  running: true,
  budget: 21,
  cap: 12,
  boot: [15, 12],
  class_costs: { big: 6, little: 3 },
  repos: [],
});
h.elements["btn-big-plus"].click();
h.elements["btn-big-minus"].click();
h.elements["btn-little-plus"].click();
h.elements["btn-little-minus"].click();
h.elements["btn-ci-reset"].click();

assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/ci", body: { args: ["budget", "27", "18"] }, opts: { key: "ci.budget", value: { budget: 27, cap: 18 } } },
  { path: "/api/ci", body: { args: ["budget", "15", "6"] }, opts: { key: "ci.budget", value: { budget: 15, cap: 6 } } },
  { path: "/api/ci", body: { args: ["budget", "24", "12"] }, opts: { key: "ci.budget", value: { budget: 24, cap: 12 } } },
  { path: "/api/ci", body: { args: ["budget", "18", "12"] }, opts: { key: "ci.budget", value: { budget: 18, cap: 12 } } },
  { path: "/api/ci", body: { args: ["reset"] }, opts: { key: "ci.budget", value: { budget: 15, cap: 12 } } },
]);

// Pending ci.budget overlays live counts using class_costs and disables buttons
h.pendingMap["ci.budget"] = { value: { budget: 27, cap: 18 } };
h.notify(["pending"]);
assert.strictEqual(h.elements["ci-big-cap"].textContent, "3");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "3");
assert.strictEqual(h.elements["ci-budget-summary"].textContent, "= 27 cores (big 18, little 9)");
assert.strictEqual(h.elements["btn-big-plus"].disabled, true);
assert.strictEqual(h.elements["btn-ci-reset"].disabled, true);
delete h.pendingMap["ci.budget"];

// Boundary: big=1 disables minus when cap=8, bigCost=8 (would look like big=2 under hardcoded 4)
h.pushGate({ running: true, budget: 12, cap: 8, class_costs: { big: 8, little: 4 }, repos: [] });
assert.strictEqual(h.elements["ci-big-cap"].textContent, "1");
assert.strictEqual(h.elements["ci-little-cap"].textContent, "1");
assert.strictEqual(h.elements["btn-big-minus"].disabled, true);
assert.strictEqual(h.elements["btn-little-minus"].disabled, false);

// Boundary: big=1 clamps at 1, little=0 clamps at 0 with custom costs
h.sent.length = 0;
h.pushGate({ running: true, budget: 9, cap: 6, class_costs: { big: 6, little: 3 }, repos: [] });
assert.strictEqual(h.elements["btn-big-minus"].disabled, true);
assert.strictEqual(h.elements["btn-little-minus"].disabled, false);
vm.runInContext("adjustRunners('big', -1)", h.ctx);

h.pushGate({ running: true, budget: 12, cap: 12, class_costs: { big: 6, little: 3 }, repos: [] });
assert.strictEqual(h.elements["btn-big-minus"].disabled, false);
assert.strictEqual(h.elements["btn-little-minus"].disabled, true);
vm.runInContext("adjustRunners('little', -1)", h.ctx);

// Boundary: zero class_costs fall back to 4 and 2 in adjustRunners()
h.pushGate({ running: true, budget: 14, cap: 8, class_costs: { big: 0, little: 0 }, repos: [] });
h.elements["btn-big-plus"].click();

assert.deepStrictEqual(JSON.parse(JSON.stringify(h.sent)), [
  { path: "/api/ci", body: { args: ["budget", "9", "6"] }, opts: { key: "ci.budget", value: { budget: 9, cap: 6 } } },
  { path: "/api/ci", body: { args: ["budget", "12", "12"] }, opts: { key: "ci.budget", value: { budget: 12, cap: 12 } } },
  { path: "/api/ci", body: { args: ["budget", "18", "12"] }, opts: { key: "ci.budget", value: { budget: 18, cap: 12 } } },
]);
""")


class TestCiJsRateLimitPill(unittest.TestCase):
    """Issue #63: CI gate header renders GH API <remaining>/<limit> badge when rate_limit is present."""

    def test_rate_limit_pill_thresholds_throttled_countdown_and_absent_degradation(self):
        html = text("page.html")
        sec_m = re.search(r'<section id="sec-ci">.*?</section>', html, re.S)
        self.assertIsNotNone(sec_m, "page.html missing #sec-ci")
        hdr_m = re.search(r'<div class="sec-header">.*?</div>\s*<!--|<div class="sec-header">.*?</div>\s*</div>', sec_m.group(0), re.S)
        hdr_html = hdr_m.group(0) if hdr_m else sec_m.group(0)
        hdr_ids = re.findall(r'id="([^"]+)"', hdr_html)

        js = TestCiJsClassCosts.VM_HARNESS + r"""
const [,, pageHtml, hdrIdsJson] = process.argv.slice(1);
const hdrIds = JSON.parse(hdrIdsJson);

// Fixed clock: 2026-11-26T00:25:00Z
const FIXED_NOW = Date.parse("2026-11-26T00:25:00Z");
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(FIXED_NOW);
    else super(...args);
  }
  static now() { return FIXED_NOW; }
  static parse(s) { return RealDate.parse(s); }
}

function createHeaderHarness() {
  const elements = {};
  for (const id of [
    "ci-gate-badge", "btn-gate-toggle", "ci-gate-meta",
    "ci-big-busy", "ci-big-cap", "ci-little-busy", "ci-little-cap",
    "btn-big-minus", "btn-big-plus", "btn-little-minus", "btn-little-plus",
    "ci-budget-summary", "btn-ci-reset", "ci-tbody",
    "ci-queue-wrap", "ci-queue-count", "ci-queue-tbody", "ci-repos-list",
    ...hdrIds,
  ]) {
    if (!elements[id]) {
      elements[id] = makeElement(id.startsWith("btn-") ? "button" : "span", id);
    }
  }
  const subscribers = [];
  const storeObj = {
    blocks: {},
    subscribe(keys, fn) { subscribers.push({ keys, fn }); return () => {}; },
    command() { return "cmd-ci-1"; },
    pendingFor() { return null; },
  };
  const ctxObj = {
    S: { ci: [] },
    Date: FixedDate,
    Math,
    console,
    document: {
      getElementById: (id) => elements[id] || null,
      createElement: (tag) => makeElement(tag, ""),
    },
    store: storeObj,
  };
  const ctx = vm.createContext(ctxObj);
  vm.runInContext(utilSrc, ctx);
  vm.runInContext(ciSrc, ctx);

  return {
    elements,
    pushGate(data) {
      storeObj.blocks.ci_gate = { data };
      for (const s of subscribers) {
        if (s.keys.includes("block:ci_gate")) s.fn();
      }
    },
    findRateLimitBadge() {
      for (const id of hdrIds) {
        if (id === "ci-gate-badge" || id === "btn-gate-toggle") continue;
        const el = elements[id];
        if (el && (el.textContent.includes("GH API") || el.innerHTML.includes("GH API"))) return el;
      }
      if (elements["ci-gate-meta"].textContent.includes("GH API") || elements["ci-gate-meta"].innerHTML.includes("GH API")) {
        return elements["ci-gate-meta"];
      }
      return null;
    },
  };
}

const h = createHeaderHarness();

const cases = [
  { rl: { remaining: 5000, limit: 5000, reset_at: "2026-11-26T01:00:00Z", throttled: false }, text: "GH API 5000/5000", cls: "badge-green" },
  { rl: { remaining: 1500, limit: 5000, reset_at: "2026-11-26T01:00:00Z", throttled: false }, text: "GH API 1500/5000", cls: "badge-green" },
  { rl: { remaining: 1499, limit: 5000, reset_at: "2026-11-26T01:00:00Z", throttled: false }, text: "GH API 1499/5000", cls: "badge-yellow" },
  { rl: { remaining: 500, limit: 5000, reset_at: "2026-11-26T01:00:00Z", throttled: false }, text: "GH API 500/5000", cls: "badge-yellow" },
  { rl: { remaining: 499, limit: 5000, reset_at: "2026-11-26T01:00:00Z", throttled: false }, text: "GH API 499/5000", cls: "badge-red" },
  { rl: { remaining: 0, limit: 5000, reset_at: "2026-11-26T00:35:00Z", throttled: true }, text: "GH API 0/5000", cls: "badge-red", countdown: true },
  { rl: { remaining: 3000, limit: 5000, reset_at: "2026-11-26T00:35:00Z", throttled: true }, text: "GH API 3000/5000", cls: "badge-red", countdown: true },
];

for (const c of cases) {
  h.pushGate({ running: true, budget: 12, cap: 8, rate_limit: c.rl, repos: [] });
  const badge = h.findRateLimitBadge();
  assert.ok(badge, `expected GH API badge in CI gate header for ${JSON.stringify(c.rl)}`);
  const fullText = badge.textContent || badge.innerHTML;
  assert.ok(fullText.includes(c.text), `expected ${c.text} in ${fullText}`);
  assert.ok(badge.classList.contains(c.cls) || badge.innerHTML.includes(c.cls), `expected ${c.cls} for ${JSON.stringify(c.rl)}, got class=${badge.className}`);
  assert.notStrictEqual(badge.style.display, "none");
  if (c.countdown) {
    assert.match(fullText, /\b10m\b|\b600s\b|reset/i, `expected reset countdown in ${fullText}`);
  }
}

// Backward compatibility: absent/null/empty rate_limit degrades cleanly
for (const bad of [undefined, null, {}, { remaining: null, limit: null }]) {
  h.pushGate({ running: true, budget: 12, cap: 8, rate_limit: bad, repos: [] });
  const badge = h.findRateLimitBadge();
  if (badge) {
    assert.ok(
      badge.style.display === "none" || !badge.textContent.includes("GH API"),
      `rate_limit=${JSON.stringify(bad)} must hide or clear GH API badge, got text=${badge.textContent} display=${badge.style.display}`
    );
  }
}
"""
        import json
        p = subprocess.run(
            ["node", "-e", js, text("static/util.js"), text("static/ci.js"), html, json.dumps(hdr_ids)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)


if __name__ == "__main__":
    unittest.main()
