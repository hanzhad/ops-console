#!/usr/bin/env python3
"""Tests for bin/agy_send.py (ops-console#40): Antigravity agentapi delivery."""

import contextlib
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SW_ROOT = pathlib.Path(__file__).resolve().parent
BIN_DIR = SW_ROOT / "bin"
for p in (str(SW_ROOT), str(BIN_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agy_send  # noqa: E402

DEFAULT_LS = "/mnt/c/Users/user/AppData/Local/Programs/antigravity/resources/bin/language_server.exe"
DEFAULT_BAT = "/mnt/c/Users/user/.gemini/antigravity/bin/agentapi.bat"
MANAGED_PATHS = {DEFAULT_LS, DEFAULT_BAT}


@contextlib.contextmanager
def _patch_paths(present, managed=MANAGED_PATHS):
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


@contextlib.contextmanager
def _clean_env(**overrides):
    env = dict(os.environ)
    env.pop("AGY_AGENTAPI_BIN", None)
    env.update(overrides)
    with patch.dict(os.environ, env, clear=True):
        yield


def _completed(argv=None, returncode=0, stdout="ok\n", stderr=""):
    return subprocess.CompletedProcess(argv or [], returncode, stdout=stdout, stderr=stderr)


class AgySendConversationId(unittest.TestCase):
    def test_strips_leading_local_prefix_from_session_ref(self):
        sess = {"id": "cid-1", "ref": "local_cid-1", "raw_ref": "cid-1"}
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", return_value=_completed()) as run:
            ok, detail, reply = agy_send.send(sess, "local_cid-1", "hello")
        self.assertIs(ok, True)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertIsNone(reply)
        run.assert_called_once()
        argv = run.call_args.args[0]
        kw = run.call_args.kwargs
        self.assertEqual(argv, [DEFAULT_LS, "agentapi", "send-message", "cid-1", "hello"])
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw.get("timeout"), 30)

    def test_bare_session_ref_kept_intact(self):
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", return_value=_completed()) as run:
            ok, _, reply = agy_send.send({}, "e2dc9999-aaaa", "hello")
        self.assertIs(ok, True)
        self.assertIsNone(reply)
        self.assertEqual(
            run.call_args.args[0],
            [DEFAULT_LS, "agentapi", "send-message", "e2dc9999-aaaa", "hello"],
        )

    def test_non_leading_local_substring_preserved(self):
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", return_value=_completed()) as run:
            ok, _, _ = agy_send.send({}, "conv_local_123", "hello")
        self.assertIs(ok, True)
        self.assertEqual(
            run.call_args.args[0],
            [DEFAULT_LS, "agentapi", "send-message", "conv_local_123", "hello"],
        )

    def test_falls_back_to_sess_raw_ref_then_id_when_session_ref_empty(self):
        cases = [
            ({"raw_ref": "local_raw-42", "id": "id-99"}, "", "raw-42"),
            ({"id": "local_id-99"}, "", "id-99"),
            ({"raw_ref": "", "id": "id-77"}, "", "id-77"),
            ({"raw_ref": "raw-55"}, None, "raw-55"),
        ]
        for sess, ref, expected_cid in cases:
            with self.subTest(sess=sess, ref=ref):
                with _clean_env(), _patch_paths({DEFAULT_LS}), \
                        patch("subprocess.run", return_value=_completed()) as run:
                    ok, _, reply = agy_send.send(sess, ref, "hello")
                self.assertIs(ok, True)
                self.assertIsNone(reply)
                self.assertEqual(run.call_args.args[0][3], expected_cid)

    def test_rejects_empty_cid_without_spawning(self):
        bad_targets = [
            ({}, ""),
            (None, ""),
            ({}, "local_"),
            ({"raw_ref": "", "id": ""}, ""),
            ({"id": "local_"}, ""),
        ]
        for sess, ref in bad_targets:
            with self.subTest(sess=sess, ref=ref):
                with _clean_env(), _patch_paths({DEFAULT_LS}), \
                        patch("subprocess.run") as run:
                    ok, detail, reply = agy_send.send(sess, ref, "hello")
                run.assert_not_called()
                self.assertIs(ok, False)
                self.assertIsInstance(detail, str)
                self.assertTrue(detail.strip())
                self.assertNotIn("not implemented", detail.lower())
                self.assertIsNone(reply)

    def test_rejects_empty_letter_without_spawning(self):
        for bad_letter in ("", None):
            with self.subTest(letter=bad_letter):
                with _clean_env(), _patch_paths({DEFAULT_LS}), \
                        patch("subprocess.run") as run:
                    ok, detail, reply = agy_send.send({"id": "cid-1"}, "local_cid-1", bad_letter)
                run.assert_not_called()
                self.assertIs(ok, False)
                self.assertIsInstance(detail, str)
                self.assertTrue(detail.strip())
                self.assertNotIn("not implemented", detail.lower())
                self.assertIsNone(reply)

    def test_preserves_multiline_and_shell_metacharacters_in_letter(self):
        letter = '[xmsg] from: claude:c1   reply: yes   hops: 1\n\nhello "hi" & %PATH% ; | `id`'
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", return_value=_completed()) as run:
            ok, _, _ = agy_send.send({}, "local_cid-1", letter)
        self.assertIs(ok, True)
        self.assertEqual(run.call_args.args[0], [DEFAULT_LS, "agentapi", "send-message", "cid-1", letter])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 30)


