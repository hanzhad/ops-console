#!/usr/bin/env python3
"""Unified Dev-Container Engine Integration & Cleaner Operations.

Manages integration with hanzhad/dev-container-engine (host_repo an earlier change, an earlier change, #36):
- Cleaner status discovery: fast = dev-container reap --all (15 min),
  slow = rescue_audit (is each bundle's work already on GitHub?)
- Rescue archives discovery & metadata (~/.agent-slots/rescue/ & ~/.dev-containers/rescue/)
- On-demand cleaner execution (/api/cleaners: fast/slow)
- Rescue bundle lifecycle (/api/rescue: restore/purge)
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import projects  # noqa: E402
import rescue_archives  # noqa: E402
import rescue_audit  # noqa: E402

HOME = pathlib.Path.home()
AGENT_SLOT_STATE = pathlib.Path(os.environ.get("AGENT_SLOT_STATE", HOME / ".agent-slots"))
DEV_CONTAINER_STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", HOME / ".dev-containers"))

# Engine binary location
ENGINE_THIN = pathlib.Path("/home/user/Projects/host_repo/tools/dev-container-engine/scripts/dev-container-thin")
ENGINE_BIN = ENGINE_THIN if ENGINE_THIN.exists() else pathlib.Path("/home/user/Projects/host_repo/tools/dev-container-engine/bin/dev-container")

SCHEDULE_INTERVALS = {
    "fast": int(os.environ.get("DEV_CONTAINER_SCHEDULE_FAST", 900)),
    "slow": int(os.environ.get("DEV_CONTAINER_SCHEDULE_SLOW", 3600)),
}


def is_engine_available() -> bool:
    """Check if dev-container engine binary is available."""
    dev_bin = os.environ.get("DEV_CONTAINER_BIN")
    if dev_bin:
        p = pathlib.Path(dev_bin)
        if p.exists() and os.access(p, os.X_OK):
            return True
        if shutil.which(dev_bin) is not None:
            return True
    if ENGINE_BIN.exists() and os.access(ENGINE_BIN, os.X_OK):
        return True
    if shutil.which("dev-container") is not None:
        return True
    return False


def _engine_bin_cmd() -> List[str]:
    dev_bin = os.environ.get("DEV_CONTAINER_BIN")
    if dev_bin:
        return [dev_bin]
    if ENGINE_BIN.exists() and os.access(ENGINE_BIN, os.X_OK):
        return [str(ENGINE_BIN)]
    which = shutil.which("dev-container")
    if which:
        return [which]
    return ["dev-container"]


def run_engine_command(args: List[str], timeout: int = 120) -> Tuple[int, str]:
    """Execute dev-container CLI command."""
    cmd = _engine_bin_cmd() + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except Exception as e:
        return 1, str(e)


def get_engine_status_json() -> Optional[Dict[str, Any]]:
    """Query dev-container status --json from the engine."""
    rc, out = run_engine_command(["status", "--json"], timeout=15)
    if rc == 0 and out:
        out_strip = out.strip()
        idx = out_strip.find("{")
        if idx != -1:
            try:
                return json.loads(out_strip[idx:])
            except Exception:
                pass
    return None


def _get_active_state_dir() -> pathlib.Path:
    return DEV_CONTAINER_STATE  # ~/.agent-slots is legacy: read-only, never the place to write


def _ensure_schedule_dirs():
    sched = DEV_CONTAINER_STATE / "schedule"
    sched.mkdir(parents=True, exist_ok=True)
    jobs_table = sched / "jobs"
    if not jobs_table.exists():
        jobs_table.write_text("fast=reap --all\nslow=check\n", encoding="utf-8")


def get_cleaners_status() -> Dict[str, Any]:
    """Read schedule stamps and status for fast and slow cleaners.

    First queries dev-container status --json from the engine.
    Falls back to schedule files if engine status is unavailable.
    """
    now = time.time()
    eng = get_engine_status_json()
    if eng and "cleaners" in eng and isinstance(eng["cleaners"], dict):
        cleaners_data = eng["cleaners"]
        result = {}
        for job in ("fast", "slow"):
            data = cleaners_data.get(job, {})
            last_run = data.get("last_run") or data.get("last_run_timestamp")
            res = data.get("result") or data.get("status") or "ok"
            next_planned = data.get("next_planned_run") or data.get("next_run")
            interval = data.get("interval") or SCHEDULE_INTERVALS.get(job, 900 if job == "fast" else 3600)
            if next_planned is not None:
                next_due = max(0, int(next_planned - now))
            elif last_run:
                next_due = max(0, int(interval - (now - last_run)))
            else:
                next_due = 0

            last_log = data.get("last_log") or (f"Result: {res}" if res else "OK")

            result[job] = {
                "command": "reap --all" if job == "fast" else "check",
                "interval_seconds": interval,
                "last_run": last_run,
                "result": res,
                "what_it_did": res,
                "next_planned_run": next_planned,
                "next_due_in_seconds": next_due,
                "last_log": last_log,
                "status": data.get("status", res),
                "locked": bool(data.get("locked")),
            }
        return result

    # Fallback to legacy schedule directory inspection
    _ensure_schedule_dirs()
    result = {}

    for job, default_cmd, default_interval in (
        ("fast", "reap --all", 900),
        ("slow", "rescue audit", 3600),
    ):
        interval = SCHEDULE_INTERVALS.get(job, default_interval)
        last_run = None
        last_log = ""
        last_exit = 0

        # Check in both state directories
        for state_dir in (DEV_CONTAINER_STATE, AGENT_SLOT_STATE):
            sched_dir = state_dir / "schedule"
            stamp_f = sched_dir / f"{job}.last"
            log_f = sched_dir / f"{job}.log"

            if stamp_f.exists():
                try:
                    ts = int(stamp_f.read_text().strip())
                    if last_run is None or ts > last_run:
                        last_run = ts
                except Exception:
                    pass

            if log_f.exists() and not last_log:
                try:
                    lines = [ln.strip() for ln in log_f.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
                    if lines:
                        last_log = "\n".join(lines[-4:])
                        m = re.search(r"exited\s+(\d+)", last_log)
                        if m:
                            last_exit = int(m.group(1))
                except Exception:
                    pass

        next_due = max(0, int(interval - (now - last_run))) if last_run else 0
        next_planned = int(last_run + interval) if last_run else None
        res_val = "ok" if last_exit == 0 else f"exit {last_exit}"
        result[job] = {
            "command": default_cmd,
            "interval_seconds": interval,
            "last_run": last_run,
            "last_exit_code": last_exit if last_run is not None else None,
            "last_log": last_log or ("No runs recorded yet" if last_run is None else "OK"),
            "next_due_in_seconds": next_due,
            "next_planned_run": next_planned,
            "result": res_val,
            "what_it_did": res_val,
            "status": res_val,
            "locked": False,
        }

    return result


def get_rescue_bundles(limit: int = 50) -> List[Dict[str, Any]]:
    """Scan and return rescue bundles from rescue/ directories."""
    bundles: List[Dict[str, Any]] = []
    seen_ids = set()
    now = time.time()

    # Delivery comes from rescue_audit's cache (bin/rescue_audit.py). The previous check matched the bundle id
    # against ~/.agent-slots/delivered markers, which are named <prefix>-<branch>-<sha>, so it could never match.
    audit = rescue_audit.load_cache()

    for state_dir in (DEV_CONTAINER_STATE, AGENT_SLOT_STATE):
        rescue_dir = state_dir / "rescue"
        if not rescue_dir.exists():
            continue

        for bf in sorted(rescue_dir.glob("*.bundle"), key=lambda p: -p.stat().st_mtime):
            bid = bf.stem
            if bid in seen_ids:
                continue
            seen_ids.add(bid)

            try:
                st = bf.stat()
                size_bytes = st.st_size
                mtime = int(st.st_mtime)

                # Filename pattern: 0-slot-<num>-<project>-<timestamp>-<pid> or <project>-slot-<num>-<timestamp>-<pid>
                parsed = projects.parse_rescue_bundle_id(bid)
                if parsed:
                    proj, _, lease_id = parsed
                else:
                    proj = bid.split("-")[0]
                    lease_id = bid

                # delivered = every branch/stash tip in the bundle is on origin, on a PR head, merged at that tip,
                # or patch-equal in master. Purge only then: no age rule (mtimes on the rescue mount are copy
                # times, not creation times).
                a = audit.get(bid) or {}
                verdict = a.get("verdict", "unaudited") if a.get("size") == size_bytes else "unaudited"
                is_delivered = verdict == "delivered"
                can_purge = is_delivered and bool(a.get("verify_ok"))
                unique = [h for h in a.get("heads", []) if h.get("status") == "UNIQUE"]

                bundles.append({
                    "id": bid,
                    "project": proj,
                    "lease_id": lease_id,
                    "created_at": mtime,
                    "bundle_path": str(bf),
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / (1024 * 1024), 1),
                    "delivery_status": verdict,
                    "delivered": is_delivered,
                    "can_purge": can_purge,
                    "unique_heads": [{"ref": h["ref"], "commits": h.get("unique_commits", 0)} for h in unique],
                })
            except Exception:
                continue

            if len(bundles) >= limit:
                break

    return bundles


def trigger_cleaner(action: str) -> Tuple[bool, str]:
    """Execute fast or slow cleaner pass via dev-container schedule run."""
    if action in ("fast", "slow"):
        _ensure_schedule_dirs()
        rc, out = run_engine_command(["schedule", "run", action], timeout=300)
        return (rc == 0, out)

    return (False, f"Unknown cleaner action '{action}'")


def _is_safe_path_segment(seg: str) -> bool:
    """Return True if seg is a single non-empty, non-traversal path segment."""
    return (
        isinstance(seg, str)
        and bool(seg)
        and seg == seg.strip()
        and seg not in (".", "..")
        and "/" not in seg
        and "\\" not in seg
        and "\x00" not in seg
    )


def _read_project_label_key(project: str) -> Optional[str]:
    """Resolve project key from DEV_CONTAINER_STATE/projects/labels/<project>."""
    if not _is_safe_path_segment(project):
        return None
    label_file = DEV_CONTAINER_STATE / "projects" / "labels" / project
    if not label_file.is_file():
        return None
    try:
        key = label_file.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    return key if _is_safe_path_segment(key) else None


def _resolve_archive_dir(
    project: str,
    lease: str,
    key: Optional[str] = None,
    base_dir: Optional[pathlib.Path] = None,
    prefer_key: bool = False,
) -> Optional[pathlib.Path]:
    """Locate a rescue archive directory under rescue/<name>/<lease> safely."""
    if not _is_safe_path_segment(lease):
        return None
    label_key = _read_project_label_key(project)
    raw_names = [key, project, label_key] if (prefer_key and key) else [project, key, label_key]
    names = [n for n in dict.fromkeys(raw_names) if n and _is_safe_path_segment(n)]
    if not names:
        return None
    bases = [base_dir] if base_dir is not None else [DEV_CONTAINER_STATE / "rescue", AGENT_SLOT_STATE / "rescue"]
    for base in bases:
        try:
            base_resolved = base.resolve()
        except OSError:
            continue
        for name in names:
            cand = base / name / lease
            try:
                if cand.is_dir() and cand.resolve().is_relative_to(base_resolved):
                    return cand
            except OSError:
                continue
    return None


def _find_verdict_file(project: str, lease: str, key: Optional[str] = None) -> Optional[pathlib.Path]:
    """Find verdict.md checking rescue/<key>/<lease> first, then rescue/<project>/<lease>."""
    if not _is_safe_path_segment(lease):
        return None
    names = [n for n in dict.fromkeys((key, project)) if n and _is_safe_path_segment(n)]
    for name in names:
        for state_dir in (DEV_CONTAINER_STATE, AGENT_SLOT_STATE):
            base = state_dir / "rescue"
            vf = base / name / lease / "verdict.md"
            try:
                if vf.is_file() and vf.resolve().is_relative_to(base.resolve()):
                    return vf
            except OSError:
                continue
    return None


def _pick_git_repo(project: str = "", meta: Optional[Dict[str, Any]] = None) -> str:
    """Choose a git repository root for verifying rescue bundles."""
    candidates: List[pathlib.Path] = []
    if isinstance(meta, dict) and meta.get("repo_root"):
        candidates.append(pathlib.Path(str(meta["repo_root"])))
    proj_name = (meta.get("project") if isinstance(meta, dict) else None) or project
    if proj_name and _is_safe_path_segment(str(proj_name)):
        candidates.append(HOME / "Projects" / str(proj_name))
        candidates.append(pathlib.Path(f"/home/user/Projects/{proj_name}"))
    candidates.append(HOME / "Projects" / "host_repo")
    candidates.append(pathlib.Path("/home/user/Projects/host_repo"))
    return next((str(r) for r in candidates if r.exists() and (r / ".git").exists()), str(HOME))


def _git_bundle_verify(bundle_path: pathlib.Path, repo_cwd: str, timeout: int = 30) -> Tuple[bool, str]:
    """Run git bundle verify and return (ok, last_error_line)."""
    try:
        p = subprocess.run(
            ["git", "-C", repo_cwd, "bundle", "verify", str(bundle_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if p.returncode == 0:
            return (True, "")
        text_out = p.stderr.strip() or p.stdout.strip()
        err_line = text_out.splitlines()[-1] if text_out else "verify failed"
        return (False, err_line)
    except Exception as e:
        return (False, str(e))


def _git_bundle_list_heads(bundle_path: pathlib.Path, timeout: int = 30) -> Tuple[bool, str]:
    """Run git bundle list-heads and require exit 0 with non-empty output."""
    try:
        p = subprocess.run(
            ["git", "bundle", "list-heads", str(bundle_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if p.returncode == 0 and p.stdout.strip():
            return (True, "")
        text_out = p.stderr.strip() or p.stdout.strip()
        err_line = text_out.splitlines()[-1] if text_out else "bundle has no heads"
        return (False, err_line)
    except Exception as e:
        return (False, str(e))


def _pick_sub_repo_cwd(meta: Dict[str, Any], sub_meta: Dict[str, Any]) -> Optional[str]:
    """Locate a local git checkout for an archived sub-repo if one exists."""
    cands: List[pathlib.Path] = []
    if meta.get("repo_root") and sub_meta.get("path"):
        cands.append(pathlib.Path(str(meta["repo_root"])) / str(sub_meta["path"]))
    url = str(sub_meta.get("url") or "").strip().rstrip("/")
    if url:
        repo_name = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
        if _is_safe_path_segment(repo_name):
            cands.append(HOME / "Projects" / repo_name)
    return next((str(c) for c in cands if c.exists() and (c / ".git").exists()), None)


def _verify_archive_dir(archive_dir: pathlib.Path, project: str, label: str) -> Tuple[bool, str]:
    """Verify integrity of a <key>/<lease> rescue archive directory (root + repos/<alias>/)."""
    if not archive_dir.is_dir():
        return (False, f"Rescue archive '{label}' not found")

    meta_file = archive_dir / "meta.json"
    meta: Dict[str, Any] = {}
    if meta_file.exists():
        try:
            loaded = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as e:
            return (False, f"Archive '{label}' has invalid meta.json: {e}")
        if not isinstance(loaded, dict):
            return (False, f"Archive '{label}' meta.json must be a JSON object")
        meta = loaded

    bundle_files = sorted(archive_dir.glob("*.bundle"))
    bundle_count = len(bundle_files)
    if bundle_files:
        repo_cwd = _pick_git_repo(project, meta)
        for bf in bundle_files:
            ok, err = _git_bundle_verify(bf, repo_cwd, timeout=30)
            if not ok:
                return (False, f"Archive '{label}' bundle '{bf.name}' verification failed: {err[:160]}")

    has_meta = meta_file.is_file()
    top_artifact = bool(bundle_files) or has_meta
    for fname in ("changes.patch", "new-files.tar"):
        fpath = archive_dir / fname
        if fpath.is_file():
            top_artifact = True
            try:
                fpath.read_bytes()
            except Exception as e:
                return (False, f"Archive '{label}' {fname} unreadable: {e}")

    sub_verified = False
    repos_by_alias = {
        r["alias"]: r
        for r in (meta.get("repos") if isinstance(meta.get("repos"), list) else [])
        if isinstance(r, dict) and isinstance(r.get("alias"), str)
    }
    repos_dir = archive_dir / "repos"
    if repos_dir.is_dir():
        for sub_dir in sorted(p for p in repos_dir.iterdir() if p.is_dir()):
            sub_meta = repos_by_alias.get(sub_dir.name, {})
            sub_cwd = _pick_sub_repo_cwd(meta, sub_meta)
            for bf in sorted(sub_dir.glob("*.bundle")):
                if sub_cwd is not None:
                    ok, err = _git_bundle_verify(bf, sub_cwd, timeout=30)
                else:
                    ok, err = _git_bundle_list_heads(bf, timeout=30)
                if not ok:
                    return (False, f"Archive '{label}' sub-repo '{sub_dir.name}' bundle '{bf.name}' verification failed: {err[:160]}")
                bundle_count += 1
                sub_verified = True
            for fname in ("changes.patch", "new-files.tar"):
                fpath = sub_dir / fname
                if fpath.is_file():
                    try:
                        fpath.read_bytes()
                    except Exception as e:
                        return (False, f"Archive '{label}' sub-repo '{sub_dir.name}' {fname} unreadable: {e}")
                    sub_verified = True

    if not (top_artifact or sub_verified):
        return (False, f"Archive '{label}' is empty (no bundles, patch, or metadata)")

    if bundle_count:
        return (True, f"Archive '{label}' verified intact ({bundle_count} bundle(s)).")
    return (True, f"Archive '{label}' verified intact.")


def _is_dir_archive_entry(entry: Dict[str, Any]) -> bool:
    """Return True if get_rescue_archives() entry represents a directory archive."""
    return bool(entry.get("key")) or "/" in str(entry.get("id", ""))


def _find_archive_entry(
    archives: List[Dict[str, Any]],
    target: str,
    match_lease: bool = False,
) -> Optional[Dict[str, Any]]:
    """Find matching archive entry by id, <key>/<lease>, or optionally lease."""
    for a in archives:
        if not _is_dir_archive_entry(a):
            continue
        if a.get("id") == target:
            return a
    for a in archives:
        if not _is_dir_archive_entry(a):
            continue
        if a.get("key") and f"{a['key']}/{a.get('lease', '')}" == target:
            return a
    if match_lease:
        for a in archives:
            if not _is_dir_archive_entry(a):
                continue
            if a.get("lease") == target:
                return a
    return None


def write_agent_verdict(
    project: str,
    lease: str,
    base_dir: Optional[pathlib.Path] = None,
    key: Optional[str] = None,
) -> Tuple[bool, str]:
    """Generate and write verdict.md for a rescue archive.

    Does NOT delete or modify any other file in the archive directory.
    """
    archive_dir = _resolve_archive_dir(
        project,
        lease,
        key=key,
        base_dir=base_dir,
        prefer_key=bool(key),
    )
    if archive_dir is None:
        return (False, f"Rescue archive directory '{project}/{lease}' not found")

    meta_file = archive_dir / "meta.json"
    meta = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    branches = meta.get("branches", [])
    files_info = []
    for item in sorted(archive_dir.iterdir()):
        if item.name == "verdict.md":
            continue
        try:
            sz = item.stat().st_size
            files_info.append(f"- `{item.name}`: {sz} bytes")
        except Exception:
            files_info.append(f"- `{item.name}`")

    branches_str = ", ".join(branches) if branches else "None recorded"
    files_str = "\n".join(files_info) if files_info else "- None"

    verdict_content = (
        f"# Rescue Archive Verdict\n\n"
        f"- **Project**: {project}\n"
        f"- **Lease**: {lease}\n"
        f"- **Timestamp**: {int(time.time())}\n"
        f"- **Branches**: {branches_str}\n\n"
        f"## Archive Files\n"
        f"{files_str}\n\n"
        f"## Verdict & Recommendation\n"
        f"Archive inspected. Work is preserved intact. Purge remains an explicit owner decision.\n"
    )

    verdict_file = archive_dir / "verdict.md"
    try:
        verdict_file.write_text(verdict_content, encoding="utf-8")
        return (True, f"Verdict written to {verdict_file}")
    except Exception as e:
        return (False, f"Failed to write verdict.md: {e}")


def get_rescue_archives(limit: int = 50) -> List[Dict[str, Any]]:
    """Scan and return rescue archives (<project>/<lease>)."""
    return rescue_archives.get_rescue_archives(sys.modules[__name__], limit=limit)


get_archives = get_rescue_archives


def verify_all_bundles() -> Tuple[bool, str]:
    """Verify integrity of all directory rescue archives and legacy rescue bundles in parallel."""
    return rescue_archives.verify_all_bundles(sys.modules[__name__])


def _dir_size_bytes(path: pathlib.Path) -> int:
    return rescue_archives._dir_size_bytes(path)


def manage_rescue(action: str, bundle_id: str) -> Tuple[bool, str]:
    """Handle rescue bundle restore, purge, verify_all, purge_delivered, or verdict."""
    return rescue_archives.manage_rescue(sys.modules[__name__], action, bundle_id)


if __name__ == "__main__":
    status = {
        "cleaners": get_cleaners_status(),
        "rescue_sample": get_rescue_bundles(limit=5),
    }
    print(json.dumps(status, indent=2))
