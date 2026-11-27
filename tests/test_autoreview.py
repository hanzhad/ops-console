#!/usr/bin/env python3
"""Tests for bin/autoreview.py: a CLI failure is an error, not an empty registry (host_repo#282)."""

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import autoreview  # noqa: E402

FAIL = (1, "", "Error: No such container: auto-review")


class TestRegistryFailure(unittest.TestCase):
    def test_cli_failure_raises_with_stderr(self):
        with patch.object(autoreview, "cli", return_value=FAIL):
            with self.assertRaises(autoreview.RegistryError) as cm:
                autoreview.get_registry_raw()
        self.assertIn("No such container", str(cm.exception))

    def test_unparseable_output_raises(self):
        with patch.object(autoreview, "cli", return_value=(0, "not json", "")):
            with self.assertRaises(autoreview.RegistryError):
                autoreview.get_registry_raw()

    def test_genuinely_empty_registry_is_not_an_error(self):
        out = json.dumps(autoreview.EMPTY_REGISTRY)
        with patch.object(autoreview, "cli", return_value=(0, out, "")):
            self.assertEqual(autoreview.get_registry_raw()["projects"], {})

    def test_mutators_report_the_failure_not_not_found(self):
        with patch.object(autoreview, "cli", return_value=FAIL):
            for call in (
                lambda: autoreview.forget_repo("o/r"),
                lambda: autoreview.toggle_repo("o/r"),
                lambda: autoreview.set_repo("o/r", "all", "true"),
                lambda: autoreview.trigger_tick("o/r"),
            ):
                with patch.object(autoreview, "get_container_state", return_value={"running": True}):
                    rc, msg = call()
                self.assertEqual(rc, 1)
                self.assertIn("No such container", msg)
                self.assertNotIn("not found", msg)
                self.assertNotIn("not registered", msg)

    def test_empty_registry_still_says_not_found(self):
        out = json.dumps(autoreview.EMPTY_REGISTRY)
        with patch.object(autoreview, "cli", return_value=(0, out, "")):
            rc, msg = autoreview.forget_repo("o/r")
        self.assertEqual((rc, msg), (1, "repo o/r not found"))

    def test_status_surfaces_registry_error(self):
        with patch.object(autoreview, "cli", return_value=FAIL), \
             patch.object(autoreview, "get_container_state", return_value={"running": True}), \
             patch.object(autoreview, "get_container_tokens", return_value=[]), \
             patch.object(autoreview, "get_host_tokens", return_value=[]), \
             patch.object(autoreview, "sh", return_value=(0, "", "")):
            data = autoreview.status()
        self.assertIn("No such container", data["registry_error"])


class TestSetGlobal(unittest.TestCase):
    def test_harness_is_one_cli_call(self):
        with patch.object(autoreview, "cli", return_value=(0, "", "")) as m:
            rc, msg = autoreview.set_global("harness", "agy")
        self.assertEqual(rc, 0)
        m.assert_called_once_with("set-global", "harness", "agy")

    def test_harness_failure_is_reported(self):
        with patch.object(autoreview, "cli", return_value=(1, "", "boom")):
            rc, msg = autoreview.set_global("harness", "claude-code")
        self.assertEqual((rc, msg), (1, "boom"))


def registry(**entry):
    return json.dumps({"version": 1, "projects": {"o/r": entry}})


def fake_cli(reg):
    """A CLI that serves `registry show` and records every other call."""
    calls = []

    def cli(*args, **kw):
        if args[:2] == ("registry", "show"):
            return 0, reg, ""
        calls.append(args)
        return 0, "", ""

    return cli, calls