class AgySendBatParsing(unittest.TestCase):
    def test_win_to_wsl_path(self):
        cases = [
            (r"C:\Users\user\AppData\Local\Programs\antigravity\resources\bin\language_server.exe",
             "/mnt/c/Users/user/AppData/Local/Programs/antigravity/resources/bin/language_server.exe"),
            (r"d:\Program Files (x86)\Antigravity\language_server.EXE",
             "/mnt/d/Program Files (x86)/Antigravity/language_server.EXE"),
            ("C:/Tools/antigravity/language_server.exe",
             "/mnt/c/Tools/antigravity/language_server.exe"),
        ]
        for win_path, expected in cases:
            with self.subTest(win_path=win_path):
                self.assertEqual(agy_send.win_to_wsl_path(win_path), expected)

    def test_parse_agentapi_bat_table(self):
        cases = [
            ("standard_bat",
             '@echo off\n"C:\\Users\\user\\AppData\\Local\\Programs\\antigravity\\resources\\bin\\language_server.exe" agentapi %*',
             "/mnt/c/Users/user/AppData/Local/Programs/antigravity/resources/bin/language_server.exe"),
            ("crlf_spaces_uppercase_exe",
             '@echo off\r\n"D:\\Program Files (x86)\\Antigravity\\language_server.EXE" agentapi %*\r\n',
             "/mnt/d/Program Files (x86)/Antigravity/language_server.EXE"),
            ("at_prefixed_invocation",
             '@"E:\\Apps\\Antigravity\\language_server.exe" agentapi %*',
             "/mnt/e/Apps/Antigravity/language_server.exe"),
            ("skips_comments_and_cmd_exe",
             '@echo off\nREM "C:\\old\\language_server.exe"\n@rem "C:\\older\\language_server.exe"\n:: "C:\\oldest\\language_server.exe"\n"C:\\Windows\\System32\\cmd.exe"\n"D:\\Real\\language_server.exe" agentapi %*\n',
             "/mnt/d/Real/language_server.exe"),
            ("only_cmd_exe_rejected",
             '@echo off\n"C:\\Windows\\System32\\cmd.exe" /d /c echo hi\n',
             None),
            ("no_quoted_exe",
             "@echo off\necho no executable here\n",
             None),
            ("empty", "", None),
        ]
        for name, text, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(agy_send.parse_agentapi_bat(text), expected)


