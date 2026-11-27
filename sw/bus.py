"""In-process fan-out for the SSE streams: a Condition and a ring of the last frames."""

import collections
import threading

from sw import core

RING = 2000


class Bus:
    def __init__(self, size=RING):
        self.cond = threading.Condition()
        self.ring = collections.deque(maxlen=size)  # (rev, frame bytes), in rev order

    def publish(self, event, payload, rev):
        """Called by the store under its lock, so ring order is rev order."""
        frame = core.sse_frame(event, payload, rev)
        with self.cond:
            self.ring.append((rev, frame))
            self.cond.notify_all()

    def wait_after(self, last_rev, timeout):
        """Blocks until a frame newer than last_rev exists or the timeout passes: (frames, gap, newest rev)."""
        with self.cond:
            frames, gap = core.frames_after(list(self.ring), last_rev)
            if not (frames or gap):
                self.cond.wait(timeout)
                frames, gap = core.frames_after(list(self.ring), last_rev)
            return frames, gap, (self.ring[-1][0] if self.ring else last_rev)
