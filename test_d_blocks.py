#!/usr/bin/env python3
"""Tests for issue #9 part D: the remaining blocks (host, slots, sessions, runners, cleaners).

Layers: pure (domain registration, RouteSpec zones and deadlines, the pure builders and parsers, the
command lifecycle with sw.core). Nothing here starts a thread, a server or a subprocess.

Assumed names (the implementer must match):
  sw.domains.host:     BLOCKS = [BlockSpec "host", 5 s, after=()]; data has key "stats" (docker stats sample)
  sw.domains.slots:    BLOCKS = [BlockSpec "slots", 10 s, after includes "host" and "sessions"]
  sw.domains.sessions: BLOCKS = [BlockSpec "sessions", 10 s]
  sw.domains.runners:  BLOCKS = [BlockSpec "runners", 10 s, after includes "host"]
  sw.domains.cleaners: BLOCKS = [BlockSpec "cleaners", 30 s]; data {"cleaners": {...}, "rescue_bundles": [...]}
  sw.commands.RouteSpec gains `wakes: Callable[[args], tuple] = (lambda args: ())` after `precheck`: blocks that are
      woken when the action finishes but do not gate `ok` (zones gate, wakes only wake)
  sw.core.parse_docker_stats(out) -> {name: [cpu, mem_mb]};  sw.core.parse_host(meminfo, loadavg, cpus, stats) -> dict
  sw.core.slots_block(slots, limits) -> {"slots", "machine_max"};  sw.core.sessions_block(sessions, limits=None) -> {"sessions"}
  bin/slots.py: build_slot(lease, sess, stats, branch, head, now=) -> dict; discover_slots(stats, sessions) -> list
  bin/runners.py: parse_ps(out), build_runner(name, klass, gate_slug, stat, worker, job), sort_runners(list),
      busy_count(list), collect(stats=None) -> list, main(argv=None) prints a JSON array
"""

import importlib
import inspect
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent
BIN = ROOT / "bin"


def load(case, name, criterion):
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    try:
        return importlib.import_module(name)
    except ImportError as e:
        case.fail("%s: cannot import %s (%s)" % (criterion, name, e))


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def domain(case, name):
    return load(case, "sw.domains." + name, "part D: %s domain" % name)


def block(case, name):
    blocks = domain(case, name).BLOCKS
    case.assertEqual([b.name for b in blocks], [name], "part D: %s domain must register exactly its block" % name)
    return blocks[0]


def route(case, dom, path):
    routes = domain(case, dom).ROUTES
    case.assertIn(path, routes)
    return routes[path]


def wakes(case, r, args):
    case.assertTrue(hasattr(r, "wakes"), "part D: RouteSpec needs a `wakes(args) -> tuple` field (woken, not gating ok)")
    return tuple(r.wakes(args))