class AgySendBinaryResolution(unittest.TestCase):
    def test_uses_AGY_AGENTAPI_BIN_when_set_and_exists(self):
        with tempfile.TemporaryDirectory() as td:
            custom_bin = pathlib.Path(td) / "language_server.exe"
            custom_bin.write_text("stub", encoding="utf-8")
            custom_bin.chmod(0o755)
            with _clean_env(AGY_AGENTAPI_BIN=str(custom_bin)), _patch_paths(set()), \
                    patch("subprocess.run", return_value=_completed()) as run:
                ok, detail, reply = agy_send.send({}, "local_cid-1", "hi")
        self.assertIs(ok, True)
        self.assertTrue(detail.strip())
        self.assertIsNone(reply)
        self.assertEqual(
            run.call_args.args[0],
            [str(custom_bin), "agentapi", "send-message", "cid-1", "hi"],
        )
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 30)

    def test_AGY_AGENTAPI_BIN_takes_precedence_over_default_binary(self):
        with tempfile.TemporaryDirectory() as td:
            custom_bin = pathlib.Path(td) / "language_server.exe"
            custom_bin.write_text("stub", encoding="utf-8")
            custom_bin.chmod(0o755)
            with _clean_env(AGY_AGENTAPI_BIN=str(custom_bin)), _patch_paths({DEFAULT_LS}), \
                    patch("subprocess.run", return_value=_completed()) as run:
                ok, _, _ = agy_send.send({}, "cid-1", "hi")
        self.assertIs(ok, True)
        self.assertEqual(run.call_args.args[0][0], str(custom_bin))

    def test_falls_back_to_default_binary_when_AGY_AGENTAPI_BIN_missing(self):
        with tempfile.TemporaryDirectory() as td:
            missing_bin = str(pathlib.Path(td) / "missing_language_server.exe")
            with _clean_env(AGY_AGENTAPI_BIN=missing_bin), _patch_paths({DEFAULT_LS}), \
                    patch("subprocess.run", return_value=_completed()) as run:
                ok, _, _ = agy_send.send({}, "cid-1", "hi")
        self.assertIs(ok, True)
        self.assertEqual(run.call_args.args[0][0], DEFAULT_LS)

    def test_falls_back_to_parsed_exe_from_agentapi_bat_when_default_bin_absent(self):
        parsed_exe = "/mnt/d/Program Files/Antigravity/resources/bin/language_server.exe"
        bat_content = '@echo off\r\n"D:\\Program Files\\Antigravity\\resources\\bin\\language_server.exe" agentapi %*\r\n'
        with tempfile.TemporaryDirectory() as td:
            bat_file = pathlib.Path(td) / "agentapi.bat"
            bat_file.write_text(bat_content, encoding="utf-8")
            with _clean_env(), \
                    patch.object(agy_send, "DEFAULT_AGENTAPI_BAT", str(bat_file)), \
                    _patch_paths({parsed_exe}, MANAGED_PATHS | {parsed_exe}), \
                    patch("shutil.which") as which_mock, \
                    patch("subprocess.run", return_value=_completed()) as run:
                ok, detail, reply = agy_send.send({}, "local_cid-2", "msg", wait=0)
        self.assertIs(ok, True)
        self.assertTrue(detail.strip())
        self.assertIsNone(reply)
        which_mock.assert_not_called()
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv, [parsed_exe, "agentapi", "send-message", "cid-2", "msg"])
        self.assertNotIn("cmd.exe", argv[0].lower())
        self.assertFalse(argv[0].lower().endswith(".bat"))
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 30)

    def test_bat_fallback_passes_shell_metacharacters_literally_without_cmd_exe(self):
        parsed_exe = "/mnt/d/Antigravity/bin/language_server.exe"
        bat_content = '@echo off\n"D:\\Antigravity\\bin\\language_server.exe" agentapi %*\n'
        letter = 'say "hi" & calc.exe %PATH%\nline 2; | `id`'
        with tempfile.TemporaryDirectory() as td:
            bat_file = pathlib.Path(td) / "agentapi.bat"
            bat_file.write_text(bat_content, encoding="utf-8")
            with _clean_env(), \
                    patch.object(agy_send, "DEFAULT_AGENTAPI_BAT", str(bat_file)), \
                    _patch_paths({parsed_exe}, MANAGED_PATHS | {parsed_exe}), \
                    patch("subprocess.run", return_value=_completed()) as run:
                ok, _, reply = agy_send.send({}, "local_cid-2", letter)
        self.assertIs(ok, True)
        self.assertIsNone(reply)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv, [parsed_exe, "agentapi", "send-message", "cid-2", letter])
        self.assertNotIn("cmd.exe", " ".join(argv[:-1]).lower())
        self.assertFalse(argv[0].lower().endswith(".bat"))
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 30)

    def test_errors_without_spawning_when_all_binaries_absent(self):
        with _clean_env(), _patch_paths(set()), \
                patch("subprocess.run") as run:
            ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_not_called()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)

    def test_errors_without_spawning_when_bat_has_no_parseable_exe(self):
        with tempfile.TemporaryDirectory() as td:
            bat_file = pathlib.Path(td) / "agentapi.bat"
            bat_file.write_text("@echo off\necho broken\n", encoding="utf-8")
            with _clean_env(), \
                    patch.object(agy_send, "DEFAULT_AGENTAPI_BAT", str(bat_file)), \
                    _patch_paths(set()), \
                    patch("subprocess.run") as run:
                ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_not_called()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)

    def test_errors_without_spawning_when_bat_parsed_exe_missing_on_disk(self):
        missing_exe = "/mnt/d/Missing/language_server.exe"
        with tempfile.TemporaryDirectory() as td:
            bat_file = pathlib.Path(td) / "agentapi.bat"
            bat_file.write_text('@echo off\n"D:\\Missing\\language_server.exe" agentapi %*\n', encoding="utf-8")
            with _clean_env(), \
                    patch.object(agy_send, "DEFAULT_AGENTAPI_BAT", str(bat_file)), \
                    _patch_paths(set(), MANAGED_PATHS | {missing_exe}), \
                    patch("subprocess.run") as run:
                ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_not_called()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)

    def test_errors_without_spawning_when_bat_unreadable(self):
        with _clean_env(), _patch_paths({DEFAULT_BAT}), \
                patch.object(pathlib.Path, "read_text", side_effect=OSError("permission denied")), \
                patch("subprocess.run") as run:
            ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_not_called()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)

    def test_errors_without_spawning_when_AGY_AGENTAPI_BIN_missing_and_no_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            missing_bin = str(pathlib.Path(td) / "missing_language_server.exe")
            with _clean_env(AGY_AGENTAPI_BIN=missing_bin), _patch_paths(set()), \
                    patch("subprocess.run") as run:
                ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_not_called()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)


