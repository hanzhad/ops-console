#!/usr/bin/env python3
"""Rescue archive discovery, verification, and lifecycle operations."""

import concurrent.futures
import json
import pathlib
import sys
import time
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import projects  # noqa: E402


def _normalize_archive_repos(raw: Any) -> List[Dict[str, Any]]:
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _read_archive_repos(eng: Any, proj: str, lease: str, key: str) -> List[Dict[str, Any]]:
    adir = eng._resolve_archive_dir(proj, lease, key=key, prefer_key=bool(key))
    if adir is None:
        return []
    meta_f = adir / "meta.json"
    if not meta_f.is_file():
        return []
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
        return _normalize_archive_repos(meta.get("repos")) if isinstance(meta, dict) else []
    except Exception:
        return []


def get_rescue_archives(eng: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """Scan and return rescue archives (<project>/<lease>).

    Queries dev-container status --json first. Falls back to scanning rescue directories.
    """
    status = eng.get_engine_status_json()
    if status and "archives" in status and isinstance(status["archives"], list):
        mapped = []
        for arch in status["archives"]:
            proj = arch.get("project", "")
            key = arch.get("key") or proj
            lease = arch.get("lease", "")
            aid = arch.get("id") or f"{proj}/{lease}"
            sz = arch.get("size") if arch.get("size") is not None else arch.get("size_bytes", 0)
            sz_mb = round(sz / (1024 * 1024), 2) if sz else 0.0
            age = arch.get("age") if arch.get("age") is not None else arch.get("age_seconds", 0)
            branches = arch.get("branches", [])
            repos = (
                _normalize_archive_repos(arch["repos"])
                if isinstance(arch.get("repos"), list)
                else _read_archive_repos(eng, proj, lease, key)
            )
            has_verdict = bool(arch.get("has_verdict") or arch.get("verdict"))
            verdict_summary = arch.get("verdict_summary", "")

            if not verdict_summary:
                vf = eng._find_verdict_file(proj, lease, key=key)
                if vf is not None:
                    has_verdict = True
                    try:
                        verdict_summary = vf.read_text(encoding="utf-8", errors="ignore")[:300]
                    except Exception:
                        pass

            mapped.append({
                "id": aid,
                "project": proj,
                "key": key,
                "lease": lease,
                "size": sz,
                "size_bytes": sz,
                "size_mb": sz_mb,
                "age": age,
                "age_seconds": age,
                "branches": branches,
                "repos": repos,
                "verdict": has_verdict,
                "has_verdict": has_verdict,
                "verdict_summary": verdict_summary,
                "can_purge": False,
            })
        return mapped

    # Fallback to scanning filesystem
    archives: List[Dict[str, Any]] = []
    seen = set()
    now = time.time()

    for state_dir in (eng.DEV_CONTAINER_STATE, eng.AGENT_SLOT_STATE):
        rescue_dir = state_dir / "rescue"
        if not rescue_dir.exists():
            continue

        # Look for <project_or_key>/<lease>/ directories
        for proj_dir in sorted(rescue_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            proj = proj_dir.name
            for lease_dir in sorted(proj_dir.iterdir()):
                if not lease_dir.is_dir():
                    continue
                lease = lease_dir.name
                aid = f"{proj}/{lease}"
                if aid in seen:
                    continue
                seen.add(aid)

                total_sz = eng._dir_size_bytes(lease_dir)

                meta = {}
                meta_f = lease_dir / "meta.json"
                if meta_f.exists():
                    try:
                        meta = json.loads(meta_f.read_text(encoding="utf-8"))
                    except Exception:
                        pass

                vf = lease_dir / "verdict.md"
                has_verdict = vf.exists()
                v_summary = ""
                if has_verdict:
                    try:
                        v_summary = vf.read_text(encoding="utf-8", errors="ignore")[:300]
                    except Exception:
                        pass

                age = int(now - lease_dir.stat().st_mtime)
                archives.append({
                    "id": aid,
                    "project": proj,
                    "key": proj,
                    "lease": lease,
                    "size": total_sz,
                    "size_bytes": total_sz,
                    "size_mb": round(total_sz / (1024 * 1024), 2),
                    "age": age,
                    "age_seconds": age,
                    "branches": meta.get("branches", []),
                    "repos": _normalize_archive_repos(meta.get("repos")),
                    "verdict": has_verdict,
                    "has_verdict": has_verdict,
                    "verdict_summary": v_summary,
                    "can_purge": False,
                })
                if len(archives) >= limit:
                    break

    # Also include legacy rescue bundles if limit not reached
    legacy_bundles = eng.get_rescue_bundles(limit=limit)
    for b in legacy_bundles:
        bid = b["id"]
        if bid not in seen:
            seen.add(bid)
            archives.append({
                "id": bid,
                "project": b["project"],
                "lease": b.get("lease_id", bid),
                "size": b.get("size_bytes", 0),
                "size_bytes": b.get("size_bytes", 0),
                "size_mb": b.get("size_mb", 0.0),
                "age": int(now - b.get("created_at", now)),
                "age_seconds": int(now - b.get("created_at", now)),
                "branches": [h["ref"] for h in b.get("unique_heads", [])],
                "repos": [],
                "verdict": False,
                "has_verdict": False,
                "verdict_summary": "",
                "can_purge": b.get("can_purge", False),
            })
            if len(archives) >= limit:
                break

    return archives


def verify_all_bundles(eng: Any) -> Tuple[bool, str]:
    """Verify integrity of all directory rescue archives and legacy rescue bundles in parallel."""
    bundles = eng.get_rescue_bundles(limit=500)
    legacy_ids = {b["id"] for b in bundles}
    dir_archives = [
        a for a in eng.get_rescue_archives(limit=500)
        if eng._is_dir_archive_entry(a) and a.get("id") not in legacy_ids
    ]

    if not dir_archives and not bundles:
        return (True, "No rescue bundles found")

    def _verify_one_bundle(b: Dict[str, Any]) -> Tuple[bool, str]:
        bid = b["id"]
        bpath = pathlib.Path(b["bundle_path"])
        if not bpath.exists():
            return (False, f"{bid}: missing")
        target_repo = eng._pick_git_repo(b.get("project", ""))
        ok, err = eng._git_bundle_verify(bpath, target_repo, timeout=20)
        return (True, bid) if ok else (False, f"{bid} ({err[:60]})")

    def _verify_one_archive(a: Dict[str, Any]) -> Tuple[bool, str]:
        aid = a.get("id") or f"{a.get('project', '')}/{a.get('lease', '')}"
        adir = eng._resolve_archive_dir(
            a.get("project", ""),
            a.get("lease", ""),
            key=a.get("key"),
            prefer_key=bool(a.get("key")),
        )
        if adir is None:
            return (False, f"{aid}: missing")
        ok, msg = eng._verify_archive_dir(adir, a.get("project", ""), aid)
        return (True, aid) if ok else (False, msg)

    verified = 0
    failed = []
    total = len(dir_archives) + len(bundles)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [
            *(ex.submit(_verify_one_archive, a) for a in dir_archives),
            *(ex.submit(_verify_one_bundle, b) for b in bundles),
        ]
        for fut in concurrent.futures.as_completed(futures):
            ok, msg = fut.result()
            if ok:
                verified += 1
            else:
                failed.append(msg)

    if not failed:
        return (True, f"All {verified} rescue archive(s)/bundle(s) verified intact")
    return (False, f"Verified {verified}/{total}, issues: {', '.join(failed[:3])}")


def _dir_size_bytes(path: pathlib.Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def manage_rescue(eng: Any, action: str, bundle_id: str) -> Tuple[bool, str]:
    """Handle rescue bundle restore, purge, verify_all, purge_delivered, or verdict."""
    if action in ("verdict", "agent_verdict"):
        archives = eng.get_rescue_archives(limit=500)
        matched = eng._find_archive_entry(archives, bundle_id, match_lease=True)
        if matched is not None:
            return eng.write_agent_verdict(
                matched.get("project", ""),
                matched.get("lease", ""),
                key=matched.get("key"),
            )
        if "/" in bundle_id:
            proj, _, lease = bundle_id.partition("/")
            return eng.write_agent_verdict(proj, lease)
        parsed = projects.parse_rescue_bundle_id(bundle_id)
        if parsed:
            proj, slot_num, _ = parsed
            return eng.write_agent_verdict(proj, f"slot-{slot_num}")
        return (False, f"Cannot identify project and lease from '{bundle_id}'")

    if action == "verify_all" or bundle_id == "all":
        return eng.verify_all_bundles()

    bundles = eng.get_rescue_bundles(limit=500)
    legacy_ids = {b["id"] for b in bundles}
    archives = [
        a for a in eng.get_rescue_archives(limit=500)
        if eng._is_dir_archive_entry(a) and a.get("id") not in legacy_ids
    ]

    if action == "purge_delivered":
        delivered_archives = [a for a in archives if a.get("can_purge")]
        delivered_bundles = [b for b in bundles if b.get("can_purge")]
        if not delivered_archives and not delivered_bundles:
            return (True, "No delivered bundles to purge")
        purged = 0
        bytes_freed = 0
        for a in delivered_archives:
            adir = eng._resolve_archive_dir(
                a.get("project", ""),
                a.get("lease", ""),
                key=a.get("key"),
                prefer_key=bool(a.get("key")),
            )
            if adir is None:
                continue
            try:
                bytes_freed += eng._dir_size_bytes(adir)
                eng.shutil.rmtree(adir)
                purged += 1
            except Exception:
                pass
        for b in delivered_bundles:
            bp = pathlib.Path(b["bundle_path"])
            try:
                if bp.exists():
                    sz = bp.stat().st_size
                    bp.unlink(missing_ok=True)
                    bytes_freed += sz
                    purged += 1
                for extra in bp.parent.glob(f"{b['id']}*"):
                    extra.unlink(missing_ok=True)
            except Exception:
                pass
        mb = round(bytes_freed / (1024 * 1024), 1)
        return (True, f"Purged {purged} delivered archive(s)/bundle(s), freed {mb} MB")

    target_archive = eng._find_archive_entry(archives, bundle_id)
    if target_archive is not None:
        adir = eng._resolve_archive_dir(
            target_archive.get("project", ""),
            target_archive.get("lease", ""),
            key=target_archive.get("key"),
            prefer_key=bool(target_archive.get("key")),
        )
        if action == "purge":
            if not target_archive.get("can_purge"):
                return (
                    False,
                    f"refusing to purge '{bundle_id}': archive is not marked delivered/purgeable — "
                    "only a verified archive whose every branch is on GitHub can be purged",
                )
            if adir is None:
                return (False, f"Rescue archive '{bundle_id}' not found")
            try:
                eng.shutil.rmtree(adir)
                return (True, f"Archive '{bundle_id}' purged successfully")
            except Exception as e:
                return (False, f"Failed to purge '{bundle_id}': {e}")
        elif action == "restore":
            if adir is None:
                return (False, f"Rescue archive '{bundle_id}' not found")
            return eng._verify_archive_dir(adir, target_archive.get("project", ""), bundle_id)
        return (False, f"Unknown rescue action '{action}'")

    target = next((b for b in bundles if b["id"] == bundle_id), None)
    if not target:
        if "/" in bundle_id and action in ("restore", "purge"):
            proj, _, lease = bundle_id.partition("/")
            adir = eng._resolve_archive_dir(proj, lease)
            if adir is None:
                return (False, f"Rescue archive '{bundle_id}' not found")
            if action == "purge":
                return (
                    False,
                    f"refusing to purge '{bundle_id}': archive is not marked delivered/purgeable",
                )
            return eng._verify_archive_dir(adir, proj, bundle_id)
        return (False, f"Rescue bundle '{bundle_id}' not found")

    bundle_path = pathlib.Path(target["bundle_path"])

    if action == "purge":
        if not target.get("can_purge"):
            return (False, f"refusing to purge '{bundle_id}': audit says '{target.get('delivery_status')}' — "
                           "only a verified bundle whose every branch is on GitHub can be purged")
        try:
            bundle_path.unlink()
            return (True, f"Bundle '{bundle_id}' purged successfully")
        except Exception as e:
            return (False, f"Failed to purge '{bundle_id}': {e}")

    elif action == "restore":
        # Unbundle verification against repository context
        try:
            repo_cwd = eng._pick_git_repo(target.get("project", ""))
            ok, err = eng._git_bundle_verify(bundle_path, repo_cwd, timeout=30)
            if ok:
                return (True, f"Bundle '{bundle_id}' verified intact.")
            return (False, f"Bundle verification: {err[:200]}")
        except Exception as e:
            return (False, f"Failed to restore '{bundle_id}': {e}")

    return (False, f"Unknown rescue action '{action}'")