class TestBlockRegistration(unittest.TestCase):
    def test_host_5s(self):
        b = block(self, "host")
        self.assertEqual(b.period, 5)
        self.assertTrue(callable(b.read))
        self.assertEqual(tuple(b.after), ())

    def test_slots_10s_woken_by_host_and_sessions(self):
        # slots reads host.stats and the sessions block (bin/slots.py), so a rev change of either must wake it
        b = block(self, "slots")
        self.assertEqual(b.period, 10)
        self.assertTrue(callable(b.read))
        self.assertTrue({"host", "sessions"} <= set(b.after), b.after)

    def test_sessions_10s(self):
        b = block(self, "sessions")
        self.assertEqual(b.period, 10)
        self.assertTrue(callable(b.read))

    def test_runners_10s_woken_by_host(self):
        b = block(self, "runners")
        self.assertEqual(b.period, 10)
        self.assertTrue(callable(b.read))
        self.assertIn("host", b.after)

    def test_cleaners_30s(self):
        b = block(self, "cleaners")
        self.assertEqual(b.period, 30)
        self.assertTrue(callable(b.read))

    def test_discover_finds_exactly_the_eight_blocks_and_no_legacy(self):
        domains = load(self, "sw.domains", "discover")
        names = sorted(b.name for b in domains.discover()[0])
        self.assertEqual(names, sorted(["ci_gate", "gh_queue", "autoreview", "host", "slots", "sessions",
                                        "runners", "cleaners"]))

    def test_cleaners_reader_holds_cleaners_and_rescue_bundles(self):
        b = block(self, "cleaners")
        engine = load(self, "engine", "bin/engine.py")
        with patch.object(engine, "get_cleaners_status", return_value={"fast": {"ok": True}}), \
                patch.object(engine, "get_rescue_archives", return_value=[{"id": "p/l"}]):
            out = b.read(None)
        self.assertEqual(out, {"cleaners": {"fast": {"ok": True}}, "rescue_bundles": [{"id": "p/l"}]})

    def test_slots_and_runners_readers_take_the_docker_sample_from_the_host_block(self):
        for dom in ("slots", "runners"):
            src = text("sw/domains/%s.py" % dom)
            self.assertRegex(src, r"block_data\(\s*[\"']host[\"']\s*\)", "part D: %s must read host.stats" % dom)

    def test_slots_reader_takes_sessions_from_the_sessions_block(self):
        self.assertRegex(text("sw/domains/slots.py"), r"block_data\(\s*[\"']sessions[\"']\s*\)")

    def test_gh_queue_reader_takes_queue_from_ci_gate_block_with_zero_api_calls(self):
        b = next(blk for blk in domain(self, "ci").BLOCKS if blk.name == "gh_queue")
        ci_mod = load(self, "ci", "bin/ci.py")
        queue_items = [{"repo": "hanzhad/host_repo", "id": 55, "created": "2026-11-26T00:00:00Z", "name": "CI", "branch": "main", "class": "big"}]
        store = FakeStore({"ci_gate": {"running": True, "queue": queue_items}})
        with patch.object(ci_mod, "queue", side_effect=AssertionError("read_gh_queue must not call bin/ci.py queue()")), \
                patch.object(ci_mod.subprocess, "run", side_effect=AssertionError("read_gh_queue must not invoke subprocess")):
            self.assertEqual(b.read(store), queue_items)
            self.assertEqual(b.read(FakeStore({"ci_gate": {"running": True}})), [])
            self.assertEqual(b.read(FakeStore({})), [])


ROUTE_TABLE = [
    # (domain, path, sample args, zones that gate ok, zones also woken, deadline)
    ("slots", "/api/cap", (1,), ("slots",), (), 30),
    ("sessions", "/api/limit", ("s1", 1), ("sessions",), (), 30),
    ("cleaners", "/api/cleaners", ("fast",), ("cleaners",), ("slots",), 960),
    ("cleaners", "/api/rescue", ("restore", "p/l"), ("cleaners",), ("slots",), 960),
    ("slots", "/api/evict", ("s-slot-1", "release"), ("slots",), ("sessions", "cleaners"), 960),
    ("messaging", "/api/send", (["r"], "hi"), (), (), 30),
]


class TestRouteZones(unittest.TestCase):
    def test_slots_domain_has_no_project_limit_route_and_sessions_has_limit_route(self):
        self.assertNotIn("/api/project-limit", domain(self, "slots").ROUTES)
        self.assertIn("/api/limit", domain(self, "sessions").ROUTES)

    def test_gating_zones(self):
        for dom, path, args, zones, _w, _d in ROUTE_TABLE:
            self.assertEqual(tuple(route(self, dom, path).zones(args)), zones, path)

    def test_no_route_gates_on_legacy(self):
        for dom, path, args, _z, _w, _d in ROUTE_TABLE:
            self.assertNotIn("legacy", route(self, dom, path).zones(args), path)

    def test_also_woken_zones(self):
        for dom, path, args, _z, woken, _d in ROUTE_TABLE:
            self.assertEqual(wakes(self, route(self, dom, path), args), woken, path)

    def test_woken_zones_never_gate_ok(self):
        for dom, path, args, _z, _w, _d in ROUTE_TABLE:
            r = route(self, dom, path)
            self.assertFalse(set(r.zones(args)) & set(wakes(self, r, args)), path)

    def test_deadlines(self):
        for dom, path, _a, _z, _w, deadline in ROUTE_TABLE:
            self.assertEqual(route(self, dom, path).deadline_s, deadline, path)

    def test_commands_wake_the_extra_zones_after_the_action(self):
        self.assertRegex(text("sw/commands.py"), r"\.wakes\(", "part D: Commands._exec must wake spec.wakes(args) too")

    def test_send_is_ok_when_the_action_returns(self):
        core = load(self, "sw.core", "send has no zone")
        r = route(self, "messaging", "/api/send")
        cmd = {"id": "cmd-00000009", "route": "/api/send", "body": "{}", "zones": list(r.zones(([], "x"))),
               "waiting": [], "state": "running", "message": None, "created_at": 1.0, "deadline_at": 31.0,
               "finished_seq": None, "done_at": None, "rev": 1}
        self.assertEqual(core.cmd_on_finish(cmd, 0, "delivered", 5, 2.0)["state"], "ok")


