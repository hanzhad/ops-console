"""Zero-dependency fixture seeding for `python3 server.py --demo`."""

import datetime
import time


def seed_demo(store):
    """Populate realistic sanitized blocks and log events into `store`."""
    now = time.time()
    reset_iso = datetime.datetime.fromtimestamp(now + 2400, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    store.put_block("host", {
        "load1": 1.42,
        "load5": 1.18,
        "mem_used_mb": 11264,
        "mem_total_mb": 32768,
        "docker_cpu": 24.6,
    }, pass_seq=1, period=10)

    store.put_block("sessions", {
        "default_limit": 2,
        "sessions": [
            {
                "id": "sess-api-1",
                "ref": "sess-api-1",
                "title": "feat(#14): token rotation & audit log",
                "harness": "antigravity",
                "cwd": "/home/user/Projects/acme-api",
                "is_active": True,
                "last_active": now - 45,
                "held_slots": 2,
                "tree_held": 2,
                "limit": 2,
            },
            {
                "id": "sess-api-sub",
                "ref": "sess-api-sub",
                "title": "test-writer: rotation edge cases",
                "harness": "claude",
                "parent_id": "sess-api-1",
                "root_id": "sess-api-1",
                "cwd": "/home/user/Projects/acme-api",
                "is_active": True,
                "last_active": now - 18,
                "held_slots": 0,
                "tree_held": 0,
                "limit": 2,
            },
            {
                "id": "sess-web-2",
                "ref": "sess-web-2",
                "title": "fix(#27): SSE reconnect backoff",
                "harness": "claude",
                "cwd": "/home/user/Projects/acme-web",
                "is_active": True,
                "last_active": now - 190,
                "held_slots": 1,
                "tree_held": 1,
                "limit": 2,
            },
        ],
    }, pass_seq=1, period=15)

    store.put_block("slots", {
        "machine_max": 8,
        "slots": [
            {
                "id": "0-slot-1-acme-api",
                "slot": 1,
                "project": "acme-api",
                "holder": "issue-14",
                "session_ref": "sess-api-1",
                "group_ref": "sess-api-1",
                "group_title": "feat(#14): token rotation & audit log",
                "session_title": "implement: rotation service",
                "harness": "antigravity",
                "branch": "feat/issue-14-rotation",
                "head": "a1b2c3d",
                "ports": {"SSH": 42016, "API": 42017},
                "since": now - 1840,
                "last_activity": now - 35,
                "ttl_until": now + 5400,
                "cpu": 18.4,
                "mem_mb": 640,
                "running": True,
            },
            {
                "id": "0-slot-2-acme-api",
                "slot": 2,
                "project": "acme-api",
                "holder": "issue-14-tests",
                "session_ref": "sess-api-1",
                "group_ref": "sess-api-1",
                "group_title": "feat(#14): token rotation & audit log",
                "session_title": "test-writer: integration suite",
                "harness": "claude",
                "branch": "test/issue-14-rotation",
                "head": "d4e5f6a",
                "ports": {"SSH": 42032, "DB": 42033},
                "since": now - 920,
                "last_activity": now - 12,
                "ttl_until": now + 6200,
                "cpu": 9.2,
                "mem_mb": 412,
                "running": True,
            },
            {
                "id": "0-slot-3-acme-web",
                "slot": 3,
                "project": "acme-web",
                "holder": "issue-27",
                "session_ref": "sess-web-2",
                "group_ref": "sess-web-2",
                "group_title": "fix(#27): SSE reconnect backoff",
                "session_title": "implement-light: reconnect timer",
                "harness": "claude",
                "branch": "fix/issue-27-sse",
                "head": "7c8d9e0",
                "ports": {"SSH": 42048, "VITE": 42049},
                "since": now - 4200,
                "last_activity": now - 180,
                "ttl_until": now + 3000,
                "cpu": 4.1,
                "mem_mb": 520,
                "running": True,
            },
        ],
    }, pass_seq=1, period=15)

    store.put_block("cleaners", {
        "fast": {"last_run": now - 120, "next_due_in_seconds": 180, "result": "ok", "last_log": "swept 0 stale leases"},
        "slow": {"last_run": now - 1800, "next_due_in_seconds": 1800, "result": "ok", "last_log": "verified 3 image digests"},
        "rescue_bundles": [
            {"id": "acme-web/slot-5", "project": "acme-web", "lease": "slot-5", "branches": ["fix/issue-19"], "size_mb": 1.4, "age": 5400, "has_verdict": True, "verdict_summary": "Clean rescue archive"},
        ],
    }, pass_seq=1, period=30)

    store.put_block("runners", [
        {"name": "ci-runner-acme-api-mybox-1", "repo": "acme/api", "class": "big", "busy": True, "job": "e2e-tests", "elapsed_s": 74, "cpu": 62.5, "mem_mb": 2150},
        {"name": "ci-runner-acme-web-mybox-2", "repo": "acme/web", "class": "little", "busy": False, "job": "lint", "elapsed_s": 12, "cpu": 1.2, "mem_mb": 307},
    ], pass_seq=1, period=10)

    store.put_block("ci_gate", {
        "running": True,
        "locked": False,
        "paused": False,
        "budget": 10,
        "cap": 4,
        "used": 4,
        "boot": [10, 4],
        "class_costs": {"big": 4, "little": 2, "mega": 10},
        "commit": "69ebcd7",
        "token_expires": "2027-02-01",
        "rate_limit": {"remaining": 4620, "limit": 5000, "throttled": False, "reset_at": reset_iso},
        "repos": [
            {"repo": "acme/api", "local": True, "mega": False},
            {"repo": "acme/web", "local": True, "mega": False},
            {"repo": "acme/docs", "local": False, "mega": False},
        ],
    }, pass_seq=1, period=15)

    store.put_block("gh_queue", [
        {"run_id": 10042, "repo": "acme/api", "name": "ci / e2e", "branch": "feat/issue-14-rotation", "status": "in_progress"},
        {"run_id": 10043, "repo": "acme/web", "name": "ci / unit", "branch": "fix/issue-27-sse", "status": "queued"},
    ], pass_seq=1, period=15)

    store.put_block("autoreview", {
        "container": {"running": True, "paused": False, "uptime_s": 14200},
        "health": {"tokens": [], "harnesses": {"claude-code": {"state": "ok"}}},
        "global_harness": "claude-code",
        "global_model": "sonnet",
        "available_harnesses": ["claude-code", "antigravity"],
        "available_models": {
            "claude-code": ["sonnet", "opus", "haiku"],
            "antigravity": ["gemini-4-neon:medium", "gemini-4-carbon:high"],
        },
        "global": {"harness": "claude-code", "model": "sonnet", "interval": 180},
        "projects": {
            "acme/api": {"paused": False, "all": True, "self_review": True},
            "acme/web": {"paused": False, "all": False, "self_review": True},
        },
    }, pass_seq=1, period=20)

    for line in [
        "demo mode seeded: 8 blocks, 3 active slots, 2 CI runners",
        "[slots] 0-slot-1-acme-api leased by sess-api-1 (feat/issue-14-rotation)",
        "[ci_gate] spawned ci-runner-acme-api-mybox-1 (class=big, 4c)",
        "[autoreview] acme/api PR #42 reviewed (No findings)",
    ]:
        store.add_event(line)
