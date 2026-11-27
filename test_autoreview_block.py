#!/usr/bin/env python3
"""Tests for issue #9 part C: auto-review as a block.

Layers: pure (sw.domains.autoreview registration, the command lifecycle with sw.core, the legacy key
list) and structural (source text of bin/autoreview.py, static/*.js, page.html). Nothing here starts a
thread, a server or a subprocess; the reader is called with bin/autoreview.status patched.

Assumed names (the implementer must match):
  sw.domains.autoreview.BLOCKS: one BlockSpec "autoreview", period 15, after=(), read(store) -> autoreview.status()
  sw.domains.autoreview.ROUTES["/api/autoreview"]: zones(args) == ("autoreview",), deadline_s == 90
  bin/autoreview.py: status() stays (the block's reader and the CLI call it); no CACHE, no autoreview.json
  static/autoreview.js: onState(['block:autoreview', 'pending'], ...); topbar.js also follows block:autoreview
"""

import importlib
import pathlib
import re
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent
BIN = ROOT / "bin"

SAMPLE_ARGS = [["start"], ["stop"], ["restart"], ["sync-tokens"], ["pause"], ["resume"],
               ["set-global", "harness", "claude"], ["set-global", "model", "opus"],
               ["toggle", "a/b"], ["forget", "a/b"], ["tick", "a/b"], ["set", "a/b", "all", "true"],
               ["adopt", "a/b", "--all"]]


def load(case, name, criterion):
    try:
        return importlib.import_module(name)
    except ImportError as e:
        case.fail("%s: cannot import %s (%s)" % (criterion, name, e))


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def ar_domain(case):
    return load(case, "sw.domains.autoreview", "part C: autoreview domain")


def ar_route(case):
    return ar_domain(case).ROUTES["/api/autoreview"]


def ar_cmd(zones, **kw):
    c = {"id": "cmd-00000002", "route": "/api/autoreview", "body": "{}", "zones": list(zones),
         "waiting": list(zones), "state": "running", "message": None, "created_at": 100.0,
         "deadline_at": 190.0, "finished_seq": None, "done_at": None, "rev": 1}
    c.update(kw)
    return c


def bin_autoreview(case):
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    return load(case, "autoreview", "bin/autoreview.py")


class TestAutoreviewRegistration(unittest.TestCase):
    def test_one_block_named_autoreview(self):
        self.assertEqual([b.name for b in ar_domain(self).BLOCKS], ["autoreview"])

    def test_period_is_15_seconds(self):
        blocks = ar_domain(self).BLOCKS
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].period, 15)

    def test_reader_is_callable_and_woken_by_nothing(self):
        blocks = ar_domain(self).BLOCKS
        self.assertEqual(len(blocks), 1)
        self.assertTrue(callable(blocks[0].read))
        self.assertEqual(tuple(blocks[0].after), ())

    def test_reader_returns_what_bin_autoreview_status_returns(self):
        blocks = ar_domain(self).BLOCKS
        self.assertEqual(len(blocks), 1)
        mod = bin_autoreview(self)
        with patch.object(mod, "status", return_value={"container": {"running": True}}) as st:
            out = blocks[0].read(None)
        self.assertEqual(out, {"container": {"running": True}})
        st.assert_called_once_with()

    def test_block_is_discovered_with_the_other_domains(self):
        domains = load(self, "sw.domains", "discover")
        names = [b.name for b in domains.discover()[0]]
        self.assertIn("autoreview", names)
        self.assertEqual(names.count("autoreview"), 1)


class TestAutoreviewRoute(unittest.TestCase):
    def test_zone_is_autoreview_not_legacy_for_every_verb(self):
        route = ar_route(self)
        for args in SAMPLE_ARGS:
            self.assertEqual(tuple(route.zones(args)), ("autoreview",), args)

    def test_deadline_90(self):
        self.assertEqual(ar_route(self).deadline_s, 90)

    def test_validator_unchanged(self):
        v = ar_route(self).validate
        self.assertEqual(v({"id": "acc-ar-0001", "args": ["set-global", "model", "opus"]}),
                         ["set-global", "model", "opus"])
        self.assertEqual(v({"args": ["adopt", "a/b", "--all", "--self-review"]}),
                         ["adopt", "a/b", "--all", "--self-review"])
        self.assertIsNone(v({"args": ["set-global", "color", "red"]}))
        self.assertIsNone(v({"args": ["toggle", "a/b;rm"]}))
        self.assertIsNone(v({"args": ["set", "a/b", "all", "maybe"]}))
        self.assertIsNone(v({"args": "start"}))


