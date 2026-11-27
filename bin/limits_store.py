#!/usr/bin/env python3
"""Flock-serialized atomic read-modify-write store for $DEV_CONTAINER_HOME/limits.json."""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import sys
import tempfile
from typing import Callable, Tuple, TypeVar

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw.limits_core import parse_limits  # noqa: E402

T = TypeVar("T")


def default_state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DEV_CONTAINER_HOME", pathlib.Path.home() / ".dev-containers"))


def update_limits(state_dir: pathlib.Path, mutate: Callable[[dict], Tuple[dict, T]]) -> T:
    """Acquire an exclusive flock on limits.lock, read-modify-write limits.json atomically, and return result."""
    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "limits.lock"
    limits_path = state_dir / "limits.json"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                raw = limits_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            doc = parse_limits(raw)
            updated, result = mutate(doc)
            payload = json.dumps(updated, indent=2) + "\n"
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=state_dir,
                    prefix=".limits.",
                    suffix=".tmp",
                    delete=False,
                ) as tmp:
                    tmp_path = pathlib.Path(tmp.name)
                    tmp.write(payload)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, limits_path)
            except BaseException:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