class FakeStore:
    def __init__(self, blocks, running=()):
        self._b, self._r = blocks, list(running)

    def block_data(self, name):
        return self._b.get(name)

    def commands(self, state=None):
        return self._r


class TestEvictPrecheck(unittest.TestCase):
    def test_slot_is_looked_up_in_the_slots_block(self):
        r = route(self, "slots", "/api/evict")
        store = FakeStore({"slots": {"slots": [{"id": "s-slot-1"}], "machine_max": 5}})
        self.assertIsNone(r.precheck(("s-slot-1", "release"), store))
        refused = r.precheck(("s-slot-9", "release"), store)
        self.assertIsNotNone(refused)
        self.assertEqual(refused[0], 400)

    def test_nothing_reads_the_legacy_block(self):
        self.assertNotIn("legacy", text("sw/domains/slots.py"))


class TestCacheRaceLifecycle(unittest.TestCase):
    """A pass of a gating zone that started before the command finished does not confirm; after, it does."""

    def cmd(self, r, args):
        zones = list(r.zones(args))
        return {"id": "cmd-0000000d", "route": "/x", "body": "{}", "zones": zones, "waiting": zones,
                "state": "running", "message": None, "created_at": 100.0, "deadline_at": 190.0,
                "finished_seq": None, "done_at": None, "rev": 1}

    def test_per_route(self):
        core = load(self, "sw.core", "cache race")
        for dom, path, args, zones, _w, _d in ROUTE_TABLE:
            if not zones:
                continue
            r = route(self, dom, path)
            c = core.cmd_on_finish(self.cmd(r, args), 0, "done", 12, 200.0)
            self.assertEqual(c["state"], "running", path)
            for z in zones:
                c = core.cmd_on_pass(c, z, 10, 201.0)
                c = core.cmd_on_pass(c, z, 12, 201.0)
            self.assertEqual(c["state"], "running", path + ": a pass that started before the finish confirmed")
            for z in zones:
                c = core.cmd_on_pass(c, z, 13, 202.0)
            self.assertEqual(c["state"], "ok", path + ": a pass after the finish did not confirm")

    def test_unrelated_zone_never_confirms(self):
        core = load(self, "sw.core", "cache race")
        for dom, path, args, zones, _w, _d in ROUTE_TABLE:
            if not zones:
                continue
            r = route(self, dom, path)
            c = core.cmd_on_finish(self.cmd(r, args), 0, "done", 12, 200.0)
            for other in sorted({"host", "slots", "sessions", "runners", "cleaners", "ci_gate", "legacy"} - set(zones)):
                c = core.cmd_on_pass(c, other, 99, 203.0)
            self.assertEqual(c["state"], "running", path)


def core_fn(case, name):
    core = load(case, "sw.core", "part D: " + name)
    if not hasattr(core, name):
        case.fail("part D: sw.core.%s is missing" % name)
    return getattr(core, name)


class TestHostPure(unittest.TestCase):
    def test_parse_docker_stats(self):
        out = core_fn(self, "parse_docker_stats")("a-app-1|12.5%|1.5GiB / 8GiB\nb-db-1|0.00%|512MiB / 8GiB\nbroken\n")
        self.assertEqual(list(out["a-app-1"]), [12.5, 1536.0])
        self.assertEqual(list(out["b-db-1"]), [0.0, 512.0])

    def test_parse_docker_stats_empty(self):
        self.assertEqual(core_fn(self, "parse_docker_stats")(""), {})

    def test_parse_host_exposes_stats_and_holds_no_clock(self):
        stats = {"a": [10.0, 100.0], "b": [2.55, 50.0]}
        h = core_fn(self, "parse_host")("MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n",
                                         "0.50 1.25 2.00 1/200 999\n", 8, stats)
        self.assertEqual(h["stats"], stats)
        self.assertEqual((h["cpus"], h["load1"], h["load5"]), (8, 0.5, 1.25))
        self.assertEqual((h["mem_total_mb"], h["mem_used_mb"]), (16000, 8000))
        self.assertEqual(h["docker_cpu"], 12.6)
        self.assertNotIn("ts", h)
        self.assertNotIn("now", h)


