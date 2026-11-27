"""Paths and settings, read from the environment at import time. Opens nothing, starts nothing."""

import os
import pathlib
import secrets
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
BIN = HERE / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

PORT = int(os.environ.get("OPS_CONSOLE_PORT", "7631"))
TOKEN = secrets.token_urlsafe(24)  # regenerated on every boot

CACHE_DIR = pathlib.Path.home() / ".cache/ops-console"
LOG = CACHE_DIR / "actions.log"
DB_PATH = CACHE_DIR / "state.db"  # a read model on WSL ext4: deletable, rebuilt
