#!/usr/bin/env python3
"""Tests for sw/core.py (issue #9, part A): the pure core, no I/O.

Conventions assumed from the issue blueprint: a command is a dict with the columns of the
`commands` table, `zones` and `waiting` as lists; a stored block row is a dict
{"json", "pass_seq"}; blocks passed to assemble_legacy are {name: data}; the ring is a list of
(rev, frame) pairs; prune rows are dicts with id, state, rev.
"""

import importlib
import inspect
import json
import unittest

MISSING = "sw/core.py is missing or lacks %s (issue #9 part A: pure core)"


def core(case, *names):
    try:
        mod = importlib.import_module("sw.core")
    except ImportError as e:
        case.fail(MISSING % ("the module: " + str(e)))
    for n in names:
        if not hasattr(mod, n):
            case.fail(MISSING % n)
    return mod


def cmd(zones=("ci_gate",), state="running", **kw):
    c = {"id": "cmd-00000001", "route": "/api/ci", "body": "{}", "zones": list(zones), "waiting": list(zones),
         "state": state, "message": None, "created_at": 100.0, "deadline_at": 250.0,
         "finished_seq": None, "done_at": None, "rev": 1}
    c.update(kw)
    return c


class TestCanonical(unittest.TestCase):
    def test_sorted_compact_unicode(self):
        c = core(self, "canonical")
        self.assertEqual(c.canonical({"b": 1, "a": "é"}), '{"a":"é","b":1}')


class TestDecidePut(unittest.TestCase):
    def row(self, c, data, seq):
        return {"json": c.canonical(data), "pass_seq": seq}

    def test_no_previous_row_is_changed(self):
        c = core(self, "decide_put", "canonical")
        self.assertEqual(c.decide_put(None, c.canonical({"a": 1}), 1), "changed")

    def test_same_json_newer_pass_is_same(self):
        c = core(self, "decide_put", "canonical")
        self.assertEqual(c.decide_put(self.row(c, {"a": 1}, 5), c.canonical({"a": 1}), 7), "same")

    def test_stale_pass_is_older_even_when_different(self):
        c = core(self, "decide_put", "canonical")
        self.assertEqual(c.decide_put(self.row(c, {"a": 1}, 9), c.canonical({"a": 2}), 7), "older")

    def test_pass_reset_at_boot_is_changed(self):
        c = core(self, "decide_put", "canonical")
        self.assertEqual(c.decide_put(self.row(c, {"a": 1}, 0), c.canonical({"a": 2}), 1), "changed")

    def test_key_order_does_not_matter(self):
        c = core(self, "decide_put", "canonical")
        prev = self.row(c, {"a": 1, "b": 2}, 5)
        self.assertEqual(c.decide_put(prev, c.canonical({"b": 2, "a": 1}), 6), "same")


class TestPassCounts(unittest.TestCase):
    def test_vectors(self):
        c = core(self, "pass_counts")
        self.assertIs(c.pass_counts(10, 12), False)
        self.assertIs(c.pass_counts(12, 12), False)
        self.assertIs(c.pass_counts(13, 12), True)
        self.assertIs(c.pass_counts(13, None), False)