class TestBlockShapes(unittest.TestCase):
    def test_slots_block_carries_the_machine_cap(self):
        f = core_fn(self, "slots_block")
        self.assertEqual(f([{"id": "x"}], {"machine_max": 5}), {"slots": [{"id": "x"}], "machine_max": 5})
        self.assertEqual(f([], {"machine_max": 99})["machine_max"], 20)
        self.assertEqual(f([], {"machine_max": 0})["machine_max"], 0)
        self.assertEqual(f([], {"machine_max": -5})["machine_max"], 0)
        self.assertEqual(f([], {"machine_max": "x"})["machine_max"], 20)
        self.assertEqual(f([], {"machine_max": True})["machine_max"], 20)
        self.assertEqual(f([], {"machine_max": False})["machine_max"], 20)
        self.assertEqual(f([], {})["machine_max"], 20)

    def test_slots_block_two_arg_has_no_projects_or_default(self):
        f = core_fn(self, "slots_block")
        self.assertEqual(len(inspect.signature(f).parameters), 2)
        out = f([{"id": "acme-org-slot-1", "project": "acme-org"}], {"machine_max": 8})
        self.assertEqual(out, {"slots": [{"id": "acme-org-slot-1", "project": "acme-org"}], "machine_max": 8})

    def test_sessions_block_includes_default_limit(self):
        f = core_fn(self, "sessions_block")
        self.assertEqual(f([{"id": "s", "held_slots": 5}], {"default": 3}), {"sessions": [{"id": "s", "held_slots": 5}], "default_limit": 3})
        self.assertEqual(f([{"id": "s"}], {"default": 0}), {"sessions": [{"id": "s"}], "default_limit": 0})
        self.assertEqual(f([{"id": "s"}], {"default": 25}), {"sessions": [{"id": "s"}], "default_limit": 20})
        self.assertEqual(f([{"id": "s"}], {"default": True}), {"sessions": [{"id": "s"}], "default_limit": 1})
        self.assertEqual(f([], {}), {"sessions": [], "default_limit": 1})

    def test_sessions_reader_passes_limits_and_emits_default_limit(self):
        b = block(self, "sessions")
        sessions_mod = load(self, "sessions", "bin/sessions.py")
        rows = [{"id": "s1", "held_slots": 5, "tree_held": 5, "limit": 1, "is_over_limit": True}]
        lim = {"default": 3, "sessions": {"s1": 1}}
        with patch.object(sessions_mod, "read_all_leases", return_value=[]), \
                patch.object(sessions_mod, "read_limits", return_value=lim), \
                patch.object(sessions_mod, "discover_sessions", return_value=rows) as disc_mock:
            out = b.read(None)
        self.assertEqual(out, {"sessions": rows, "default_limit": 3})
        self.assertEqual(disc_mock.call_args.kwargs.get("limits"), lim)

    def test_api_state_flattens_the_new_blocks_to_todays_keys(self):
        core = load(self, "sw.core", "api_state")
        blocks = {"host": {"load1": 0.5, "stats": {}}, "slots": {"slots": [{"id": "a"}, {"id": "b"}], "machine_max": 7},
                  "sessions": {"sessions": [{"id": "s", "held_slots": 5}], "default_limit": 2}, "runners": [{"repo": "r", "busy": True}],
                  "cleaners": {"cleaners": {"fast": 1}, "rescue_bundles": [{"id": "p/l"}]}, "autoreview": {"x": 1}}
        out = core.api_state(blocks, [], [], 5.0, None)
        self.assertEqual(out["slots"], [{"id": "a"}, {"id": "b"}])
        self.assertEqual((out["machine_max"], out["machine_in_use"]), (7, 2))
        self.assertEqual(out["sessions"], [{"id": "s", "held_slots": 5}])
        self.assertEqual(out["default_limit"], 2)
        self.assertEqual(out["snap"]["default_limit"], 2)
        self.assertNotIn("projects", out)
        self.assertNotIn("project_default", out)
        self.assertEqual(out["ci"], [{"repo": "r", "busy": True}])
        self.assertEqual((out["cleaners"], out["rescue_bundles"]), ({"fast": 1}, [{"id": "p/l"}]))
        self.assertEqual(out["host"]["load1"], 0.5)
        self.assertEqual(out["autoreview"], {"x": 1})

    def test_api_state_defaults_without_blocks(self):
        out = load(self, "sw.core", "api_state").api_state({}, [], [], 5.0, None)
        self.assertEqual((out["slots"], out["sessions"], out["cleaners"], out["rescue_bundles"]), ([], [], {}, []))
        self.assertEqual((out["machine_max"], out["machine_in_use"]), (20, 0))
        self.assertEqual(out["default_limit"], 1)

    def test_api_state_keeps_the_other_keys(self):
        out = load(self, "sw.core", "api_state").api_state({}, [], [], 5.0, None)
        for k in ("ts", "now", "snap", "error", "events", "evicting", "results"):
            self.assertIn(k, out)


