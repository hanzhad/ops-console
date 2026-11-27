#!/usr/bin/env python3
"""Tests for the layout of the split (issue #9, part A): line-count ratchet, package shape,
removed machinery, domain registry. There is no CI here, so this walks the tree.
"""

import importlib
import os
import pathlib
import pkgutil
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
FROZEN = {"bin/engine.py": 631}  # existing files over the limit: a ratchet
DOMAINS = ["ci", "autoreview", "slots", "sessions", "cleaners", "host", "runners", "messaging"]
STATIC = ["util", "store", "app", "topbar", "slots", "sessions", "cleaners", "ci", "autoreview", "log"]
FORBIDDEN = re.compile(r'snapshot_loop|setInterval\(refresh|renderAll|state\["snap"\]')
PATTERN = re.compile(r"snapshot|optimisticLimits|checks_lock")
SKIP_DIRS = {".git", "docs", "__pycache__", "node_modules"}


def lines(p):
    return len(p.read_text(encoding="utf-8").splitlines())


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _source_files():
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if (f.startswith("test_") and f.endswith(".py")) or f.endswith((".pyc", ".db", ".png")):
                continue
            yield pathlib.Path(dirpath, f)


def _grep_hits(pattern, only=None):
    out = []
    for p in _source_files():
        rel = p.relative_to(ROOT).as_posix()
        if only and not only(rel):
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    out.append("%s:%d: %s" % (rel, i, line.strip()[:100]))
        except (UnicodeDecodeError, OSError):
            pass
    return out


def mod(case, name):
    try:
        return importlib.import_module(name)
    except ImportError as e:
        case.fail("%s is missing (issue #9 part A): %s" % (name, e))


class TestLineLimits(unittest.TestCase):
    def check(self, p, limit):
        rel = p.relative_to(ROOT).as_posix()
        limit = max(limit, FROZEN.get(rel, 0))
        self.assertLessEqual(lines(p), limit, "%s has %d lines, limit %d" % (rel, lines(p), limit))

    def test_new_python_and_js_files_at_most_400(self):
        files = list((ROOT / "sw").rglob("*.py")) + list((ROOT / "bin").glob("*.py")) + list((ROOT / "static").glob("*.js"))
        self.assertTrue(files)
        for p in files:
            self.check(p, 400)

    def test_server_entry_point_at_most_80(self):
        self.check(ROOT / "server.py", 80)

    def test_page_html_at_most_1200(self):
        self.check(ROOT / "page.html", 1200)

    def test_ratchet_files_do_not_grow(self):
        for rel, n in FROZEN.items():
            self.assertLessEqual(lines(ROOT / rel), n, rel)


class TestPackageShape(unittest.TestCase):
    def test_modules_exist(self):
        for m in ("core", "store", "bus", "watch", "commands", "http"):
            self.assertTrue((ROOT / "sw" / (m + ".py")).is_file(), "sw/%s.py" % m)

    def test_domain_modules_export_blocks_and_routes(self):
        pkg = mod(self, "sw.domains")
        found = {m.name for m in pkgutil.iter_modules(pkg.__path__)}
        self.assertEqual(found, set(DOMAINS))
        for name in DOMAINS:
            d = mod(self, "sw.domains." + name)
            self.assertIsInstance(d.BLOCKS, list, name)
            self.assertIsInstance(d.ROUTES, dict, name)

    def test_import_has_no_side_effects(self):
        home = tempfile.mkdtemp()
        code = ("import threading, sys\n"
                "sys.path.insert(0, %r)\n"
                "import sw, server\n"
                "print(threading.active_count())\n" % str(ROOT))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=home,
                           env={**os.environ, "HOME": home}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertEqual(r.stdout.strip(), "1", "import started threads")
        self.assertFalse(list(pathlib.Path(home).rglob("state.db*")), "import opened the database")


class TestRoutes(unittest.TestCase):
    def routes(self):
        out = {}
        for name in DOMAINS:
            out.update(mod(self, "sw.domains." + name).ROUTES)
        return out

    def test_existing_routes_are_registered(self):
        r = self.routes()
        for path in ("/api/limit", "/api/cap", "/api/cleaners", "/api/rescue", "/api/send",
                     "/api/ci", "/api/autoreview", "/api/evict"):
            self.assertIn(path, r)
        self.assertNotIn("/api/project-limit", r)

    def test_deadline_table(self):
        r = self.routes()
        want = {"/api/ci": 150, "/api/autoreview": 90, "/api/evict": 960, "/api/cleaners": 960,
                "/api/rescue": 960, "/api/limit": 30, "/api/cap": 30, "/api/send": 30}
        for path, secs in want.items():
            self.assertEqual(r[path].deadline_s, secs, path)

    def test_validators_return_args_or_none(self):
        r = self.routes()
        self.assertIsNotNone(r["/api/cap"].validate({"id": "cmd-00000001", "delta": 1}))
        self.assertIsNone(r["/api/cap"].validate({"id": "cmd-00000001", "delta": 5}))
        self.assertIsNotNone(r["/api/ci"].validate({"id": "cmd-00000001", "args": ["on", "a/b"]}))
        self.assertIsNone(r["/api/ci"].validate({"id": "cmd-00000001", "args": ["on", "a/b;rm"]}))

    def test_send_has_no_zone(self):
        r = self.routes()
        args = r["/api/send"].validate({"id": "cmd-00000001", "to": ["x"], "text": "hi"})
        self.assertIsNotNone(args)
        self.assertEqual(tuple(r["/api/send"].zones(args)), ())

    def test_no_legacy_block_registered(self):
        self.assertFalse((ROOT / "sw" / "domains" / "legacy.py").exists())
        names = [b.name for b in mod(self, "sw.domains").discover()[0]]
        self.assertNotIn("legacy", names)

    def test_snapshot_script_is_gone(self):
        self.assertFalse((ROOT / "bin" / "snapshot.py").exists())
        self.assertTrue((ROOT / "bin" / "slots.py").is_file())