class TestCommandLifecycle(unittest.TestCase):
    def test_pass_started_before_finish_does_not_confirm(self):
        c = core(self, "cmd_on_finish", "cmd_on_pass")
        x = c.cmd_on_finish(cmd(), 0, "done", 12, 200.0)
        self.assertEqual(x["state"], "running")
        x = c.cmd_on_pass(x, "ci_gate", 10, 201.0)
        self.assertEqual(x["state"], "running")

    def test_pass_started_after_finish_confirms(self):
        c = core(self, "cmd_on_finish", "cmd_on_pass")
        x = c.cmd_on_finish(cmd(), 0, "done", 12, 200.0)
        x = c.cmd_on_pass(x, "ci_gate", 13, 201.0)
        self.assertEqual(x["state"], "ok")

    def test_pass_started_at_finish_seq_does_not_confirm(self):
        c = core(self, "cmd_on_finish", "cmd_on_pass")
        x = c.cmd_on_finish(cmd(), 0, "done", 12, 200.0)
        self.assertEqual(c.cmd_on_pass(x, "ci_gate", 12, 201.0)["state"], "running")

    def test_nonzero_rc_fails_with_output_tail(self):
        c = core(self, "cmd_on_finish")
        x = c.cmd_on_finish(cmd(), 1, "boom: no checkout", 12, 200.0)
        self.assertEqual(x["state"], "failed")
        self.assertIn("no checkout", x["message"])

    def test_no_zones_ok_at_once(self):
        c = core(self, "cmd_on_finish")
        self.assertEqual(c.cmd_on_finish(cmd(zones=()), 0, "sent", 12, 200.0)["state"], "ok")

    def test_every_zone_must_count(self):
        c = core(self, "cmd_on_finish", "cmd_on_pass")
        x = c.cmd_on_finish(cmd(zones=("a", "b")), 0, "done", 12, 200.0)
        x = c.cmd_on_pass(x, "a", 13, 201.0)
        self.assertEqual(x["state"], "running")
        x = c.cmd_on_pass(x, "b", 14, 202.0)
        self.assertEqual(x["state"], "ok")

    def test_pass_before_the_command_finished_is_ignored(self):
        c = core(self, "cmd_on_pass")
        x = c.cmd_on_pass(cmd(), "ci_gate", 99, 201.0)
        self.assertEqual(x["state"], "running")


class TestDeadlineAndBoot(unittest.TestCase):
    def test_deadline_fails_running_command(self):
        c = core(self, "cmd_on_deadline")
        x = c.cmd_on_deadline(cmd(), 251.0, {})
        self.assertEqual(x["state"], "failed")
        self.assertIn("not confirmed", x["message"])

    def test_deadline_includes_block_error(self):
        c = core(self, "cmd_on_deadline")
        x = c.cmd_on_deadline(cmd(), 251.0, {"ci_gate": "gh: rate limited"})
        self.assertIn("not confirmed", x["message"])
        self.assertIn("rate limited", x["message"])

    def test_deadline_before_time_changes_nothing(self):
        c = core(self, "cmd_on_deadline")
        self.assertEqual(c.cmd_on_deadline(cmd(), 200.0, {})["state"], "running")

    def test_deadline_leaves_ok_unchanged(self):
        c = core(self, "cmd_on_deadline")
        done = cmd(state="ok")
        self.assertEqual(c.cmd_on_deadline(done, 9999.0, {}), done)

    def test_boot_fails_running(self):
        c = core(self, "cmd_on_boot")
        x = c.cmd_on_boot(cmd())
        self.assertEqual(x["state"], "failed")
        self.assertIn("server restarted", x["message"])

    def test_boot_leaves_ok_unchanged(self):
        c = core(self, "cmd_on_boot")
        done = cmd(state="ok")
        self.assertEqual(c.cmd_on_boot(done), done)

    def test_cmd_on_boot_sets_done_at(self):
        c = core(self, "cmd_on_boot")
        x = c.cmd_on_boot(cmd())
        self.assertEqual(x["state"], "failed")
        self.assertIsInstance(x["done_at"], float)
        self.assertGreater(x["done_at"], 1.0)


class TestPrunePlan(unittest.TestCase):
    def test_prunes_oldest_terminal_never_running(self):
        c = core(self, "prune_plan")
        rows = [{"id": "t%03d" % i, "state": "ok" if i % 2 else "failed", "rev": i} for i in range(205)]
        rows += [{"id": "r%d" % i, "state": "running", "rev": i} for i in range(3)]
        plan = c.prune_plan(rows, 200)
        self.assertEqual(sorted(plan), ["t%03d" % i for i in range(5)])


CI_LIST = """\
WARNING: something
gate running unlocked
hanzhad/host_repo          docker CI_RUNNER=1 runners=2
hanzhad/ops-console        CI_RUNNER=0 runners=0
hanzhad/demo-app-ai-extension  CI_RUNNER=1 runners=1
"""


