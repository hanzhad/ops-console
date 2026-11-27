#!/usr/bin/env python3
"""Tests for bin/winproc.py (host_repo#278 task 1): pure parsing and a safe shell wrapper."""

import contextlib
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

SW_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = SW_ROOT / "bin"
for p in (str(SW_ROOT), str(BIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import winproc  # noqa: E402

POWERSHELL_FALLBACK = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


@contextlib.contextmanager
def _patch_paths(present, managed):
    orig_exists = os.path.exists
    orig_isfile = os.path.isfile
    orig_access = os.access
    orig_path_exists = pathlib.Path.exists
    orig_path_isfile = pathlib.Path.is_file

    def _exists(p):
        s = os.fspath(p)
        return (s in present) if s in managed else orig_exists(p)

    def _isfile(p):
        s = os.fspath(p)
        return (s in present) if s in managed else orig_isfile(p)

    def _access(p, mode):
        s = os.fspath(p)
        return (s in present) if s in managed else orig_access(p, mode)

    def _path_exists(self):
        s = str(self)
        return (s in present) if s in managed else orig_path_exists(self)

    def _path_isfile(self):
        s = str(self)
        return (s in present) if s in managed else orig_path_isfile(self)

    with patch("os.path.exists", side_effect=_exists), \
            patch("os.path.isfile", side_effect=_isfile), \
            patch("os.access", side_effect=_access), \
            patch.object(pathlib.Path, "exists", _path_exists), \
            patch.object(pathlib.Path, "is_file", _path_isfile):
        yield


class ParseProcessTable(unittest.TestCase):
    def test_full_line(self):
        self.assertEqual(
            winproc.parse_process_table("100 134387076752919983 claude"),
            {100: (134387076752919983, "claude")},
        )

    def test_missing_start_time(self):
        self.assertEqual(winproc.parse_process_table("100  claude"), {100: (None, "claude")})

    def test_name_is_lowercased_and_exe_stripped(self):
        self.assertEqual(
            winproc.parse_process_table("7 5 Language_Server.exe"),
            {7: (5, "language_server")},
        )

    def test_garbage_and_empty(self):
        self.assertEqual(winproc.parse_process_table("garbage"), {})
        self.assertEqual(winproc.parse_process_table(""), {})

    def test_several_lines(self):
        out = winproc.parse_process_table("1 10 a\n2 20 B.exe\n")
        self.assertEqual(out, {1: (10, "a"), 2: (20, "b")})

    def test_missing_filetime_keeps_digit_leading_name(self):
        self.assertEqual(winproc.parse_process_table("100  7zFM"), {100: (None, "7zfm")})

    def test_filetime_present_with_digit_leading_name(self):
        self.assertEqual(winproc.parse_process_table("100 12345 7zFM"), {100: (12345, "7zfm")})


class QueryWindowsProcesses(unittest.TestCase):
    def test_empty_pid_list_spawns_nothing(self):
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            self.assertEqual(winproc.query_windows_processes([]), {})
        run.assert_not_called()
        popen.assert_not_called()

    def test_none_when_powershell_unavailable(self):
        with patch("shutil.which", return_value=None), \
                patch("subprocess.run", side_effect=FileNotFoundError("powershell.exe")), \
                patch("subprocess.Popen", side_effect=FileNotFoundError("powershell.exe")):
            self.assertIsNone(winproc.query_windows_processes([100]))

    def test_uses_which_resolved_powershell(self):
        completed = subprocess.CompletedProcess([], 0, stdout="100 134387076752919983 claude\n", stderr="")
        with patch("shutil.which", side_effect=lambda n: "/custom/bin/powershell.exe" if n == "powershell.exe" else None), \
                _patch_paths(set(), {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", return_value=completed) as run:
            out = winproc.query_windows_processes([100])
        self.assertEqual(out, {100: (134387076752919983, "claude")})
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "/custom/bin/powershell.exe")
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_falls_back_to_system32_powershell_when_which_is_none(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "powershell.exe":
                raise FileNotFoundError("powershell.exe")
            return subprocess.CompletedProcess(
                argv, 0, stdout="100 134387076752919983 claude\n200  Language_Server.exe\n", stderr=""
            )

        with patch("shutil.which", return_value=None), \
                _patch_paths({POWERSHELL_FALLBACK}, {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", side_effect=fake_run) as run:
            out = winproc.query_windows_processes([100, 200])
        self.assertEqual(out, {100: (134387076752919983, "claude"), 200: (None, "language_server")})
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], POWERSHELL_FALLBACK)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_returns_none_without_spawning_when_which_and_fallback_missing(self):
        completed = subprocess.CompletedProcess([], 0, stdout="100 500 claude\n", stderr="")
        with patch("shutil.which", return_value=None), \
                _patch_paths(set(), {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", return_value=completed) as run:
            out = winproc.query_windows_processes([100])
        self.assertIsNone(out)
        run.assert_not_called()

    def test_returns_none_on_nonzero_exit_via_fallback(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="failed")
        with patch("shutil.which", return_value=None), \
                _patch_paths({POWERSHELL_FALLBACK}, {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", return_value=completed) as run:
            out = winproc.query_windows_processes([100])
        self.assertIsNone(out)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], POWERSHELL_FALLBACK)

    def test_returns_none_on_subprocess_timeout_via_fallback(self):
        with patch("shutil.which", return_value=None), \
                _patch_paths({POWERSHELL_FALLBACK}, {POWERSHELL_FALLBACK}), \
                patch("subprocess.run", side_effect=subprocess.TimeoutExpired(POWERSHELL_FALLBACK, 15)) as run:
            out = winproc.query_windows_processes([100])
        self.assertIsNone(out)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], POWERSHELL_FALLBACK)


if __name__ == "__main__":
    unittest.main()