class TestPauseResume(unittest.TestCase):
    def toggle(self, **entry):
        cli, calls = fake_cli(registry(**entry))
        with patch.object(autoreview, "cli", side_effect=cli):
            rc, msg = autoreview.toggle_repo("o/r")
        self.assertEqual(rc, 0)
        return calls

    def test_active_repo_is_paused(self):
        self.assertEqual(self.toggle(paused=False), [("pause", "o/r")])

    def test_paused_repo_is_resumed(self):
        self.assertEqual(self.toggle(paused=True, paused_reason="rate limit"), [("resume", "o/r")])

    def test_legacy_enabled_false_is_paused(self):
        self.assertTrue(autoreview.is_paused({"enabled": False}))
        self.assertEqual(self.toggle(enabled=False), [("resume", "o/r")])

    def test_legacy_enabled_true_and_bare_entry_are_active(self):
        self.assertFalse(autoreview.is_paused({"enabled": True}))
        self.assertFalse(autoreview.is_paused({}))

    def test_paused_key_wins_over_legacy_enabled(self):
        self.assertFalse(autoreview.is_paused({"paused": False, "enabled": False}))

    def test_status_carries_paused_and_reason(self):
        raw = json.loads(registry(enabled=False, paused_reason="why"))
        with patch.object(autoreview, "get_registry_raw", return_value=raw),              patch.object(autoreview, "get_container_state", return_value={"running": True}),              patch.object(autoreview, "get_container_tokens", return_value=[]),              patch.object(autoreview, "get_host_tokens", return_value=[]),              patch.object(autoreview, "sh", return_value=(0, "", "")):
            data = autoreview.status()
        self.assertTrue(data["projects"]["o/r"]["paused"])
        self.assertEqual(data["projects"]["o/r"]["paused_reason"], "why")


class TestAdoptAndSet(unittest.TestCase):
    def test_adopt_takes_only_the_repo(self):
        with patch.object(autoreview, "cli", return_value=(0, "", "")) as m:
            rc, _ = autoreview.adopt_repo("o/r")
        self.assertEqual(rc, 0)
        m.assert_called_once_with("adopt", "o/r")

    def test_adopt_passes_flags(self):
        with patch.object(autoreview, "cli", return_value=(0, "", "")) as m:
            autoreview.adopt_repo("o/r", all_prs=True, self_review=True)
        m.assert_called_once_with("adopt", "o/r", "--all", "--self-review")

    def test_main_adopt_ignores_legacy_positionals(self):
        with patch.object(autoreview, "cli", return_value=(0, "", "")) as m:
            autoreview.main(["adopt", "o/r", "opus", "60"])
        m.assert_called_once_with("adopt", "o/r")

    def test_machine_wide_keys_are_refused_per_repo(self):
        cli, calls = fake_cli(registry())
        with patch.object(autoreview, "cli", side_effect=cli):
            for key in ("model", "harness", "interval", "slots", "enabled"):
                rc, msg = autoreview.set_repo("o/r", key, "x")
                self.assertEqual(rc, 1)
                self.assertIn("set-global", msg)
        self.assertEqual(calls, [])

    def test_all_and_self_review_are_kept(self):
        cli, calls = fake_cli(registry())
        with patch.object(autoreview, "cli", side_effect=cli):
            self.assertEqual(autoreview.set_repo("o/r", "all", "true")[0], 0)
            self.assertEqual(autoreview.set_repo("o/r", "self_review", "false")[0], 0)
        self.assertEqual(calls, [("set", "o/r", "all", "true"), ("set", "o/r", "self_review", "false")])


MODELS_JSON = json.dumps({"harnesses": [
    {"harness": "agy", "models": ["auto", "flash", "pro", "sonnet", "opus"], "default": "auto"},
    {"harness": "claude-code", "models": ["sonnet", "opus", "haiku"], "default": "sonnet"},
]})


def status_with(models_result):
    raw = json.loads(registry())

    def cli(*args, **kw):
        return models_result if args == ("models",) else (0, "", "")

    with patch.object(autoreview, "cli", side_effect=cli),          patch.object(autoreview, "get_registry_raw", return_value=raw),          patch.object(autoreview, "get_container_state", return_value={"running": True}),          patch.object(autoreview, "get_container_tokens", return_value=[]),          patch.object(autoreview, "get_host_tokens", return_value=[]),          patch.object(autoreview, "sh", return_value=(0, "", "")):
        return autoreview.status()