LEASE = {"project": "demo-slot-3", "holder": "issue-9", "owner": "me [abc123]", "harness": "claude",
         "ssh_port": 2203, "since": 1000.0, "ttl_seconds": 600}
RELATIVE = ("idle_seconds", "age_s", "ttl_left_s")


def build(case, now):
    mod = load(case, "slots", "bin/slots.py")
    if not hasattr(mod, "build_slot"):
        case.fail("part D: bin/slots.py needs build_slot(lease, sess, stats, branch, head, now=)")
    sess = {"harness": "claude", "title": "T", "root_id": "root1", "root_title": "Root"}
    return mod.build_slot(dict(LEASE), sess, {"demo-slot-3-app-1": [10.0, 100.0], "demo-slot-3-db-1": [2.0, 20.4]},
                          "feat", "abc1234", now=now)


class TestSlotEntry(unittest.TestCase):
    def test_absolute_times_are_stored(self):
        e = build(self, 5000.0)
        self.assertEqual(e["since"], 1000.0)
        self.assertEqual(e["ttl_until"], 1600.0)

    def test_relative_fields_are_not_stored(self):
        e = build(self, 5000.0)
        for k in RELATIVE:
            self.assertNotIn(k, e)

    def test_entry_is_the_same_whatever_the_clock(self):
        # an entry that changed with `now` would bump the rev of the block on every pass
        self.assertEqual(build(self, 5000.0), build(self, 100000.0))

    def test_identity_and_stats(self):
        e = build(self, 5000.0)
        self.assertEqual((e["id"], e["project"], e["slot"], e["holder"]), ("demo-slot-3", "demo", 3, "issue-9"))
        self.assertEqual((e["running"], e["cpu"], e["mem_mb"]), (True, 12.0, 120))
        self.assertEqual((e["branch"], e["head"], e["harness"]), ("feat", "abc1234", "claude"))
        self.assertEqual(e["ports"], {"ssh": 2203})
        self.assertEqual((e["group_ref"], e["group_title"]), ("root1", "Root"))

    def test_stopped_slot_has_no_stats(self):
        mod = load(self, "slots", "bin/slots.py")
        e = mod.build_slot(dict(LEASE), None, {}, None, None, now=5000.0)
        self.assertEqual((e["running"], e["cpu"], e["mem_mb"]), (False, 0, 0))

    def test_build_slot_does_not_fabricate_vite_without_ports_file(self):
        mod = load(self, "slots", "bin/slots.py")
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            with patch.object(mod, "DEV_CONTAINER_STATE", state):
                e_with_ssh = mod.build_slot(dict(LEASE), None, {}, "main", "abc", now=5000.0)
                self.assertEqual(e_with_ssh["ports"], {"ssh": 2203})
                self.assertNotIn("vite", e_with_ssh["ports"])

                no_ssh_lease = {"project": "demo-slot-5", "holder": "h", "since": 1000.0, "ttl_seconds": 60}
                e_no_ssh = mod.build_slot(no_ssh_lease, None, {}, "main", "abc", now=5000.0)
                self.assertEqual(e_no_ssh["ports"], {})
                self.assertNotIn("vite", e_no_ssh["ports"])

    def test_build_slot_uses_real_ports_file_only(self):
        mod = load(self, "slots", "bin/slots.py")
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "ports").mkdir()
            (state / "ports" / "demo-slot-4.ports").write_text("SSH_PORT=42064\n", encoding="utf-8")
            lease = {"project": "demo-slot-4", "holder": "h", "since": 1000.0, "ttl_seconds": 60, "ssh_port": 2204}
            with patch.object(mod, "DEV_CONTAINER_STATE", state):
                ssh_only = mod.build_slot(dict(lease), None, {}, "main", "abc", now=5000.0)
                self.assertEqual(ssh_only["ports"], {"ssh": 42064})
                self.assertNotIn("vite", ssh_only["ports"])

                (state / "ports" / "demo-slot-4.ports").write_text("DB_PORT=42065\n", encoding="utf-8")
                db_and_ssh = mod.build_slot(dict(lease), None, {}, "main", "abc", now=5000.0)
                self.assertEqual(db_and_ssh["ports"], {"db": 42065, "ssh": 2204})

                (state / "ports" / "demo-slot-4.ports").write_text(
                    "SSH_PORT=42064\nDB_PORT=42065\nVITE_DEV_PORT=42066\n", encoding="utf-8"
                )
                with_vite = mod.build_slot(dict(lease), None, {}, "main", "abc", now=5000.0)
                self.assertEqual(with_vite["ports"], {"ssh": 42064, "db": 42065, "vite_dev": 42066})

    def test_abandoned_slot_does_not_fabricate_vite_without_ports_file(self):
        mod = load(self, "slots", "bin/slots.py")
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "ports").mkdir()
            with patch.object(mod, "DEV_CONTAINER_STATE", state):
                for num in (0, 1, 3):
                    e = mod.abandoned_slot("demo", num, {}, "main", "abc")
                    self.assertEqual(e["ports"], {}, f"slot {num} must not fabricate ports when .ports is absent")
                    self.assertNotIn("vite", e["ports"])

                (state / "ports" / "demo-slot-3.ports").write_text("SSH_PORT=42048\nDB_PORT=42049\n", encoding="utf-8")
                e_real = mod.abandoned_slot("demo", 3, {}, "main", "abc")
                self.assertEqual(e_real["ports"], {"ssh": 42048, "db": 42049})
                self.assertNotIn("vite", e_real["ports"])