class TestPageSplit(unittest.TestCase):
    def test_page_loads_classic_scripts_in_files(self):
        html = (ROOT / "page.html").read_text(encoding="utf-8")
        self.assertIn("const TOKEN = '__TOKEN__'", html)
        for n in STATIC:
            self.assertTrue((ROOT / "static" / (n + ".js")).is_file(), "static/%s.js" % n)
            self.assertRegex(html, r'<script\s+src="/?static/%s\.js"\s*>' % n)
        self.assertNotRegex(html, r'type="module"')

    def test_store_api_is_present(self):
        self.assertTrue((ROOT / "static" / "store.js").is_file(), "static/store.js")
        js = (ROOT / "static" / "store.js").read_text(encoding="utf-8")
        for token in ("subscribe", "command", "pendingFor", "setUI", "EventSource", "randomUUID", "deadlineAt"):
            self.assertIn(token, js)


class TestRemoved(unittest.TestCase):
    def test_install_bin_is_gone(self):
        self.assertFalse((ROOT / "install-bin.sh").exists())

    def test_no_forbidden_names_remain(self):
        paths = [ROOT / "server.py", ROOT / "page.html"] + list((ROOT / "sw").rglob("*.py")) + list((ROOT / "static").glob("*.js"))
        hits = ["%s:%d" % (p.name, i + 1) for p in paths if p.is_file()
                for i, l in enumerate(p.read_text(encoding="utf-8").splitlines()) if FORBIDDEN.search(l)]
        self.assertEqual(hits, [])

    def test_sw_package_exists_for_the_grep_to_mean_anything(self):
        self.assertTrue((ROOT / "sw").is_dir())
        self.assertTrue((ROOT / "static").is_dir())

    def test_new_scripts_exist(self):
        self.assertTrue((ROOT / "bin" / "slots.py").is_file())
        self.assertTrue((ROOT / "bin" / "runners.py").is_file())


class TestGrepIsEmpty(unittest.TestCase):
    def test_no_snapshot_optimistic_limits_or_checks_lock_anywhere(self):
        found = _grep_hits(PATTERN)
        self.assertEqual(found, [], "part D acceptance grep is not empty:\n" + "\n".join(found[:15]))


class TestDeadChecksMachinery(unittest.TestCase):
    def test_server_py_has_no_checks_state(self):
        self.assertNotRegex(text("server.py"), r"\bchecks\b|\bchecking\b|checks_lock|\bCHECKS\b")

    def test_no_checks_lock_or_CHECKS_constant_in_sw_or_bin(self):
        found = _grep_hits(re.compile(r"checks_lock|\bCHECKS\b"), lambda r: r.endswith(".py"))
        self.assertEqual(found, [])


class TestLegacyMachineryGone(unittest.TestCase):
    def test_nothing_outside_core_uses_legacy_without_or_block_keys(self):
        found = _grep_hits(re.compile(r"legacy_without|BLOCK_KEYS"), lambda r: r != "sw/core.py")
        self.assertEqual(found, [])

    def test_http_does_not_look_up_a_legacy_block(self):
        self.assertNotRegex(text("sw/http.py"), r"[\"']legacy[\"']")

    def test_no_domain_module_names_a_legacy_zone(self):
        found = _grep_hits(re.compile(r"[\"']legacy[\"']"), lambda r: r.startswith("sw/domains/"))
        self.assertEqual(found, [])

    def test_page_assembles_state_without_a_legacy_block(self):
        self.assertNotRegex(text("static/app.js"), r"legacy")


class TestRegate(unittest.TestCase):
    def test_regate_calls_runners_not_snapshot(self):
        src = text("regate.sh")
        self.assertIn("bin/runners.py", src)
        self.assertNotIn("snapshot", src)

    def test_regate_still_counts_busy_runners_and_fails_safe(self):
        src = text("regate.sh")
        self.assertIn("busy", src)
        self.assertIn("|| echo 9", src)


class TestMobileLayoutCss(unittest.TestCase):
    def test_style_braces_balanced_and_safe_area_and_card_tables_present(self):
        html = (ROOT / "page.html").read_text(encoding="utf-8")
        m = re.search(r"<style>(.*?)</style>", html, re.S)
        self.assertIsNotNone(m)
        css = m.group(1)
        self.assertEqual(css.count("{"), css.count("}"), "unbalanced CSS braces in page.html")
        self.assertIn("safe-area-inset-top", css)
        self.assertIn(".stat-item small", css)
        self.assertIn("overflow-x: clip", css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("#ci-queue-tbody", css)
        self.assertIn(r'content: "\21b3  "', css)
        self.assertIsNone(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", css), "control chars in CSS")
        self.assertIn("--depth", css)
        self.assertIn("--depth", (ROOT / "static" / "sessions.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