class TestModelsFromCli(unittest.TestCase):
    def test_parse_models(self):
        hs, ms, ds = autoreview.parse_models(MODELS_JSON)
        self.assertEqual(hs, ["agy", "claude-code"])
        self.assertEqual(ms["agy"], ["auto", "flash", "pro", "sonnet", "opus"])
        self.assertEqual(ds, {"agy": "auto", "claude-code": "sonnet"})

    def test_state_carries_agy_models(self):
        data = status_with((0, MODELS_JSON, ""))
        self.assertEqual(data["available_harnesses"], ["agy", "claude-code"])
        self.assertIn("auto", data["available_models"]["agy"])
        self.assertEqual(data["default_models"]["agy"], "auto")
        self.assertIsNone(data["models_error"])

    def test_no_gemini_cli_anywhere(self):
        self.assertNotIn("gemini-cli", json.dumps(status_with((0, MODELS_JSON, ""))))
        self.assertFalse(hasattr(autoreview, "AVAILABLE_HARNESSES"))
        self.assertFalse(hasattr(autoreview, "AVAILABLE_MODELS"))

    def test_failed_models_call_gives_error_and_empty_lists(self):
        data = status_with((1, "", "Error: No such container: auto-review"))
        self.assertEqual((data["available_harnesses"], data["available_models"], data["default_models"]), ([], {}, {}))
        self.assertIn("No such container", data["models_error"])

    def test_unparseable_models_output_is_an_error(self):
        data = status_with((0, "not json", ""))
        self.assertEqual(data["available_harnesses"], [])
        self.assertIn("unparseable", data["models_error"])

    def test_set_global_harness_sends_exactly_the_cli_call(self):
        with patch.object(autoreview, "cli", return_value=(0, "", "")) as m:
            autoreview.set_global("harness", "agy")
        m.assert_called_once_with("set-global", "harness", "agy")


class TestPageSource(unittest.TestCase):
    """page.html has no browser test; these pin the source of the selects."""

    root = pathlib.Path(__file__).resolve().parents[1]
    page = "\n".join(f.read_text(encoding="utf-8") for f in [root / "page.html", *sorted((root / "static").glob("*.js"))])

    def test_no_hard_coded_lists(self):
        self.assertNotIn("gemini", self.page)
        self.assertNotIn("'sonnet'", self.page)

    def test_mismatch_is_labelled_and_selected(self):
        self.assertIn("(not offered by ", self.page)
        self.assertIn("unshift(opt(ar.global_model", self.page)

    def test_selects_are_disabled_while_saving(self):
        # set-global is an ordinary command now: pending on ar.global.<key>, selects locked until its terminal event
        self.assertIn("'ar.global.' + key", self.page)
        self.assertIn("hSelect.disabled = mSelect.disabled", self.page)
        self.assertNotIn("await refresh()", self.page)


