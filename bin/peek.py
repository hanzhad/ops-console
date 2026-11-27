#!/usr/bin/env python3
"""peek.py <slot id> status | branches | log <ref> | show <ref> | diff <ref> | file <path> | grep <text>

The only thing check.py's model can run: reads of one slot folder, clipped. What a slot holds was written by an agent,
so git runs with nothing of the slot's config that can execute (fsmonitor, pager, external diff, textconv), a ref must
look like a ref, and a file must resolve inside the folder.
"""
import pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import projects

LIMIT = 8000


def git(d, *a):
    p = subprocess.run(["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "diff.external=",
                        "-C", str(d), *a], capture_output=True, text=True, errors="replace", timeout=20)
    return p.stdout if p.returncode == 0 else f"(git failed: {p.stderr.strip()[:200]})"


def main(sid, verb, arg=None):
    found = projects.slot_of(projects.discover(), sid)
    if not found:
        return f"not a slot: {sid}"
    d = found[0]["slots_dir"] / f"slot-{found[1]}"
    ref_ok = arg is not None and re.fullmatch(r"[\w][\w./@^~-]{0,200}", arg)
    if verb == "status":
        return git(d, "status", "--short", "--branch") + "\nstashes:\n" + git(d, "stash", "list")
    if verb == "branches":
        return git(d, "for-each-ref", "--format=%(refname:short) %(objectname:short) %(committerdate:relative) %(upstream:track)", "refs/heads")
    if verb == "log" and ref_ok:
        return git(d, "log", "--stat", "--no-color", "-15", arg, "--not", "--remotes")
    if verb == "show" and ref_ok:
        return git(d, "show", "--no-ext-diff", "--no-textconv", "--no-color", arg)
    if verb == "diff" and ref_ok:
        return git(d, "diff", "--no-ext-diff", "--no-textconv", "--no-color", arg)
    if verb == "file" and arg:
        f = (d / arg).resolve()
        if not f.is_relative_to(d.resolve()) or not f.is_file():
            return "no such file in this slot"
        return f.read_text(errors="replace")
    if verb == "grep" and arg:
        return git(d, "grep", "-n", "--untracked", "--no-color", "-e", arg)
    return __doc__


print(main(*sys.argv[1:4])[:LIMIT])