class TestDiscoverSlotsSignature(unittest.TestCase):
    def test_reads_stats_and_sessions_from_its_caller(self):
        mod = load(self, "slots", "bin/slots.py")
        self.assertTrue(hasattr(mod, "discover_slots"), "part D: discover_slots moves to bin/slots.py")
        params = set(inspect.signature(mod.discover_slots).parameters)
        self.assertTrue({"stats", "sessions"} <= params, params)

    def test_the_lookup_is_not_rebuilt_from_a_second_discover_sessions_call(self):
        load(self, "slots", "bin/slots.py")
        self.assertNotIn("discover_sessions", text("bin/slots.py"))


PS = "ci-runner-hanzhad-host_repo-pc-2-1|big|hanzhad/host_repo\nci-runner-x-pc-1-1|small|\nnoise\n"


class TestRunnersPure(unittest.TestCase):
    def mod(self):
        return load(self, "runners", "bin/runners.py")

    def test_parse_ps(self):
        rows = self.mod().parse_ps(PS)
        self.assertEqual([tuple(r) for r in rows][:2], [("ci-runner-hanzhad-host_repo-pc-2-1", "big", "hanzhad/host_repo"),
                                                        ("ci-runner-x-pc-1-1", "small", "")])

    def test_build_runner_busy(self):
        r = self.mod().build_runner("ci-runner-hanzhad-host_repo-pc-2-1", "big", "hanzhad/host_repo", [40.0, 1000.4],
                                    ["77", "01:02", "Runner.Worker"], "build")
        self.assertEqual(r, {"repo": "hanzhad/host_repo", "n": 2, "class": "big", "busy": True, "job": "build",
                             "job_time": "01:02", "cpu": 40.0, "mem_mb": 1000})

    def test_build_runner_idle_falls_back_to_the_name(self):
        r = self.mod().build_runner("ci-runner-x-pc-1-1", "", "", [0, 0], None, None)
        self.assertEqual((r["repo"], r["n"], r["class"], r["busy"], r["job"], r["job_time"]),
                         ("x", 1, "big", False, None, None))

    def test_sort_and_busy_count(self):
        m = self.mod()
        rs = [{"class": "small", "repo": "b", "n": 1, "busy": True}, {"class": "big", "repo": "z", "n": 2, "busy": False},
              {"class": "big", "repo": "a", "n": 1, "busy": True}]
        self.assertEqual([(r["class"], r["repo"]) for r in m.sort_runners(rs)], [("big", "a"), ("big", "z"), ("small", "b")])
        self.assertEqual(m.busy_count(rs), 2)
        self.assertEqual(m.busy_count([]), 0)

    def test_cli_and_collect_exist(self):
        m = self.mod()
        self.assertTrue(callable(getattr(m, "main", None)))
        self.assertIn("stats", inspect.signature(m.collect).parameters)
        self.assertIn("__main__", text("bin/runners.py"))
        self.assertNotIn("snapshot", text("bin/runners.py"))