class TestHealthStatus(unittest.TestCase):
    def test_get_health_status_parses_tokens_and_faults(self):
        payload = json.dumps({
            "tokens": [{"name": "gh-hanzhad.token", "kind": "github", "problem": "short"}],
            "harnesses": {"claude-code": {"state": "fault", "message": "401 unauthorized"}},
        })
        with patch.object(autoreview, "cli", return_value=(0, "WARNING: banner\n" + payload, "")):
            h = autoreview.get_health_status()
        self.assertIsNone(h["error"])
        self.assertEqual(h["tokens"][0]["problem"], "short")
        self.assertEqual(h["harnesses"]["claude-code"]["state"], "fault")

    def test_get_health_status_reports_unparseable_and_non_dict(self):
        for bad in ((0, "not json", ""), (0, "[]", "")):
            with patch.object(autoreview, "cli", return_value=bad):
                h = autoreview.get_health_status()
            self.assertIn("unparseable", h["error"])

    def test_status_surfaces_health_cli_error(self):
        reg = json.dumps(autoreview.EMPTY_REGISTRY)
        models = json.dumps({"harnesses": [{"harness": "claude-code", "models": ["sonnet"], "default": "sonnet"}]})

        def fake(*args, **kw):
            if args[:2] == ("registry", "show"):
                return 0, reg, ""
            if args == ("models",):
                return 0, models, ""
            return 1, "", "status unavailable"

        with patch.object(autoreview, "cli", side_effect=fake), \
             patch.object(autoreview, "get_container_state", return_value={"running": True}), \
             patch.object(autoreview, "get_container_tokens", return_value=[]), \
             patch.object(autoreview, "get_host_tokens", return_value=[]), \
             patch.object(autoreview, "sh", return_value=(0, "", "")):
            st = autoreview.status()
        self.assertEqual(st["health"], {"tokens": [], "harnesses": {}, "error": "status unavailable"})