class TestLegacyNoLongerCollectsAutoreview(unittest.TestCase):
    def test_without_list_for_the_registered_blocks_includes_autoreview(self):
        core = load(self, "sw.core", "legacy without")
        domains = load(self, "sw.domains", "legacy without")
        names = [b.name for b in domains.discover()[0]]
        self.assertIn("autoreview", core.legacy_without(names),
                      "part C: the legacy snapshot run must be told --without autoreview once the block is registered")

    def test_without_key_for_the_block_alone(self):
        core = load(self, "sw.core", "legacy without")
        self.assertEqual(core.legacy_without({"autoreview"}), ["autoreview"])

    def test_assemble_legacy_takes_autoreview_from_the_block(self):
        core = load(self, "sw.core", "assemble_legacy")
        out = core.assemble_legacy({"legacy": {"slots": [1], "autoreview": {"stale": True}},
                                    "autoreview": {"container": {"running": True}}}, {"now": 5})
        self.assertEqual(out["autoreview"], {"container": {"running": True}})
        self.assertEqual(out["slots"], [1])


class TestCacheRaceLifecycle(unittest.TestCase):
    """A pass of the autoreview zone that started before the command finished must not confirm it."""

    def finished(self):
        core = load(self, "sw.core", "cache race")
        zones = ar_route(self).zones(["toggle", "a/b"])
        return core, core.cmd_on_finish(ar_cmd(zones), 0, "toggled", 12, 200.0)

    def test_pass_started_before_the_command_finished_does_not_confirm(self):
        core, c = self.finished()
        c = core.cmd_on_pass(c, "autoreview", 10, 201.0)
        self.assertEqual(c["state"], "running")
        c = core.cmd_on_pass(c, "autoreview", 12, 201.0)
        self.assertEqual(c["state"], "running")

    def test_pass_started_after_finish_confirms(self):
        core, c = self.finished()
        c = core.cmd_on_pass(c, "autoreview", 10, 201.0)
        c = core.cmd_on_pass(c, "autoreview", 13, 202.0)
        self.assertEqual(c["state"], "ok")

    def test_legacy_pass_never_confirms_it(self):
        core, c = self.finished()
        c = core.cmd_on_pass(c, "legacy", 99, 201.0)
        self.assertEqual(c["state"], "running")


class TestFileCacheGone(unittest.TestCase):
    def test_bin_autoreview_has_no_cache_or_json_file(self):
        self.assertNotRegex(text("bin/autoreview.py"), r"CACHE|autoreview\.json")

    def test_no_cache_read_write_or_unlink_left(self):
        src = text("bin/autoreview.py")
        self.assertNotIn("unlink", src)
        self.assertNotRegex(src, r"write_text|read_text")

    def test_module_has_no_cache_attribute_and_keeps_status(self):
        mod = bin_autoreview(self)
        self.assertFalse(hasattr(mod, "CACHE"), "bin/autoreview.py must not define CACHE")
        self.assertTrue(callable(getattr(mod, "status", None)))

    def test_domain_reader_calls_status(self):
        self.assertRegex(text("sw/domains/autoreview.py"), r"\.status\(\)")


class TestPageAutoreview(unittest.TestCase):
    def set_global_body(self):
        src = text("static/autoreview.js")
        m = re.search(r"function\s+setGlobal\b.*?(?=\nfunction\s+renderAutoReview)", src, re.S)
        self.assertIsNotNone(m, "setGlobal must stay defined before renderAutoReview")
        return m.group(0)

    def test_subscribes_to_block_autoreview_not_legacy(self):
        src = text("static/autoreview.js")
        self.assertIn("block:autoreview", src)
        self.assertNotIn("block:legacy", src)

    def test_topbar_follows_block_autoreview(self):
        """The topbar badge reads S.autoreview, which no longer arrives with block:legacy."""
        self.assertIn("block:autoreview", text("static/topbar.js"))

    def test_set_global_goes_through_store_command_with_pending_key(self):
        body = self.set_global_body()
        self.assertIn("store.command(", body)
        self.assertRegex(body, r"['\"]ar\.global\.['\"]\s*\+\s*key")

    def test_set_global_has_no_manual_disable_or_refresh(self):
        body = self.set_global_body()
        self.assertNotRegex(body, r"\.disabled\s*=")
        self.assertNotIn("await", body)
        self.assertNotIn("refresh", body)

    def test_selects_locked_from_pending_in_the_renderer(self):
        src = text("static/autoreview.js")
        self.assertIn("pendingFor('ar.global.harness')", src)
        self.assertIn("pendingFor('ar.global.model')", src)

    def test_no_cache_json_or_await_refresh_anywhere(self):
        files = ["bin/autoreview.py", "page.html"] + ["static/" + p.name for p in sorted((ROOT / "static").glob("*.js"))]
        for rel in files:
            self.assertNotRegex(text(rel), r"CACHE|autoreview\.json|await refresh", rel)


if __name__ == "__main__":
    unittest.main()