class TestSlotsDomainLimitsAndPause(unittest.TestCase):
    def test_slots_reader_returns_slots_and_machine_max_only(self):
        b = block(self, "slots")
        slots_mod = load(self, "slots", "bin/slots.py")
        sessions_mod = load(self, "sessions", "bin/sessions.py")
        store = FakeStore({"host": {"stats": {}}, "sessions": {"sessions": []}})
        fake_slots = [{"id": "acme-org-slot-1", "project": "acme-org"}]
        fake_limits = {"default": 1, "machine_max": 10}

        with patch.object(slots_mod, "discover_slots", return_value=fake_slots), \
                patch.object(sessions_mod, "read_limits", return_value=fake_limits):
            out = b.read(store)

        self.assertEqual(out, {"slots": fake_slots, "machine_max": 10})
        self.assertNotIn("projects", out)
        self.assertNotIn("project_default", out)

    def test_validate_cap_accepts_deltas_and_pause_and_rejects_bad_inputs(self):
        v = route(self, "slots", "/api/cap").validate
        self.assertEqual(v({"delta": -1}), (-1,))
        self.assertEqual(v({"delta": 1}), (1,))
        self.assertEqual(v({"mode": "pause"}), ("pause",))
        for bad in (
            {"delta": 0},
            {"delta": 2},
            {"delta": -2},
            {"delta": True},
            {"delta": False},
            {"delta": "1"},
            {"mode": "bogus"},
            {"mode": ""},
            {},
        ):
            self.assertIsNone(v(bad), bad)

    def test_do_cap_supports_pause(self):
        slots_dom = domain(self, "slots")
        r = route(self, "slots", "/api/cap")
        with patch.object(slots_dom, "run_bin", return_value=(0, "0")) as mock_run, \
                patch.object(slots_dom, "note"):
            try:
                res = r.run(("pause",))
            except Exception as e:
                self.fail(f"do_cap(('pause',)) raised {type(e).__name__}: {e}")
        self.assertEqual(res, (0, "0"))
        self.assertEqual(mock_run.call_args[0][:2], ("setcap.py", "pause"))

    def test_validate_limit_accepts_valid_and_rejects_invalid(self):
        v = route(self, "sessions", "/api/limit").validate
        self.assertEqual(v({"ref": "s1", "delta": 1}), ("s1", 1))
        self.assertEqual(v({"ref": "local_abc-123", "delta": -1}), ("local_abc-123", -1))
        self.assertEqual(v({"ref": "s1", "mode": "pause"}), ("s1", "pause"))
        for bad in (
            {},
            {"delta": 1},
            {"ref": "", "delta": 1},
            {"ref": "   ", "delta": 1},
            {"ref": None, "delta": 1},
            {"ref": 123, "delta": 1},
            {"ref": "s1"},
            {"ref": "s1", "delta": 0},
            {"ref": "s1", "delta": 2},
            {"ref": "s1", "delta": -2},
            {"ref": "s1", "delta": True},
            {"ref": "s1", "delta": False},
            {"ref": "s1", "delta": "1"},
            {"ref": "s1", "mode": "bogus"},
            {"ref": "s1", "mode": ""},
        ):
            self.assertIsNone(v(bad), bad)

    def test_do_limit_invokes_setlimit_bin_under_action_lock(self):
        from sw import ctx

        sessions_dom = domain(self, "sessions")
        r = route(self, "sessions", "/api/limit")
        seen = {}

        def fake_run(*a, **kw):
            seen["locked"] = ctx.action_lock.locked()
            return 0, "2"

        with patch.object(sessions_dom, "run_bin", side_effect=fake_run) as mock_run, \
                patch.object(sessions_dom, "note"):
            res = r.run(("s1", 1))
            self.assertEqual(res, (0, "2"))
            self.assertTrue(seen["locked"])
            self.assertEqual(mock_run.call_args[0][0], "setlimit.py")
            self.assertEqual(mock_run.call_args[0][1], "s1")
            self.assertEqual(str(mock_run.call_args[0][2]), "1")

            res_p = r.run(("s1", "pause"))
            self.assertEqual(res_p, (0, "2"))
            self.assertEqual(mock_run.call_args[0][:3], ("setlimit.py", "s1", "pause"))
        self.assertFalse(ctx.action_lock.locked())