class _TickRig:
    """In-tempdir container shell rig for testing bin/autoreview.py:trigger_tick."""

    def __init__(self, case):
        self.root = pathlib.Path(tempfile.mkdtemp())
        case.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.clones = self.root / "clones"
        self.clones.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.ar_log = self.root / "ar_calls.jsonl"
        self.git_log = self.root / "git_calls.jsonl"
        self.sh_cmds = []

        self.ar_bin = self.bin_dir / "auto-review"
        self.ar_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"with open({str(self.ar_log)!r}, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('tick completed')\n",
            encoding="utf-8",
        )
        self.ar_bin.chmod(0o755)

        self.git_bin = self.bin_dir / "git"
        self.git_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"with open({str(self.git_log)!r}, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('fatal: unauthenticated git clone disallowed', file=sys.stderr)\n"
            "sys.exit(128)\n",
            encoding="utf-8",
        )
        self.git_bin.chmod(0o755)

    def make_clone(self, repo, with_git=True):
        slug = repo.replace("/", "-")
        clone_dir = self.clones / slug
        clone_dir.mkdir(parents=True, exist_ok=True)
        if with_git:
            (clone_dir / ".git").mkdir(exist_ok=True)
        return clone_dir

    def ar_calls(self):
        if not self.ar_log.exists():
            return []
        prefix = str(self.clones)
        return [
            [a.replace(prefix, "/var/lib/auto-review/clones") for a in json.loads(line)]
            for line in self.ar_log.read_text(encoding="utf-8").splitlines()
        ]

    def git_calls(self):
        if not self.git_log.exists():
            return []
        return [json.loads(line) for line in self.git_log.read_text(encoding="utf-8").splitlines()]

    def reset_logs(self):
        for p in (self.ar_log, self.git_log, self.root / "pwned"):
            if p.exists():
                p.unlink()
        self.sh_cmds.clear()

    def _map_text(self, text):
        return (
            text.replace("/var/lib/auto-review/clones", str(self.clones))
            .replace("/usr/local/bin/auto-review", str(self.ar_bin))
        )

    def fake_sh(self, cmd, timeout=30):
        self.sh_cmds.append(list(cmd))
        env = {**os.environ, "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}
        inner = list(cmd)
        if inner[:2] == ["docker", "exec"]:
            i = 2
            while i < len(inner) and inner[i].startswith("-"):
                i += 2 if inner[i] in ("-u", "--user", "-e", "--env", "-w", "--workdir") else 1
            if i < len(inner) and inner[i] == "auto-review":
                inner = inner[i + 1:]
        if inner[:2] == ["sh", "-c"] and len(inner) >= 3:
            mapped = self._map_text(inner[2])
            p = subprocess.run(
                ["sh", "-c", mapped, *inner[3:]],
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        if inner[:2] == ["test", "-d"] and len(inner) == 3:
            return (0, "", "") if os.path.isdir(self._map_text(inner[2])) else (1, "", "")
        if inner[:2] == ["[", "-d"] and len(inner) == 4 and inner[3] == "]":
            return (0, "", "") if os.path.isdir(self._map_text(inner[2])) else (1, "", "")
        mapped_cmd = [self._map_text(str(a)) for a in inner]
        p = subprocess.run(
            mapped_cmd,
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def run_tick(self, repo, raw, cli_args=None):
        reg_json = json.dumps(raw)

        def fake_cli(*args, **kw):
            if args[:2] == ("registry", "show"):
                return 0, reg_json, ""
            if args and args[0] == "tick":
                return self.fake_sh(
                    ["docker", "exec", "-u", autoreview.GATE_UID, "auto-review", "/usr/local/bin/auto-review", *args],
                    timeout=kw.get("timeout", 300),
                )
            raise AssertionError(f"unexpected cli call: {args}")

        with patch.object(autoreview, "get_container_state", return_value={"running": True}), \
             patch.object(autoreview, "cli", side_effect=fake_cli), \
             patch.object(autoreview, "sh", side_effect=self.fake_sh):
            if cli_args is None:
                return autoreview.trigger_tick(repo)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = autoreview.main(cli_args)
            return rc, buf.getvalue().strip()


class TestTriggerTick(unittest.TestCase):
    """Issue #41: trigger_tick forwards --all/--self-review, quotes shell args, and rejects missing clones."""

    def _flag_value(self, flags, flag):
        self.assertIn(flag, flags, f"missing {flag} in {flags}")
        idx = flags.index(flag)
        self.assertLess(idx + 1, len(flags), f"{flag} missing value in {flags}")
        return flags[idx + 1]

    def test_forwards_all_and_self_review_flags_across_registry_entries(self):
        rig = _TickRig(self)
        cases = [
            ("o/r", {"o/r": {}}, False, False),
            ("o/r", {"o/r": {"all": False, "self_review": False}}, False, False),
            ("o/r", {"o/r": {"all": True, "self_review": False}}, True, False),
            ("o/r", {"o/r": {"all": False, "self_review": True}}, False, True),
            ("o/r", {"o/r": {"all": True, "self_review": True}}, True, True),
            ("o/r", {"o/r": {"all": 1, "self_review": 0}}, True, False),
            ("o/r", {"o/r": {"all": None, "self_review": ""}}, False, False),
            ("o/other", {"o/r": {"all": True, "self_review": True}}, False, False),
        ]
        for repo, projects, want_all, want_sr in cases:
            with self.subTest(repo=repo, projects=projects):
                rig.reset_logs()
                rig.make_clone(repo, with_git=True)
                raw = {"version": 1, "harness": "agy", "model": "pro", "projects": projects}
                rc, msg = rig.run_tick(repo, raw)
                self.assertEqual(rc, 0, msg)
                self.assertEqual(rig.git_calls(), [])
                calls = rig.ar_calls()
                self.assertEqual(len(calls), 1)
                argv = calls[0]
                self.assertEqual(argv[0], "tick")
                self.assertEqual(argv[1], f"/var/lib/auto-review/clones/{repo.replace('/', '-')}")
                flags = argv[2:]
                self.assertEqual(self._flag_value(flags, "--model"), "pro")
                self.assertEqual(self._flag_value(flags, "--harness"), "agy")
                self.assertEqual("--all" in flags, want_all, flags)
                self.assertEqual("--self-review" in flags, want_sr, flags)

    def test_shell_arguments_escaped_with_shlex_quote_no_injection(self):
        rig = _TickRig(self)
        pwned = rig.root / "pwned"
        cases = [
            ("o/r", "sonnet'; touch pwned; echo '", "claude-code"),
            ("o/r", "sonnet", "agy'; touch pwned; echo '"),
            ("o/r", "$(touch pwned)", "`touch pwned`"),
            ("o/r", "m'o\"d  * #1", "h'a\"r  ? #2"),
            ("o/r'; touch pwned; #", "sonnet", "claude-code"),
            ("o/r $(touch pwned)", "auto", "agy"),
        ]
        for repo, model, harness in cases:
            with self.subTest(repo=repo, model=model, harness=harness):
                rig.reset_logs()
                rig.make_clone(repo, with_git=True)
                raw = {
                    "version": 1,
                    "harness": harness,
                    "model": model,
                    "projects": {repo: {}},
                }
                rc, msg = rig.run_tick(repo, raw)
                self.assertFalse(pwned.exists(), f"shell injection executed for {(repo, model, harness)}")
                self.assertEqual(rc, 0, msg)
                self.assertEqual(rig.git_calls(), [])
                calls = rig.ar_calls()
                self.assertEqual(len(calls), 1)
                argv = calls[0]
                self.assertEqual(argv[0], "tick")
                self.assertEqual(argv[1], f"/var/lib/auto-review/clones/{repo.replace('/', '-')}")
                flags = argv[2:]
                self.assertEqual(self._flag_value(flags, "--model"), model)
                self.assertEqual(self._flag_value(flags, "--harness"), harness)

    def test_missing_clone_git_dir_returns_nonzero_with_setup_instruction(self):
        rig = _TickRig(self)
        pwned = rig.root / "pwned"

        # Case 1: clone directory does not exist at all
        rc, msg = rig.run_tick(
            "hanzhad/ops-console",
            {"version": 1, "harness": "agy", "model": "auto", "projects": {"hanzhad/ops-console": {"all": True}}},
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("auto-review setup hanzhad/ops-console", msg)
        self.assertEqual(rig.git_calls(), [])
        self.assertEqual(rig.ar_calls(), [])

        # Case 2: clone directory exists without .git
        rig.reset_logs()
        rig.make_clone("hanzhad/ops-console", with_git=False)
        rc, msg = rig.run_tick(
            "hanzhad/ops-console",
            {"version": 1, "harness": "agy", "model": "auto", "projects": {"hanzhad/ops-console": {"all": True}}},
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("auto-review setup hanzhad/ops-console", msg)
        self.assertEqual(rig.git_calls(), [])
        self.assertEqual(rig.ar_calls(), [])

        # Case 3: .git is a regular file, not a directory
        rig.reset_logs()
        clone_dir = rig.make_clone("o/file-git", with_git=False)
        (clone_dir / ".git").write_text("not a dir", encoding="utf-8")
        rc, msg = rig.run_tick(
            "o/file-git",
            {"version": 1, "harness": "claude-code", "model": "sonnet", "projects": {"o/file-git": {}}},
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("auto-review setup o/file-git", msg)
        self.assertEqual(rig.git_calls(), [])
        self.assertEqual(rig.ar_calls(), [])

        # Case 4: missing clone with shell metacharacters in repo must not inject
        rig.reset_logs()
        bad_repo = "o/r'; touch pwned; #"
        rc, msg = rig.run_tick(
            bad_repo,
            {"version": 1, "harness": "claude-code", "model": "sonnet", "projects": {bad_repo: {}}},
        )
        self.assertFalse(pwned.exists(), "shell injection executed on missing clone error path")
        self.assertNotEqual(rc, 0)
        self.assertIn("auto-review setup", msg)
        self.assertEqual(rig.git_calls(), [])
        self.assertEqual(rig.ar_calls(), [])

    def test_main_tick_forwards_missing_clone_failure(self):
        rig = _TickRig(self)
        rc, printed = rig.run_tick(
            "hanzhad/ops-console",
            {"version": 1, "harness": "agy", "model": "auto", "projects": {"hanzhad/ops-console": {}}},
            cli_args=["tick", "hanzhad/ops-console"],
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("auto-review setup hanzhad/ops-console", printed)
        self.assertEqual(rig.git_calls(), [])
        self.assertEqual(rig.ar_calls(), [])


if __name__ == "__main__":
    unittest.main()
