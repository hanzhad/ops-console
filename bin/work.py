#!/usr/bin/env python3
"""What a slot folder holds, per local branch, without touching the network.

Reads the verdicts agent-slot.sh already cached in ~/.agent-slots: delivered/<key> and closed/<key>, where key is
<prefix>-<branch with / as _>-<tip>. Imported by check.py; prints one folder's verdict when run
with `work.py <prefix> <dir> [held]`. All git reads are of a directory an agent writes: no index lock, no fsmonitor.

saved       every branch delivered, nothing uncommitted
not sure    commits past a merged PR, or nothing known about a branch nobody holds
only here   uncommitted files, a stash, or commits on no remote at the HEAD of a held slot. A reused folder keeps
            old branches its holder never touched: those stay not sure
"""
import hashlib, json, os, pathlib, subprocess, sys

STATE = pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", pathlib.Path.home() / ".dev-containers"))
LEGACY_STATE = pathlib.Path(os.environ.get("AGENT_SLOT_STATE", pathlib.Path.home() / ".agent-slots"))
RANK = {"saved": 0, "not sure": 1, "only here": 2}


def git(d, *a, timeout=10):
    return subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(d), *a],
                          capture_output=True, text=True, timeout=timeout, check=True).stdout


def nested_clone_clean(d, name):
    """A clean clone under an untracked folder, such as acme-org's docs/fix: nothing there is at risk."""
    try:
        clones = [p.parent for p in (d / name).glob("*/.git")] + ([d / name] if (d / name / ".git").exists() else [])
        return bool(clones) and all(not git(c, "status", "--porcelain").strip() and not int(git(c, "rev-list", "--count", "HEAD", "--not", "--remotes")) for c in clones)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def branch_verdict(d, prefix, branch, tip, current):
    key = f"{prefix}-{branch.replace('/', '_')}-{tip}"
    if (STATE / "delivered" / key).exists() or (LEGACY_STATE / "delivered" / key).exists():
        return "saved", None
    closed = STATE / "closed" / key if (STATE / "closed" / key).exists() else LEGACY_STATE / "closed" / key
    if closed.exists():
        report = closed.read_text(errors="replace").strip()
        side = closed.with_name(key + ".merge-commits")
        merged = side.exists() and side.read_text().strip()
        if merged and " 0 commits beyond" in report:
            return "saved", f"the script still holds it, so Remove is refused: {report[:90]}"
        return "not sure", report[:120]
    commits = int(git(d, "rev-list", "--count", tip, "--not", "--remotes"))
    if not commits:
        return "saved", None
    return ("only here" if current else "not sure"), f"{branch}: {commits} commits on no remote"


def verdict(d, prefix, held):
    """{"work", "why": [...], "changed", "stashes", "branches"}; {"work": None} when git cannot answer."""
    d = pathlib.Path(d)
    try:
        status = git(d, "status", "--porcelain").splitlines()
        loose = [l for l in status if not (l.startswith("?? ") and l.endswith("/") and nested_clone_clean(d, l[3:].rstrip("/")))]
        stashes = len(git(d, "stash", "list").splitlines())
        rows = [r.split("\t") for r in git(d, "for-each-ref", "--format=%(refname:short)\t%(objectname)", "refs/heads").splitlines()]
        head = git(d, "rev-parse", "HEAD").strip()
        if head not in {t for _, t in rows}:
            rows.append(["HEAD", head])
        found = [branch_verdict(d, prefix, b, t, held and t == head) for b, t in rows]
        moved = [os.lstat(d / ".git/logs/HEAD").st_mtime] if (d / ".git/logs/HEAD").exists() else []
        for l in status[:100]:
            try:
                moved.append(os.lstat(d / l[3:].split(" -> ")[-1].strip('"')).st_mtime)
            except OSError:
                pass
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"work": None, "why": ["git could not answer"], "changed": None, "stashes": None, "branches": None}
    why = [w for _, w in sorted(found, key=lambda f: -RANK[f[0]]) if w]
    if loose:
        why.insert(0, f"{len(loose)} uncommitted: {', '.join(l[3:] for l in loose[:3])}")
    if stashes:
        why.insert(0, f"{stashes} stash")
    work = "only here" if loose or stashes else max((v for v, _ in found), key=RANK.get, default="saved")
    head_saved = next((v for (_, tip), (v, _) in zip(rows, found) if tip == head), "saved") == "saved"
    return {"work": work, "why": why[:6], "changed": len(loose), "stashes": stashes, "branches": len(rows), "head": head,
            "moved_at": max(moved, default=None), "reuse_clean": not status and not stashes and head_saved,
            "sig": hashlib.sha1(json.dumps([rows, status, stashes]).encode()).hexdigest()[:12]}


if __name__ == "__main__":
    print(json.dumps(verdict(sys.argv[2], sys.argv[1], len(sys.argv) > 3), ensure_ascii=False))