class TestParseCI(unittest.TestCase):
    def test_list_with_skipped_repo(self):
        c = core(self, "parse_ci_list")
        r = c.parse_ci_list(CI_LIST, {"hanzhad/demo-app-ai-extension"})
        self.assertEqual(r["repos"], [{"repo": "hanzhad/host_repo", "local": True, "runners": 2},
                                      {"repo": "hanzhad/ops-console", "local": False, "runners": 0}])
        self.assertTrue(r["running"])
        self.assertFalse(r["locked"])
        self.assertEqual(r["gate"], "running unlocked")

    def test_empty_list(self):
        c = core(self, "parse_ci_list")
        r = c.parse_ci_list("", set())
        self.assertEqual(r["gate"], "unknown")
        self.assertEqual(r["repos"], [])

    def test_budget_live(self):
        c = core(self, "parse_ci_budget")
        r = c.parse_ci_budget("budget 10 cores, big cap 8\nlive 12 8\n")
        self.assertEqual((r["budget"], r["cap"], r["live"], r["boot"]), (12, 8, True, [10, 8]))

    def test_budget_boot_only(self):
        c = core(self, "parse_ci_budget")
        r = c.parse_ci_budget("budget 10 cores, big cap 8\n")
        self.assertEqual((r["budget"], r["cap"], r["live"], r["boot"]), (10, 8, False, [10, 8]))

    def test_budget_neither(self):
        c = core(self, "parse_ci_budget")
        r = c.parse_ci_budget("nothing useful\n")
        self.assertEqual((r["budget"], r["cap"], r["live"], r["boot"]), (None, None, False, None))

    def test_queue_tsv_drops_malformed_and_non_digit(self):
        c = core(self, "parse_queue_tsv")
        out = "101\t2026-01-02T00:00:00Z\tci\tmain\nabc\t2026-01-01T00:00:00Z\tci\tmain\nshort\trow\n"
        items = c.parse_queue_tsv("a/b", out)
        self.assertEqual(items, [{"repo": "a/b", "id": 101, "created": "2026-01-02T00:00:00Z",
                                  "name": "ci", "branch": "main"}])

    def test_sort_queue_oldest_first(self):
        c = core(self, "sort_queue")
        items = [{"created": "2026-02"}, {"created": "2026-01"}]
        self.assertEqual([i["created"] for i in c.sort_queue(items)], ["2026-01", "2026-02"])


class TestValidateCI(unittest.TestCase):
    def test_valid(self):
        c = core(self, "validate_ci")
        for ok in (["on", "a/b"], ["off", "a/b"], ["pause"], ["resume"], ["reset"], ["budget", "12", "8"]):
            self.assertTrue(c.validate_ci(ok), ok)

    def test_invalid(self):
        c = core(self, "validate_ci")
        for bad in (["budget", "8", "10"], ["budget", "12", "3"], ["on", "a/b;rm"], [], ["on"], ["nope"]):
            self.assertFalse(c.validate_ci(bad), bad)


class TestLegacy(unittest.TestCase):
    def test_gate_is_ci_gate_plus_queue_rest_from_legacy(self):
        c = core(self, "assemble_legacy")
        queue = [{"repo": "a/b", "id": 1}]
        out = c.assemble_legacy({"legacy": {"ts": 1, "slots": [7], "gate": "stale"},
                                 "ci_gate": {"gate": "running", "repos": []}, "gh_queue": queue}, {"now": 5})
        self.assertEqual(out["gate"], {"gate": "running", "repos": [], "queue": queue})
        self.assertEqual(out["slots"], [7])
        self.assertEqual(out["now"], 5)

    def test_no_blocks_gives_todays_defaults(self):
        c = core(self, "assemble_legacy")
        out = c.assemble_legacy({}, {})
        self.assertEqual(out["slots"], [])
        self.assertEqual(out["sessions"], [])
        self.assertEqual(out["machine_max"], 20)
        self.assertEqual(out["machine_in_use"], 0)

    def test_legacy_without(self):
        c = core(self, "legacy_without")
        self.assertEqual(c.legacy_without({"ci_gate", "gh_queue"}), ["gate"])


