#!/usr/bin/env python3
"""Tests for zero-dependency --demo fixture seeding."""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw import demo  # noqa: E402
from sw.store import Store  # noqa: E402


class TestDemoMode(unittest.TestCase):
    def test_seed_demo_populates_all_eight_blocks_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "demo.db")
            store.boot()
            demo.seed_demo(store)
            rev, blocks, _, events = store.full_view()
            self.assertGreater(rev, 0)
            self.assertEqual(
                {b["name"] for b in blocks},
                {"host", "sessions", "slots", "cleaners", "runners", "ci_gate", "gh_queue", "autoreview"},
            )
            self.assertGreaterEqual(len(events), 4)
            store.close()


if __name__ == "__main__":
    unittest.main()
