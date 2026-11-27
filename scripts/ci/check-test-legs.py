#!/usr/bin/env python3
"""Verify every tracked test_*.py file ran exactly once across CI leg reports."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import pathlib
import sys


def parse_tracked_files(stdin_text: str) -> list[str]:
    """Parse newline-delimited tracked test filenames from stdin."""
    return [line.strip() for line in stdin_text.splitlines() if line.strip()]


def collect_report_rows(reports: list[dict[str, object]]) -> list[tuple[str, dict[str, object]]]:
    """Flatten (leg, file_entry) pairs across loaded JSON reports."""
    rows: list[tuple[str, dict[str, object]]] = []
    for report in reports:
        leg = str(report.get("leg", "unknown"))
        for entry in report.get("files", []):
            rows.append((leg, entry))
    return rows


def evaluate_completeness(
    tracked: list[str],
    rows: list[tuple[str, dict[str, object]]],
) -> tuple[list[str], list[str]]:
    """Return (missed, duplicated) lists for tracked test files."""
    counts = Counter(str(entry["file"]) for _, entry in rows)
    missed = [name for name in tracked if counts[name] == 0]
    duplicated = sorted(name for name, count in counts.items() if count > 1)
    return missed, duplicated


def format_summary_markdown(rows: list[tuple[str, dict[str, object]]]) -> str:
    """Render a Markdown summary table for GITHUB_STEP_SUMMARY."""
    lines = [
        "### Test Legs Summary",
        "",
        "| File | Leg | Tests | Failures | Errors | Skipped | Duration (s) |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for leg, entry in rows:
        lines.append(
            f"| {entry['file']} | {leg} | {entry.get('tests', 0)} | "
            f"{entry.get('failures', 0)} | {entry.get('errors', 0)} | "
            f"{entry.get('skipped', 0)} | {float(entry.get('duration_s', 0.0)):.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check that every tracked test_*.py ran exactly once.")
    parser.add_argument("reports", nargs="+", help="One or more JSON report files from run-tests.py.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tracked = parse_tracked_files(sys.stdin.read())
    reports = [json.loads(pathlib.Path(p).read_text(encoding="utf-8")) for p in args.reports]
    rows = collect_report_rows(reports)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(format_summary_markdown(rows))

    missed, duplicated = evaluate_completeness(tracked, rows)
    if missed:
        print(f"Missed test files: {', '.join(missed)}", file=sys.stderr)
    if duplicated:
        print(f"Duplicated test files: {', '.join(duplicated)}", file=sys.stderr)

    return 1 if (missed or duplicated) else 0


if __name__ == "__main__":
    raise SystemExit(main())