class TestSSE(unittest.TestCase):
    def test_frames_after_no_gap(self):
        c = core(self, "frames_after")
        ring = [(r, b"frame%d" % r) for r in range(5, 10)]
        frames, gap = c.frames_after(ring, 7)
        self.assertEqual(frames, [b"frame8", b"frame9"])
        self.assertFalse(gap)

    def test_frames_after_gap(self):
        c = core(self, "frames_after")
        ring = [(r, b"frame%d" % r) for r in range(5, 10)]
        _, gap = c.frames_after(ring, 2)
        self.assertTrue(gap)

    def test_sse_frame_format(self):
        c = core(self, "sse_frame")
        f = c.sse_frame("block", {"name": "ci_gate"}, 7)
        self.assertIsInstance(f, bytes)
        self.assertIn(b"event: block\n", f)
        self.assertIn(b"id: 7\n", f)
        self.assertTrue(f.endswith(b"\n\n"))
        data = [l for l in f.decode().splitlines() if l.startswith("data: ")][0][6:]
        self.assertEqual(json.loads(data), {"name": "ci_gate"})

    def test_cmd_id_ok(self):
        c = core(self, "cmd_id_ok")
        self.assertFalse(c.cmd_id_ok(""))
        self.assertFalse(c.cmd_id_ok("a" * 65))
        self.assertFalse(c.cmd_id_ok("a b"))
        self.assertFalse(c.cmd_id_ok("short"))
        self.assertTrue(c.cmd_id_ok("acc-ci-0001"))
        self.assertTrue(c.cmd_id_ok("a" * 64))


class TestLimitsAndSessionsBlock(unittest.TestCase):
    def test_build_project_limits_is_removed(self):
        c = core(self, "slots_block")
        self.assertFalse(hasattr(c, "build_project_limits"))

    def test_slots_block_two_arg_clamps_zero_negative_and_rejects_bool(self):
        c = core(self, "slots_block")
        self.assertEqual(len(inspect.signature(c.slots_block).parameters), 2)
        self.assertEqual(c.slots_block([{"id": "x"}], {"machine_max": 5}), {"slots": [{"id": "x"}], "machine_max": 5})
        for raw, want in ((0, 0), (-5, 0), (1, 1), (20, 20), (99, 20), ("x", 20), (True, 20), (False, 20)):
            out = c.slots_block([], {"machine_max": raw})
            self.assertEqual(out["machine_max"], want, raw)
            self.assertNotIn("projects", out)
            self.assertNotIn("project_default", out)
        self.assertEqual(c.slots_block([], {})["machine_max"], 20)

    def test_api_state_omits_projects_and_project_default(self):
        c = core(self, "api_state")
        out = c.api_state({"slots": {"slots": [{"id": "s"}], "machine_max": 12}}, [], [], 5.0, None)
        self.assertNotIn("projects", out)
        self.assertNotIn("projects", out["snap"])
        self.assertNotIn("project_default", out)
        self.assertNotIn("project_default", out["snap"])


class TestSessionsBlock(unittest.TestCase):
    def test_sessions_block_returns_sessions_and_clamped_default_limit(self):
        c = core(self, "sessions_block")
        rows = [{"id": "s1", "held_slots": 5, "tree_held": 5}]
        self.assertEqual(c.sessions_block(rows), {"sessions": rows, "default_limit": 1})
        self.assertEqual(c.sessions_block([], {}), {"sessions": [], "default_limit": 1})
        for raw, want in ((-3, 0), (0, 0), (1, 1), (3, 3), (20, 20), (25, 20), ("x", 1), (True, 1), (False, 1), (None, 1)):
            out = c.sessions_block(rows, {"default": raw})
            self.assertEqual(out, {"sessions": rows, "default_limit": want}, raw)

    def test_api_state_and_block_keys_include_default_limit(self):
        c = core(self, "api_state", "BLOCK_KEYS")
        self.assertEqual(c.BLOCK_KEYS.get("sessions"), ["sessions", "default_limit"])
        rows = [{"id": "s1", "held_slots": 5, "tree_held": 5}]

        empty = c.api_state({}, [], [], 5.0, None)
        self.assertEqual(empty["sessions"], [])
        self.assertEqual(empty["default_limit"], 1)
        self.assertEqual(empty["snap"]["default_limit"], 1)

        zero = c.api_state({"sessions": {"sessions": rows, "default_limit": 0}}, [], [], 5.0, None)
        self.assertEqual(zero["sessions"], rows)
        self.assertEqual(zero["default_limit"], 0)
        self.assertEqual(zero["snap"]["default_limit"], 0)

        three = c.api_state({"sessions": {"sessions": rows, "default_limit": 3}}, [], [], 5.0, None)
        self.assertEqual(three["default_limit"], 3)
        self.assertEqual(three["snap"]["default_limit"], 3)


if __name__ == "__main__":
    unittest.main()
