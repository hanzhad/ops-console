#!/usr/bin/env python3
"""rescue_audit.py: is the work in each rescue bundle already on GitHub? Read-only toward bundles, slots and checkouts.

A bundle is `delivered` only when every refs/heads/* and refs/stash tip in it is accounted for:
  in_origin        the tip is reachable from an origin branch (rescue/* included) or a GitHub PR head (refs/pull/*/head)
  delivered_marker ~/.agent-slots/delivered/<prefix>-<branch with / as _>-<full sha> exists (PR merged at that tip)
  patch_in_master  not reachable, but `git cherry origin/master` finds every commit's patch there (rebase/cherry-pick)
Anything else is UNIQUE: work that exists only in this bundle (and maybe in a slot). A refs/stash tip is never
patch-checked, so a bundle holding a stash with unpushed content stays `unmerged`.

Results are cached in ~/.cache/ops-console/rescue-audit.json keyed by bundle id; an entry is reused while the bundle's
size is unchanged. The objects live in a private bare repo (~/.cache/ops-console/rescue-audit.git), never in a real
checkout. Usage: rescue_audit.py [--no-fetch] [--refresh]
"""
import json, os, pathlib, subprocess, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import projects  # noqa: E402

HOME = pathlib.Path.home()
STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", HOME / ".dev-containers"))
LEGACY_STATE = pathlib.Path(os.environ.get("AGENT_SLOT_STATE", HOME / ".agent-slots"))
RESCUE_DIRS = [STATE / "rescue", LEGACY_STATE / "rescue"]
CACHE_DIR = HOME / ".cache/ops-console"
CACHE = CACHE_DIR / "rescue-audit.json"
AUDIT_REPO = CACHE_DIR / "rescue-audit.git"


def load_cache():
    try:
        return {e["id"]: e for e in json.loads(CACHE.read_text())}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_cache(entries):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(entries.values(), key=lambda e: e["id"]), indent=1))
    tmp.replace(CACHE)


def _git(repo, *a, check=True, timeout=900):
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(a[:2])}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def _origin_for(prefix):
    """The GitHub origin of the project whose slot prefix is `prefix`, read off the project's main checkout."""
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        import projects
        p = projects.discover().get(prefix)
        if p:
            return subprocess.run(["git", "-C", str(p["main"]), "remote", "get-url", "origin"],
                                  capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        pass
    return None


def _repo_for(prefix, fetch):
    """One bare audit repo per project prefix, fetched from that project's origin (branches + PR heads)."""
    repo = AUDIT_REPO if prefix == "demo-app" else CACHE_DIR / f"rescue-audit-{prefix}.git"
    if not repo.exists():
        origin = _origin_for(prefix)
        if not origin:
            return None
        subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)
        _git(repo, "remote", "add", "origin", origin)
        fetch = True
    if fetch:
        _git(repo, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*",
             "+refs/pull/*/head:refs/pull/*/head", timeout=1800)
    return repo


def _delivered_markers():
    names = set()
    for d in (STATE / "delivered", LEGACY_STATE / "delivered"):
        if d.is_dir():
            names |= {p.name for p in d.iterdir()}
    return names


def audit_one(repo, bundle, prefix, markers):
    bid = bundle.stem
    st = bundle.stat()
    entry = {"id": bid, "size": st.st_size, "audited_at": int(time.time()), "heads": []}
    v = subprocess.run(["git", "-C", str(repo), "bundle", "verify", "-q", str(bundle)], capture_output=True, text=True)
    entry["verify_ok"] = v.returncode == 0
    if v.returncode:
        entry["verify_err"] = (v.stderr or v.stdout).strip()[-200:]
        entry["verdict"] = "verify_failed"
        return entry
    heads = []
    for line in _git(repo, "bundle", "list-heads", str(bundle)).splitlines():
        sha, ref = line.split(" ", 1)
        if ref.startswith("refs/heads/") or ref == "refs/stash":
            heads.append((sha, ref))
    ns = f"refs/audit/{bid}"
    if heads:
        _git(repo, "fetch", "-q", str(bundle), *[f"+{r}:{ns}/{r.removeprefix('refs/')}" for _, r in heads])
    for sha, ref in heads:
        name = ref.removeprefix("refs/heads/")
        ahead = _git(repo, "rev-list", sha, "--not", "--remotes=origin", "--glob=refs/pull/*").split()
        h = {"ref": name, "sha": sha, "ahead_of_origin": len(ahead)}
        if not ahead:
            h["status"] = "in_origin"
        elif f"{prefix}-{name.replace('/', '_')}-{sha}" in markers:
            h["status"] = "delivered_marker"
        else:
            plus = [c for c in _git(repo, "cherry", "refs/remotes/origin/master", sha, check=False).splitlines()
                    if c.startswith("+")]
            if ref != "refs/stash" and not plus:
                h["status"] = "patch_in_master"
            else:
                h["status"] = "UNIQUE"
                h["unique_commits"] = len(plus) if ref != "refs/stash" else len(ahead)
                h["subjects"] = _git(repo, "log", "--format=%s", "-3", sha).splitlines()
        entry["heads"].append(h)
    entry["verdict"] = "unmerged" if any(h["status"] == "UNIQUE" for h in entry["heads"]) else "delivered"
    return entry


def run(fetch=True, refresh=False, log=None):
    """Audit every bundle not yet in the cache (or all with refresh). Returns (entries_by_id, summary)."""
    cache = {} if refresh else load_cache()
    markers = _delivered_markers()
    repos = {}
    seen = set()
    for d in RESCUE_DIRS:
        if not d.is_dir():
            continue
        for b in sorted(d.glob("*.bundle")):
            bid = b.stem
            if bid in seen:
                continue
            seen.add(bid)
            old = cache.get(bid)
            if old and old.get("size") == b.stat().st_size and old.get("verdict") != "verify_failed":
                continue
            parsed = projects.parse_rescue_bundle_id(bid)
            prefix = parsed[0] if parsed else bid.split("-slot-")[0]
            if prefix not in repos:
                repos[prefix] = _repo_for(prefix, fetch)
            if not repos[prefix]:
                cache[bid] = {"id": bid, "size": b.stat().st_size, "verdict": "no_repo", "heads": []}
                continue
            try:
                cache[bid] = audit_one(repos[prefix], b, prefix, markers)
            except Exception as e:  # one unreadable bundle never stops the pass
                cache[bid] = {"id": bid, "size": b.stat().st_size, "verdict": "error", "error": str(e)[:300], "heads": []}
            if log:
                log(f"{bid}: {cache[bid]['verdict']}")
            save_cache(cache)  # incremental: a killed pass keeps what it already learned
    for gone in set(cache) - seen:
        cache.pop(gone)
    save_cache(cache)
    counts = {}
    for e in cache.values():
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    return cache, counts


if __name__ == "__main__":
    entries, counts = run(fetch="--no-fetch" not in sys.argv, refresh="--refresh" in sys.argv,
                          log=lambda s: print(s, file=sys.stderr, flush=True))
    print(json.dumps(counts))