class AgySendExecutionOutcomes(unittest.TestCase):
    def test_success_returns_nonempty_detail_and_none_reply_even_with_empty_stdout(self):
        for stdout in ("", "delivered\n"):
            with self.subTest(stdout=stdout):
                with _clean_env(), _patch_paths({DEFAULT_LS}), \
                        patch("subprocess.run", return_value=_completed(returncode=0, stdout=stdout)):
                    ok, detail, reply = agy_send.send({}, "cid-1", "hi")
                self.assertIs(ok, True)
                self.assertIsInstance(detail, str)
                self.assertTrue(detail.strip())
                self.assertIsNone(reply)

    def test_nonzero_exit_returns_false_with_error_detail(self):
        for rc, stdout, stderr in ((1, "", "conversation not found"), (2, "failed", ""), (1, "", "")):
            with self.subTest(rc=rc, stdout=stdout, stderr=stderr):
                with _clean_env(), _patch_paths({DEFAULT_LS}), \
                        patch("subprocess.run", return_value=_completed(returncode=rc, stdout=stdout, stderr=stderr)) as run:
                    ok, detail, reply = agy_send.send({}, "cid-1", "hi")
                run.assert_called_once()
                self.assertIs(ok, False)
                self.assertIsInstance(detail, str)
                self.assertTrue(detail.strip())
                self.assertNotIn("not implemented", detail.lower())
                self.assertIsNone(reply)

    def test_timeout_expired_returns_false_without_raising(self):
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=DEFAULT_LS, timeout=30)) as run:
            ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_called_once()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)

    def test_subprocess_oserror_returns_false_without_raising(self):
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", side_effect=OSError("exec format error")) as run:
            ok, detail, reply = agy_send.send({}, "cid-1", "hi")
        run.assert_called_once()
        self.assertIs(ok, False)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail.strip())
        self.assertNotIn("not implemented", detail.lower())
        self.assertIsNone(reply)

    def test_accepts_wait_positional_and_keyword(self):
        with _clean_env(), _patch_paths({DEFAULT_LS}), \
                patch("subprocess.run", return_value=_completed()) as run:
            ok1, _, r1 = agy_send.send({}, "local_cid-1", "hi", 5)
            ok2, _, r2 = agy_send.send({}, "local_cid-1", "hi", wait=10)
        self.assertIs(ok1, True)
        self.assertIs(ok2, True)
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), 30)


if __name__ == "__main__":
    unittest.main()
