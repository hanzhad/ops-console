#!/usr/bin/env python3
"""Tests for CI runner scripts and GitHub Actions workflow (issue #53)."""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
RUN_TESTS = ROOT / "scripts" / "ci" / "run-tests.py"
CHECK_LEGS = ROOT / "scripts" / "ci" / "check-test-legs.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _write_file(directory, name, content):
    path = pathlib.Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestScriptLineLimits(unittest.TestCase):
    def test_ci_scripts_exist_and_within_400_lines(self):
        for script in (RUN_TESTS, CHECK_LEGS):
            self.assertTrue(script.is_file(), f"missing {script.relative_to(ROOT)}")
            line_count = len(script.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(line_count, 400, f"{script.name} has {line_count} lines (limit 400)")


class TestRunTestsScript(unittest.TestCase):
    def _run(self, cwd, report_path):
        self.assertTrue(RUN_TESTS.is_file(), "scripts/ci/run-tests.py missing")
        return subprocess.run(
            [sys.executable, str(RUN_TESTS), "--report", str(report_path)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_discovers_root_tests_writes_json_report_and_ignores_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_file(
                tmp,
                "test_alpha.py",
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_one(self): self.assertEqual(1, 1)\n"
                "    @unittest.skip('reason')\n"
                "    def test_skip(self): pass\n",
            )
            _write_file(
                tmp,
                "test_beta.py",
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_two(self): self.assertTrue(True)\n",
            )
            _write_file(tmp, "helper.py", "raise RuntimeError('should not be imported')\n")
            _write_file(
                tmp,
                "sub/test_nested.py",
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_nested(self): self.fail('should not run')\n",
            )
            report_path = pathlib.Path(tmp) / "report.json"
            proc = self._run(tmp, report_path)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(report_path.is_file(), "report JSON was not written")

            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("leg"), "all")
            files = data.get("files", [])
            by_name = {entry["file"]: entry for entry in files}
            self.assertEqual(set(by_name.keys()), {"test_alpha.py", "test_beta.py"})

            alpha = by_name["test_alpha.py"]
            self.assertEqual(alpha["tests"], 2)
            self.assertEqual(alpha["failures"], 0)
            self.assertEqual(alpha["errors"], 0)
            self.assertEqual(alpha["skipped"], 1)
            self.assertIsInstance(alpha["duration_s"], (int, float))
            self.assertGreaterEqual(alpha["duration_s"], 0)

            beta = by_name["test_beta.py"]
            self.assertEqual(beta["tests"], 1)
            self.assertEqual(beta["failures"], 0)
            self.assertEqual(beta["errors"], 0)
            self.assertEqual(beta["skipped"], 0)

    def test_exits_nonzero_on_test_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_file(
                tmp,
                "test_ok.py",
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self): pass\n",
            )
            _write_file(
                tmp,
                "test_fail.py",
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_fail(self): self.fail('boom')\n",
            )
            report_path = pathlib.Path(tmp) / "report.json"
            proc = self._run(tmp, report_path)
            self.assertNotEqual(proc.returncode, 0, "expected non-zero exit on test failure")
            if report_path.is_file():
                data = json.loads(report_path.read_text(encoding="utf-8"))
                by_name = {e["file"]: e for e in data.get("files", [])}
                self.assertEqual(by_name["test_fail.py"]["failures"], 1)

    def test_exits_nonzero_on_test_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_file(
                tmp,
                "test_err.py",
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_err(self): raise RuntimeError('boom')\n",
            )
            report_path = pathlib.Path(tmp) / "report.json"
            proc = self._run(tmp, report_path)
            self.assertNotEqual(proc.returncode, 0, "expected non-zero exit on test error")

    def test_exits_nonzero_on_zero_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = pathlib.Path(tmp) / "report.json"
            proc = self._run(tmp, report_path)
            self.assertNotEqual(proc.returncode, 0, "expected non-zero exit when 0 tests run")


class TestCheckTestLegsScript(unittest.TestCase):
    def _run(self, report_paths, stdin_text, summary_path=None):
        self.assertTrue(CHECK_LEGS.is_file(), "scripts/ci/check-test-legs.py missing")
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        if summary_path is not None:
            env["GITHUB_STEP_SUMMARY"] = str(summary_path)
        return subprocess.run(
            [sys.executable, str(CHECK_LEGS), *[str(p) for p in report_paths]],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _report(self, directory, name, leg, entries):
        path = pathlib.Path(directory) / name
        path.write_text(json.dumps({"leg": leg, "files": entries}), encoding="utf-8")
        return path

    def test_passes_exact_once_across_reports_and_appends_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1 = self._report(
                tmp,
                "r1.json",
                "leg1",
                [{"file": "test_a.py", "tests": 3, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.1}],
            )
            r2 = self._report(
                tmp,
                "r2.json",
                "leg2",
                [{"file": "test_b.py", "tests": 2, "failures": 0, "errors": 0, "skipped": 1, "duration_s": 0.2}],
            )
            summary = pathlib.Path(tmp) / "summary.md"
            summary.write_text("# Existing Header\n", encoding="utf-8")

            proc = self._run([r1, r2], "test_a.py\ntest_b.py\n", summary_path=summary)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            text = summary.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# Existing Header\n"), "must append, not overwrite")
            self.assertIn("|", text)
            self.assertRegex(text, r"\|[\s:-]+\|")
            self.assertIn("test_a.py", text)
            self.assertIn("test_b.py", text)

    def test_passes_without_github_step_summary_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1 = self._report(
                tmp,
                "r1.json",
                "all",
                [{"file": "test_a.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.05}],
            )
            proc = self._run([r1], "test_a.py\n", summary_path=None)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_fails_on_missing_tracked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1 = self._report(
                tmp,
                "r1.json",
                "all",
                [{"file": "test_a.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.05}],
            )
            proc = self._run([r1], "test_a.py\ntest_missing.py\n")
            self.assertNotEqual(proc.returncode, 0, "expected non-zero when a tracked file was missed")
            self.assertIn("test_missing.py", proc.stdout + proc.stderr)

    def test_fails_on_duplicate_file_within_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"file": "test_dup.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.05}
            r1 = self._report(tmp, "r1.json", "all", [entry, entry])
            proc = self._run([r1], "test_dup.py\n")
            self.assertNotEqual(proc.returncode, 0, "expected non-zero on duplicate file in single report")
            self.assertIn("test_dup.py", proc.stdout + proc.stderr)

    def test_fails_on_duplicate_file_across_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"file": "test_dup.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.05}
            r1 = self._report(tmp, "r1.json", "leg1", [entry])
            r2 = self._report(tmp, "r2.json", "leg2", [entry])
            proc = self._run([r1, r2], "test_dup.py\n")
            self.assertNotEqual(proc.returncode, 0, "expected non-zero on duplicate file across reports")
            self.assertIn("test_dup.py", proc.stdout + proc.stderr)


class TestCiWorkflow(unittest.TestCase):
    def test_workflow_exists_and_matches_contract(self):
        self.assertTrue(WORKFLOW.is_file(), ".github/workflows/ci.yml missing")
        text = WORKFLOW.read_text(encoding="utf-8")

        # Triggers
        self.assertIn("workflow_dispatch", text)
        self.assertIn("pull_request", text)
        for ev in ("opened", "synchronize", "reopened"):
            self.assertIn(ev, text)
        self.assertIn("push", text)
        self.assertIn("main", text)

        # Permissions & concurrency
        self.assertRegex(text, r"permissions:\s*(\{\s*contents:\s*read\s*\}|\n\s+contents:\s*read)")
        self.assertIn("ci-${{ github.ref }}", text)
        self.assertRegex(text, r"cancel-in-progress:\s*true")

        # CI_RUNNER runs-on expression & zero docker calls
        self.assertIn(
            "runs-on: ${{ vars.CI_RUNNER == '1' && (vars.CI_RUNNER_LABEL_LIGHT || 'local-docker-light') || 'ubuntu-latest' }}",
            text,
        )
        self.assertNotRegex(text, r"(?m)^\s*run:.*\bdocker\b")

        # Pinned actions and commands
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1", text)
        self.assertIn("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0", text)
        self.assertRegex(text, r"node-version:\s*['\"]?22['\"]?")
        self.assertIn("python3 scripts/ci/run-tests.py --report test-report.json", text)
        self.assertIn("git ls-files -- 'test_*.py' | python3 scripts/ci/check-test-legs.py test-report.json", text)


if __name__ == "__main__":
    unittest.main()