class TestSlotMultiRepoEphemeralToolbox(unittest.TestCase):
    def test_read_slot_repos_state_file_and_lease_fallback(self):
        import json

        mod = load(self, "slots", "bin/slots.py")
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "repos").mkdir()
            sid = "demo-slot-1"
            lease_with_repos = {
                "project": sid,
                "holder": "h",
                "since": 1000.0,
                "ttl_seconds": 60,
                "repos": [
                    {"alias": "from-lease", "path": "docs/fix", "mode": "rw"},
                    {"alias": ""},
                    "not-a-dict",
                ],
            }

            with patch.object(mod, "DEV_CONTAINER_STATE", state):
                # State file absent -> falls back to normalized lease.repos
                self.assertEqual(
                    mod._read_slot_repos(sid, lease_with_repos),
                    [{"alias": "from-lease", "path": "docs/fix", "mode": "rw"}],
                )
                # Lease without repos -> []
                self.assertEqual(mod._read_slot_repos(sid, {"project": sid}), [])

                # Invalid JSON in state file -> falls back to lease.repos
                (state / "repos" / f"{sid}.json").write_text("{broken", encoding="utf-8")
                self.assertEqual(
                    mod._read_slot_repos(sid, lease_with_repos),
                    [{"alias": "from-lease", "path": "docs/fix", "mode": "rw"}],
                )

                # Non-list JSON in state file -> falls back to lease.repos
                (state / "repos" / f"{sid}.json").write_text('{"alias": "x"}', encoding="utf-8")
                self.assertEqual(
                    mod._read_slot_repos(sid, lease_with_repos),
                    [{"alias": "from-lease", "path": "docs/fix", "mode": "rw"}],
                )

                # Valid list in state file -> overrides lease.repos, skips invalid entries, preserves attach
                (state / "repos" / f"{sid}.json").write_text(
                    json.dumps([
                        {"alias": "docs", "path": "docs/repo", "mode": "ro", "attach": "work"},
                        {"alias": "lib", "path": "lib/sub", "mode": "rw"},
                        {"alias": ""},
                        {"alias": 123, "path": "x", "mode": "ro"},
                        {"path": "no-alias", "mode": "ro"},
                        "str-entry",
                    ]),
                    encoding="utf-8",
                )
                self.assertEqual(
                    mod._read_slot_repos(sid, lease_with_repos),
                    [
                        {"alias": "docs", "path": "docs/repo", "mode": "ro", "attach": "work"},
                        {"alias": "lib", "path": "lib/sub", "mode": "rw"},
                    ],
                )

    def test_slot_has_toolbox_marker_and_sidecar_stats(self):
        mod = load(self, "slots", "bin/slots.py")
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "toolbox").mkdir()
            sid = "demo-slot-2"
            with patch.object(mod, "DEV_CONTAINER_STATE", state):
                self.assertFalse(mod._slot_has_toolbox(sid, {}))
                self.assertFalse(mod._slot_has_toolbox(sid, {f"{sid}-app-1": [1.0, 10.0], "other-toolbox-1": [1.0, 10.0]}))
                self.assertTrue(mod._slot_has_toolbox(sid, {f"{sid}-toolbox-1": [0.5, 20.0]}))

                # Directory with sid name is not a marker file
                (state / "toolbox" / sid).mkdir()
                self.assertFalse(mod._slot_has_toolbox(sid, {}))
                (state / "toolbox" / sid).rmdir()

                # Marker file present -> True even with empty stats
                (state / "toolbox" / sid).write_text("1\n", encoding="utf-8")
                self.assertTrue(mod._slot_has_toolbox(sid, {}))

    def test_build_slot_and_abandoned_slot_expose_repos_ephemeral_toolbox(self):
        import json

        mod = load(self, "slots", "bin/slots.py")
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "repos").mkdir()
            (state / "toolbox").mkdir()
            sid = "demo-slot-7"
            (state / "repos" / f"{sid}.json").write_text(
                json.dumps([{"alias": "docs", "path": "docs/fix", "mode": "rw"}]),
                encoding="utf-8",
            )
            (state / "toolbox" / sid).write_text("1\n", encoding="utf-8")
            lease = {"project": sid, "holder": "h", "since": 1000.0, "ttl_seconds": 60, "ephemeral": True}
            with patch.object(mod, "DEV_CONTAINER_STATE", state):
                e = mod.build_slot(lease, None, {}, "main", "abc", now=2000.0)
                self.assertIs(e["ephemeral"], True)
                self.assertIs(e["toolbox"], True)
                self.assertEqual(e["repos"], [{"alias": "docs", "path": "docs/fix", "mode": "rw"}])

                plain = mod.build_slot({"project": "demo-slot-8", "holder": "h", "since": 1000.0, "ttl_seconds": 60}, None, {}, "main", "abc", now=2000.0)
                self.assertIs(plain["ephemeral"], False)
                self.assertIs(plain["toolbox"], False)
                self.assertEqual(plain["repos"], [])

                ab = mod.abandoned_slot("demo", 9, {}, "main", "abc")
                self.assertEqual(ab["repos"], [])
                self.assertIs(ab["ephemeral"], False)
                self.assertIs(ab["toolbox"], False)


if __name__ == "__main__":
    unittest.main()
