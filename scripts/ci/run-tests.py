#!/usr/bin/env python3
"""Discover and run root unittest files, emitting a JSON report."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import unittest


def discover_root_test_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return sorted tests/test_*.py files (non-recursive)."""
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    return sorted(
        p for p in tests_dir.iterdir()
        if p.is_file() and p.name.startswith("test_") and p.suffix == ".py"
    )


def build_report(leg: str, entries: list[dict[str, object]]) -> dict[str, object]:
    """Build the JSON report payload from per-file result entries."""
    return {"leg": leg, "files": entries}


def report_has_problems(entries: list[dict[str, object]]) -> bool:
    """Return True when zero tests ran or any file had failures or errors."""
    if not entries:
        return True
    total_tests = sum(int(e["tests"]) for e in entries)
    if total_tests <= 0:
        return True
    return any(int(e["failures"]) > 0 or int(e["errors"]) > 0 for e in entries)


def run_test_file(root: pathlib.Path, path: pathlib.Path, verbosity: int = 1) -> dict[str, object]:
    """Load and run a single root test_*.py file via unittest."""
    start = time.monotonic()
    loader = unittest.TestLoader()
    tests_dir = str(path.parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    suite = loader.loadTestsFromName(path.stem)
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=verbosity)
    result = runner.run(suite)
    duration_s = round(max(0.0, time.monotonic() - start), 4)
    return {
        "file": path.relative_to(root).as_posix(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "duration_s": duration_s,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run root test_*.py files and write a JSON report.")
    parser.add_argument("--report", required=True, help="Output path for the JSON test report.")
    parser.add_argument("--leg", default="all", help="Leg name recorded in the report (default: all).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose unittest output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = pathlib.Path.cwd()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    test_files = discover_root_test_files(root)
    verbosity = 2 if args.verbose else 1
    entries = [run_test_file(root, path, verbosity=verbosity) for path in test_files]

    report_path = pathlib.Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(build_report(args.leg, entries), indent=2) + "\n", encoding="utf-8")

    return 1 if report_has_problems(entries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
